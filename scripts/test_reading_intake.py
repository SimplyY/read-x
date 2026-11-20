#!/usr/bin/env python3
import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

from reading_intake import list_pending, register_event


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / "state"
        self.policy = {"allowed_app_id": "cli_app", "allowed_chat_id": "oc_group",
                       "allowed_sender_id": "ou_owner"}
        self.event = {"appId": "cli_app", "chatId": "oc_group", "chatType": "group",
                      "senderId": "ou_owner", "senderType": "user", "messageId": "om_first",
                      "urls": ["https://example.com/article?key=1#part"]}

    def tearDown(self):
        self.temp.cleanup()

    def register(self, event):
        return register_event(self.root, event, **self.policy)

    def test_repeat_and_distinct_messages_share_article_without_losing_state(self):
        self.assertEqual(self.register(self.event), {"status": "accepted", "articles": 1})
        self.assertEqual(self.register(self.event), {"status": "duplicate", "articles": 0})
        second = dict(self.event, messageId="om_second", urls=["https://example.com/article?key=1"])
        self.assertEqual(self.register(second), {"status": "accepted", "articles": 0})
        with sqlite3.connect(self.root / "intake.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM intake_articles").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM intake_messages").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT sync_status FROM intake_articles").fetchone()[0], "pending")
            self.assertEqual(db.execute("SELECT original_url FROM intake_articles").fetchone()[0], self.event["urls"][0])
        pending = list_pending(self.root)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["originalUrl"], self.event["urls"][0])
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.root / "intake.sqlite3").st_mode & 0o777, 0o600)

    def test_score_only_is_preserved_until_normal_submission(self):
        self.register(dict(self.event, scoreOnly=True))
        self.assertEqual(list_pending(self.root)[0]["packPolicy"], "score_only")
        self.register(dict(self.event, messageId="om_normal", scoreOnly=False))
        self.assertEqual(list_pending(self.root)[0]["packPolicy"], "normal")
        self.register(dict(self.event, messageId="om_again", scoreOnly=True))
        self.assertEqual(list_pending(self.root)[0]["packPolicy"], "normal")

    def test_changed_replay_and_unauthorized_events_are_rejected(self):
        self.register(self.event)
        for changes in ({"urls": ["https://other.example/"]}, {"senderType": "bot"},
                        {"chatId": "oc_other"}, {"senderId": "ou_other"},
                        {"messageId": "invalid"}, {"urls": ["file:///etc/passwd"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.register(dict(self.event, **changes))
        with sqlite3.connect(self.root / "intake.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM intake_articles").fetchone()[0], 1)

    def test_concurrent_replay_creates_one_article(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.register, (self.event, self.event)))
        self.assertEqual({r["status"] for r in results}, {"accepted", "duplicate"})
        with sqlite3.connect(self.root / "intake.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM intake_articles").fetchone()[0], 1)

    def test_multiple_urls_atomic_and_distinct_queries(self):
        event = dict(self.event, urls=["https://example.com/a?x=1", "https://example.com/a?x=2"])
        self.assertEqual(self.register(event)["articles"], 2)
        event = dict(self.event, messageId="om_second", urls=["https://example.com/b", "https://bad.example:invalid/"])
        with self.assertRaises(ValueError):
            self.register(event)
        with sqlite3.connect(self.root / "intake.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM intake_messages").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM intake_articles").fetchone()[0], 2)

    def test_verified_source_time_is_preserved_for_new_article(self):
        created_ms = 1790000000000
        event = dict(self.event, sourceCreatedAt=str(created_ms))
        self.register(event)
        expected = datetime.fromtimestamp(created_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(list_pending(self.root)[0]["createdAt"], expected)

    def test_invalid_or_implausible_source_time_uses_local_intake_time(self):
        event = dict(self.event, sourceCreatedAt="1790000000000junk")
        self.register(event)
        created_at = list_pending(self.root)[0]["createdAt"]
        self.assertNotEqual(created_at, "1790000000junk")
        self.assertRegex(created_at, r"^20[0-9]{2}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$")

    def test_cli_survives_process_restart(self):
        command = [sys.executable, str(Path(__file__).with_name("reading_intake.py")),
                   "--state-dir", str(self.root), "--allowed-app-id", "cli_app",
                   "--allowed-chat-id", "oc_group", "--allowed-sender-id", "ou_owner"]
        first = subprocess.run(command, input=json.dumps(self.event), text=True, capture_output=True, check=True)
        second = subprocess.run(command, input=json.dumps(self.event), text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(first.stdout)["status"], "accepted")
        self.assertEqual(json.loads(second.stdout)["status"], "duplicate")

    def test_rejects_public_directory_and_symlink_database(self):
        self.root.mkdir(mode=0o755)
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            self.register(self.event)
        self.root.chmod(0o700)
        (self.root / "intake.sqlite3").symlink_to(Path(self.temp.name) / "other")
        with self.assertRaises(ValueError):
            self.register(self.event)


if __name__ == "__main__":
    unittest.main()
