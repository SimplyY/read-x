import sqlite3
import sys
import tempfile
from pathlib import Path
import unittest
from reading_workflow_schema_migration import migrate_offline


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript('''PRAGMA foreign_keys=ON;
          CREATE TABLE jobs (job_id TEXT PRIMARY KEY, app_id TEXT NOT NULL,
            source_message_id TEXT, payload_json TEXT NOT NULL, UNIQUE(app_id, source_message_id));
          CREATE TABLE admission_attempts (task_id TEXT PRIMARY KEY, app_id TEXT NOT NULL,
            source_message_id TEXT NOT NULL, UNIQUE(app_id,source_message_id));
          CREATE TABLE artifacts (job_id TEXT NOT NULL REFERENCES jobs(job_id), value TEXT);
          INSERT INTO jobs VALUES ('old','app','om_old','{}');
          INSERT INTO admission_attempts VALUES ('old','app','om_old');
          INSERT INTO artifacts VALUES ('old','preserve');''')

    def tearDown(self): self.db.close()

    def test_preserves_old_rows_and_foreign_keys(self):
        migrate_offline(self.db)
        self.assertEqual(self.db.execute('SELECT article_key FROM jobs WHERE job_id=?',('old',)).fetchone(),('',))
        self.assertEqual(self.db.execute('SELECT * FROM artifacts').fetchone(),('old','preserve'))
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.db.execute("INSERT INTO jobs VALUES ('second','app','om_old','{}','article1')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO jobs VALUES ('third','app','om_old','{}','article1')")

    def test_incompatible_schema_is_rejected_without_changes(self):
        self.db.execute('DROP TABLE admission_attempts')
        self.db.commit()
        with self.assertRaises(ValueError): migrate_offline(self.db)
        self.assertNotIn('article_key', [r[1] for r in self.db.execute('PRAGMA table_info(jobs)')])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],1)

    def test_real_workflow_schema_retains_references_and_triggers(self):
        scripts = Path(__file__).resolve().parents[1]/'.agents/skills/long-read/scripts'
        sys.path.insert(0,str(scripts))
        try:
            from long_read_workflow_store import SCHEMA
            legacy = SCHEMA.replace("article_key TEXT NOT NULL DEFAULT '', UNIQUE(app_id, source_message_id,article_key)","UNIQUE(app_id, source_message_id)")
            legacy = legacy.replace("article_key TEXT NOT NULL DEFAULT '', UNIQUE(app_id,source_message_id,article_key)","UNIQUE(app_id,source_message_id)")
            self.assertNotIn('article_key TEXT',legacy)
            with tempfile.TemporaryDirectory() as directory:
                state=Path(directory).resolve()
                db=sqlite3.connect(state/'workflow.sqlite3')
                db.executescript(legacy)
                db.execute("INSERT INTO jobs(job_id,app_id,source_message_id,input_sha256,payload_json,status,created_at,updated_at) VALUES ('job','cli_app','om_old','hash','{}','pending',1,1)")
                db.execute("INSERT INTO artifacts(job_id,artifact_key,kind,required) VALUES ('job','pack','context_pack',1)")
                db.commit()
                backup=sqlite3.connect(state/'backup.sqlite3')
                db.backup(backup)
                self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(backup.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
                db.execute('PRAGMA foreign_keys=ON')
                migrate_offline(db)
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0],1)
                triggers={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
                self.assertTrue({'events_no_update','events_no_delete'} <= triggers)
                db.close()
                restored=sqlite3.connect(state/'restored.sqlite3')
                backup.backup(restored)
                self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertNotIn('article_key',{r[1] for r in restored.execute('PRAGMA table_info(jobs)')})
                self.assertEqual(restored.execute('PRAGMA foreign_key_check').fetchall(),[])
                self.assertEqual(restored.execute('SELECT count(*) FROM artifacts').fetchone()[0],1)
                restored.close(); backup.close()
        finally:
            sys.path.remove(str(scripts))

if __name__ == '__main__': unittest.main()
