"""Offline-only migration for an empty, prematurely created v2 effect table.

Caller must stop all writers and take a verified SQLite backup first. This
function is never invoked by the running application. Restore that backup to
roll back only while every writer is stopped and no later data would be lost.
"""
import sqlite3


def migrate_empty_legacy_v2(connection: sqlite3.Connection) -> None:
    if connection.in_transaction: raise ValueError('migration requires an idle connection')
    expected={'app_id','message_id','article_key','request_id','scoring_sha','status'}
    columns={row[1] for row in connection.execute('PRAGMA table_info(score_send_effects_v2)')}
    if columns != expected: raise ValueError('unexpected effect schema')
    connection.execute('BEGIN EXCLUSIVE')
    try:
        if connection.execute('SELECT 1 FROM score_send_effects_v2 LIMIT 1').fetchone():
            raise ValueError('nonempty legacy effects require manual reconciliation')
        for name in ('card_sha','target_type','target_id'):
            connection.execute(f"ALTER TABLE score_send_effects_v2 ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        if connection.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('foreign key check failed')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
