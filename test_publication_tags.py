from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import sqlite3
import tempfile
import unittest
from pathlib import Path

from community_store import CommunityStore, normalize_tags


class PublicationTagTests(unittest.TestCase):
    def test_normalization_trims_deduplicates_and_preserves_order(self):
        self.assertEqual(
            normalize_tags([" Shadow Slave ", "shadow slave", "", "Fantasia"]),
            ["Shadow Slave", "Fantasia"],
        )

    def test_invalid_and_excess_tags_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid_tags"):
            normalize_tags(["bad/tag"])
        with self.assertRaisesRegex(ValueError, "too_many_tags"):
            normalize_tags([f"tag{i}" for i in range(21)])

    def test_tags_are_persisted_and_read_back(self):
        path = Path(tempfile.mkdtemp()) / "community.sqlite3"
        store = CommunityStore(path)
        try:
            post_id = store.create_post(
                user_id="owner", title="Capítulo", tags=["A", "a", "B"])
            post = store.get_post(post_id)
            self.assertEqual(post["tags"], ["A", "B"])
            self.assertEqual(store.list_user_posts("owner")[0]["tags"], ["A", "B"])
            columns = {row[1] for row in store._conn.execute("PRAGMA table_info(community_posts)")}
            self.assertIn("tags_json", columns)
        finally:
            store.close()

    def test_existing_v1_database_gets_tags_column_without_data_loss(self):
        path = Path(tempfile.mkdtemp()) / "legacy.sqlite3"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta(key,value) VALUES('schema_version','1');
            CREATE TABLE community_posts(
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, source_job_id TEXT,
                source_run_id TEXT, series_title TEXT, series_slug TEXT,
                episode_number TEXT, output_dir TEXT, title TEXT, description TEXT,
                cover_reference TEXT, status TEXT NOT NULL, visibility TEXT NOT NULL,
                moderation_status TEXT NOT NULL, views INTEGER DEFAULT 0,
                published_at REAL, unpublished_at REAL, created_at REAL, updated_at REAL
            );
            INSERT INTO community_posts(id,user_id,status,visibility,moderation_status)
            VALUES('p1','owner','draft','public','pending');
            """
        )
        conn.close()
        store = CommunityStore(path)
        try:
            self.assertEqual(store.get_post("p1")["tags"], [])
            self.assertEqual(store._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "4")
            self.assertIsNotNone(store._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='community_profiles'"
            ).fetchone())
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
