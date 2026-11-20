#!/usr/bin/env python3
"""Offline-only SQLite migration primitive for per-article workflow identity.

The caller must drain *all* old readers/writers and take a verified SQLite
backup first. This module does not open production databases or deploy code.
The operation is atomic: an exception rolls back both rebuilt tables. Rollback
after a successful migration requires restoring the pre-migration backup while
all processes remain stopped; never discard writes from a subsequently started
process. Existing rows keep their old IDs and use article_key=''.
"""
from __future__ import annotations

import re
import sqlite3


def migrate_offline(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        raise ValueError('migration requires an idle connection')
    specs = []
    for table in ('jobs', 'admission_attempts'):
        row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not row or not row[0]:
            raise ValueError('required workflow table is absent')
        cols = [r[1] for r in connection.execute(f'PRAGMA table_info({table})')]
        if 'article_key' in cols:
            raise ValueError('schema already migrated or incompatible')
        sql, count = re.subn(r'UNIQUE\s*\(\s*app_id\s*,\s*source_message_id\s*\)',
                             "article_key TEXT NOT NULL DEFAULT '', UNIQUE(app_id,source_message_id,article_key)",
                             row[0], flags=re.IGNORECASE)
        if count != 1:
            raise ValueError('unexpected workflow uniqueness constraint')
        objects = connection.execute("SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (table,)).fetchall()
        specs.append((table, cols, sql, [r[0] for r in objects]))

    prior_fk = connection.execute('PRAGMA foreign_keys').fetchone()[0]
    prior_legacy = connection.execute('PRAGMA legacy_alter_table').fetchone()[0]
    try:
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute('PRAGMA legacy_alter_table=ON')
        connection.execute('BEGIN EXCLUSIVE')
        for table, cols, sql, objects in specs:
            old = table + '__article_v1'
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (old,)).fetchone():
                raise ValueError('stale migration table exists')
            connection.execute(f'ALTER TABLE "{table}" RENAME TO "{old}"')
            connection.execute(sql)
            names = ','.join('"'+name.replace('"','""')+'"' for name in cols)
            connection.execute(f'INSERT INTO "{table}" ({names}) SELECT {names} FROM "{old}"')
            connection.execute(f'DROP TABLE "{old}"')
            for statement in objects:
                connection.execute(statement)
        if connection.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('foreign key check failed')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute(f'PRAGMA legacy_alter_table={int(prior_legacy)}')
        connection.execute(f'PRAGMA foreign_keys={int(prior_fk)}')
