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


CONFIG = SocialConfig(url="https://example.invalid", publishable_key="sb_publishable_pub")


class FakePublicationMetadataContractTests(unittest.TestCase):
    def test_public_view_never_exposes_storage_reference(self):
        repo = FakePublicationMetadataRepository()
        meta = _metadata()
        repo.reserve(meta)
        repo.record_storage(meta)
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
        repo.record_storage(published)
        repo.finalize(published)
        rows = repo.list_publications(owner_user_id=published["owner_user_id"])
        self.assertEqual([r.publication_id for r in rows], [published["publication_id"]])

    def test_finalize_requires_server_storage_reference(self):
        repo = FakePublicationMetadataRepository()
        meta = _metadata(storage_reference="")
        repo.reserve(meta)
        with self.assertRaisesRegex(PublicationMetadataError, "missing_storage_reference"):
            repo.record_storage(meta)

    def test_finalize_requires_recorded_storage_before_published(self):
        repo = FakePublicationMetadataRepository()
        meta = _metadata()
        repo.reserve(meta)
        with self.assertRaisesRegex(PublicationMetadataError, "invalid_publication_transition"):
            repo.finalize(meta)

    def test_storage_reference_is_immutable_and_idempotent(self):
        repo = FakePublicationMetadataRepository()
        meta = _metadata()
        repo.reserve(meta)
        repo.record_storage(meta)
        repo.record_storage(meta)
        changed = {**meta, "storage_reference": f"other-{uuid4()}"}
        with self.assertRaisesRegex(PublicationMetadataError, "storage_reference_conflict"):
            repo.record_storage(changed)
        repo.finalize(meta)
        repo.finalize(meta)
        with self.assertRaisesRegex(PublicationMetadataError, "storage_reference_conflict"):
            repo.record_storage(changed)


class SupabasePublicationMetadataRepositoryTests(unittest.TestCase):
    def test_reserve_calls_backend_rpc_without_storage_reference_or_bearer(self):
        transport = FakeTransport()
        meta = _metadata()
        transport.push(200, {"publication_id": meta["publication_id"], "publication_status": "reserved"})
        SupabasePublicationMetadataRepository(
            CONFIG, secret_key="sb_secret_server_token", transport=transport).reserve(meta)
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertIn("/rest/v1/rpc/reserve_community_publication_artifact", call["url"])
        self.assertEqual(call["headers"]["apikey"], "sb_secret_server_token")
        self.assertNotIn("Authorization", call["headers"])
        self.assertNotIn("publication_status", call["data"])
        self.assertNotIn("storage_reference", call["data"])

    def test_record_storage_and_finalize_use_narrow_rpcs(self):
        transport = FakeTransport()
        meta = _metadata()
        transport.push(200, {"publication_id": meta["publication_id"], "publication_status": "uploaded"})
        transport.push(200, {"publication_id": meta["publication_id"], "publication_status": "published"})
        repo = SupabasePublicationMetadataRepository(
            CONFIG, secret_key="sb_secret_server_token", transport=transport)
        repo.record_storage(meta)
        repo.finalize(meta)
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertIn("/rest/v1/rpc/record_community_publication_storage", call["url"])
        self.assertEqual(call["data"]["p_storage_reference"], meta["storage_reference"])
        self.assertNotIn("publication_status", call["data"])
        self.assertIn("/rest/v1/rpc/finalize_community_publication_artifact", transport.calls[1]["url"])
        self.assertNotIn("storage_reference", transport.calls[1]["data"])

    def test_backend_rpc_response_keeps_storage_reference_internal_when_returned(self):
        transport = FakeTransport()
        meta = _metadata()
        transport.push(200, [{**meta, "publication_status": "published",
                              "created_at": "2026-01-01T00:00:00Z",
                              "updated_at": "2026-01-01T00:00:00Z"}])
        repo = SupabasePublicationMetadataRepository(CONFIG, secret_key="sb_secret_server_token", transport=transport)
        row = repo._metadata_from_rpc_result([{**meta, "publication_status": "published"}])
        self.assertEqual(row.storage_reference, meta["storage_reference"])
        self.assertNotIn("storage_reference", row.public())

    def test_build_supabase_provider_fails_closed_without_backend_token(self):
        with self.assertRaisesRegex(PublicationMetadataError, "not_configured"):
            build_publication_metadata_repository({
                "provider": "supabase",
                "url": "https://example.invalid",
            })

    def test_build_supabase_provider_does_not_touch_network(self):
        transport = FakeTransport()
        repo = build_publication_metadata_repository({
            "provider": "supabase",
            "url": "https://example.invalid",
            "secret_key": "sb_secret_server_token",
            "transport": transport,
        })
        self.assertIsInstance(repo, SupabasePublicationMetadataRepository)
        self.assertEqual(transport.calls, [])

    def test_repository_error_does_not_leak_secret_value(self):
        transport = FakeTransport()
        transport.push(403, {"message": "permission denied"})
        secret = "sb_secret_do_not_leak"
        repo = SupabasePublicationMetadataRepository(CONFIG, secret_key=secret, transport=transport)
        with self.assertRaises(PublicationMetadataError) as caught:
            repo.reserve(_metadata())
        self.assertNotIn(secret, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
