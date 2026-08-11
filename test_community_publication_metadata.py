"""Publication metadata contract tests: fake + Supabase transport, no network."""

import _test_bootstrap  # noqa: F401

import json
import unittest
from dataclasses import dataclass
from uuid import uuid4

from community_publication_metadata import (
    FakePublicationMetadataRepository,
    PublicationMetadataError,
    SupabasePublicationMetadataRepository,
    build_publication_metadata_repository,
)
from supabase_social import SocialConfig


def _metadata(**overrides):
    base = {
        "publication_id": str(uuid4()),
        "owner_user_id": str(uuid4()),
        "job_id": f"job-{uuid4()}",
        "run_id": f"run-{uuid4()}",
        "artifact_sha256": "a" * 64,
        "artifact_size_bytes": 1234,
        "mime_type": "application/pdf",
        "storage_provider": "google_drive",
        "storage_reference": f"drive-{uuid4()}",
        "title": "Chapter publication",
    }
    base.update(overrides)
    return base


@dataclass
class _Resp:
    status: int
    content: bytes
    headers: dict


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.queue = []

    def push(self, status, body):
        payload = json.dumps(body).encode() if body is not None else b""
        self.queue.append(_Resp(status, payload, {}))

    def request(self, method, url, *, headers=None, data=None, stream=False):
        self.calls.append({
            "method": method,
            "url": url,
            "headers": headers or {},
            "data": json.loads(data) if data else None,
        })
        if not self.queue:
            return _Resp(200, b"[]", {})
        return self.queue.pop(0)


CONFIG = SocialConfig(url="https://proj.supabase.co", publishable_key="sb_publishable_pub")


class FakePublicationMetadataContractTests(unittest.TestCase):
    def test_public_view_never_exposes_storage_reference(self):
        repo = FakePublicationMetadataRepository()
        meta = _metadata()
        repo.reserve(meta)
        repo.finalize(meta)
        public = repo.get_publication(meta["publication_id"]).public()
        self.assertNotIn("storage_reference", public)
        self.assertNotIn(meta["storage_reference"], json.dumps(public))

    def test_duplicate_owner_artifact_is_rejected_while_active(self):
        repo = FakePublicationMetadataRepository()
        first = _metadata()
        second = _metadata(owner_user_id=first["owner_user_id"],
                           artifact_sha256=first["artifact_sha256"])
        repo.reserve(first)
        with self.assertRaisesRegex(PublicationMetadataError, "duplicate_artifact_publication"):
            repo.reserve(second)

    def test_list_publications_returns_only_published_rows(self):
        repo = FakePublicationMetadataRepository()
        published = _metadata()
        pending = _metadata(owner_user_id=published["owner_user_id"], artifact_sha256="b" * 64)
        repo.reserve(pending)
        repo.reserve(published)
        repo.finalize(published)
        rows = repo.list_publications(owner_user_id=published["owner_user_id"])
        self.assertEqual([r.publication_id for r in rows], [published["publication_id"]])

    def test_finalize_requires_server_storage_reference(self):
        repo = FakePublicationMetadataRepository()
        meta = _metadata(storage_reference="")
        repo.reserve(meta)
        with self.assertRaisesRegex(PublicationMetadataError, "missing_storage_reference"):
            repo.finalize(meta)


class SupabasePublicationMetadataRepositoryTests(unittest.TestCase):
    def test_reserve_upserts_private_artifact_without_storage_reference(self):
        transport = FakeTransport()
        meta = _metadata()
        transport.push(201, [{**meta, "storage_reference": None, "publication_status": "reserved"}])
        SupabasePublicationMetadataRepository(
            CONFIG, access_token="server-token", transport=transport).reserve(meta)
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertIn("/rest/v1/community_publication_artifacts", call["url"])
        self.assertIn("on_conflict=publication_id", call["url"])
        self.assertEqual(call["data"]["publication_status"], "reserved")
        self.assertNotIn("storage_reference", call["data"])
        self.assertEqual(call["headers"]["Authorization"], "Bearer server-token")

    def test_finalize_patches_published_state_with_storage_reference(self):
        transport = FakeTransport()
        meta = _metadata()
        transport.push(200, [{**meta, "publication_status": "published"}])
        SupabasePublicationMetadataRepository(
            CONFIG, access_token="server-token", transport=transport).finalize(meta)
        call = transport.calls[0]
        self.assertEqual(call["method"], "PATCH")
        self.assertIn(f"publication_id=eq.{meta['publication_id']}", call["url"])
        self.assertEqual(call["data"]["publication_status"], "published")
        self.assertEqual(call["data"]["storage_reference"], meta["storage_reference"])

    def test_get_publication_and_list_keep_backend_reference_internal(self):
        transport = FakeTransport()
        meta = _metadata()
        transport.push(200, [{**meta, "publication_status": "published",
                              "created_at": "2026-01-01T00:00:00Z",
                              "updated_at": "2026-01-01T00:00:00Z"}])
        repo = SupabasePublicationMetadataRepository(CONFIG, access_token="server-token", transport=transport)
        row = repo.get_publication(meta["publication_id"])
        self.assertEqual(row.storage_reference, meta["storage_reference"])
        self.assertNotIn("storage_reference", row.public())

        transport.push(200, [{**meta, "publication_status": "published"}])
        rows = repo.list_publications(owner_user_id=meta["owner_user_id"])
        self.assertEqual(len(rows), 1)
        self.assertIn("publication_status=eq.published", transport.calls[-1]["url"])
        self.assertIn(f"owner_user_id=eq.{meta['owner_user_id']}", transport.calls[-1]["url"])

    def test_build_supabase_provider_fails_closed_without_backend_token(self):
        with self.assertRaisesRegex(PublicationMetadataError, "not_configured"):
            build_publication_metadata_repository({
                "provider": "supabase",
                "url": "https://proj.supabase.co",
                "publishable_key": "sb_publishable_pub",
            })

    def test_build_supabase_provider_does_not_touch_network(self):
        transport = FakeTransport()
        repo = build_publication_metadata_repository({
            "provider": "supabase",
            "url": "https://proj.supabase.co",
            "publishable_key": "sb_publishable_pub",
            "backend_access_token": "server-token",
            "transport": transport,
        })
        self.assertIsInstance(repo, SupabasePublicationMetadataRepository)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
