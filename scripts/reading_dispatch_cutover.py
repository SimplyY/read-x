"""Offline-only marker creation after the old Bridge and workers are drained.

The caller must verify a restorable backup and that no legacy process can
resume, then choose a millisecond timestamp no earlier than that barrier.
Never call this automatically during Bridge startup or message processing.
"""
import sqlite3


def establish_cutover(connection: sqlite3.Connection, cutover_ms: int, *, legacy_drained: bool = False) -> None:
    if legacy_drained is not True or type(cutover_ms) is not int or cutover_ms < 1000000000000:
        raise ValueError('verified drained cutover is required')
    if connection.in_transaction: raise ValueError('idle connection required')
    connection.execute('BEGIN EXCLUSIVE')
    try:
        connection.execute('''CREATE TABLE IF NOT EXISTS reading_dispatch_cutover (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), cutover_ms INTEGER NOT NULL)''')
        existing=connection.execute('SELECT cutover_ms FROM reading_dispatch_cutover WHERE singleton=1').fetchone()
        if existing and existing[0] != cutover_ms: raise ValueError('cutover cannot be rewritten')
        connection.execute('INSERT OR IGNORE INTO reading_dispatch_cutover VALUES (1,?)',(cutover_ms,))
        connection.commit()
    except BaseException:
        connection.rollback(); raise
