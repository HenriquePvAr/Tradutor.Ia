import _test_bootstrap  # noqa: F401

import json
import threading
import unittest
from dataclasses import dataclass

from canonical_social_identity import (
    CanonicalSocialIdentityError,
    CanonicalSourceKeys,
    FakeCanonicalSocialIdentityMaterializer,
    SupabaseCanonicalSocialIdentityMaterializer,
    canonical_source_identity_from_publish_source,
)
from community_auth import RequestPrincipal
from supabase_social import SocialConfig


OWNER = RequestPrincipal(
    user_id="11111111-1111-4111-8111-111111111111",
    roles=frozenset({"user"}),
    authenticated=True,
)
OTHER = RequestPrincipal(
    user_id="22222222-2222-4222-8222-222222222222",
    roles=frozenset({"user"}),
    authenticated=True,
)


class MemoryJobStore:
    def __init__(self, rows):
        self.rows = rows

    def get_job(self, job_id):
        return self.rows.get(job_id)


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
        self.queue.append(_Resp(status, json.dumps(body).encode("utf-8"), {}))

    def request(self, method, url, *, headers=None, data=None, stream=False):
        self.calls.append({
            "method": method,
            "url": url,
            "headers": headers or {},
            "data": json.loads(data) if data else None,
        })
        return self.queue.pop(0) if self.queue else _Resp(200, b"{}", {})


def _canonical_job(job_id="source-job", run_id="source-run"):
    return {
        "id": job_id,
        "run_id": run_id,
        "series_title": "Synthetic Series",
        "configuration": {
            "job_type": "translation",
            "source_analysis": {
                "canonical_identity": {
                    "schema_version": 1,
                    "adapter_name": "webtoons",
                    "series_identifier": "9001",
                    "episode_identifier": "12",
                    "series_slug": "synthetic-series",
                    "episode_label": "episode-51",
                    "identity_hash": "b" * 64,
                }
            },
        },
    }


class CanonicalSocialIdentityTests(unittest.TestCase):
    def test_extracts_webtoons_source_keys_from_server_job(self):
        store = MemoryJobStore({"source-job": _canonical_job()})

        identity = canonical_source_identity_from_publish_source(
            {"source_job_id": "source-job"}, store
        )

        self.assertEqual(identity.source_provider, "webtoons")
        self.assertEqual(identity.source_work_key, "title_no=9001")
        self.assertEqual(identity.source_chapter_key, "title_no=9001:episode_no=12")
        self.assertEqual(identity.source_identity_hash, "b" * 64)
        self.assertTrue(identity.work_slug.startswith("synthetic-series-"))
        self.assertEqual(identity.chapter_number, 12.0)

    def test_missing_canonical_identity_fails_closed(self):
        store = MemoryJobStore({"source-job": {"id": "source-job", "configuration": {}}})

        with self.assertRaisesRegex(
            CanonicalSocialIdentityError,
            "canonical_source_identity_unavailable",
        ):
            canonical_source_identity_from_publish_source({"source_job_id": "source-job"}, store)

    def test_same_identity_replay_and_response_loss_recover_same_ids(self):
        materializer = FakeCanonicalSocialIdentityMaterializer()
        identity = canonical_source_identity_from_publish_source(
            {"source_job_id": "source-job"},
            MemoryJobStore({"source-job": _canonical_job()}),
        )

        first = materializer.resolve_or_materialize(OWNER, identity)
        second = materializer.resolve_or_materialize(OWNER, identity)

        self.assertEqual(first.work_id, second.work_id)
        self.assertEqual(first.chapter_id, second.chapter_id)
        self.assertTrue(first.work_created)
        self.assertFalse(second.work_created)

    def test_different_owner_is_isolated(self):
        materializer = FakeCanonicalSocialIdentityMaterializer()
        identity = canonical_source_identity_from_publish_source(
            {"source_job_id": "source-job"},
            MemoryJobStore({"source-job": _canonical_job()}),
        )

        owner = materializer.resolve_or_materialize(OWNER, identity)
        other = materializer.resolve_or_materialize(OTHER, identity)

        self.assertNotEqual(owner.work_id, other.work_id)
        self.assertNotEqual(owner.chapter_id, other.chapter_id)

    def test_same_work_different_chapter_reuses_work_only(self):
        materializer = FakeCanonicalSocialIdentityMaterializer()
        first = canonical_source_identity_from_publish_source(
            {"source_job_id": "a"},
            MemoryJobStore({"a": _canonical_job("a", "run-a")}),
        )
        altered = json.loads(json.dumps(_canonical_job("b", "run-b")))
        altered["configuration"]["source_analysis"]["canonical_identity"]["episode_identifier"] = "52"
        altered["configuration"]["source_analysis"]["canonical_identity"]["episode_label"] = "episode-52"
        altered["configuration"]["source_analysis"]["canonical_identity"]["identity_hash"] = "c" * 64
        second = canonical_source_identity_from_publish_source(
            {"source_job_id": "b"},
            MemoryJobStore({"b": altered}),
        )

        one = materializer.resolve_or_materialize(OWNER, first)
        two = materializer.resolve_or_materialize(OWNER, second)

        self.assertEqual(one.work_id, two.work_id)
        self.assertNotEqual(one.chapter_id, two.chapter_id)

    def test_same_chapter_identity_hash_mismatch_fails_closed(self):
        materializer = FakeCanonicalSocialIdentityMaterializer()
        first = canonical_source_identity_from_publish_source(
            {"source_job_id": "a"},
            MemoryJobStore({"a": _canonical_job("a", "run-a")}),
        )
        altered = json.loads(json.dumps(_canonical_job("b", "run-b")))
        altered["configuration"]["source_analysis"]["canonical_identity"]["identity_hash"] = "c" * 64
        second = canonical_source_identity_from_publish_source(
            {"source_job_id": "b"},
            MemoryJobStore({"b": altered}),
        )

        materializer.resolve_or_materialize(OWNER, first)

        with self.assertRaisesRegex(
            CanonicalSocialIdentityError,
            "canonical_social_identity_conflict",
        ):
            materializer.resolve_or_materialize(OWNER, second)

    def test_concurrent_fake_materialization_has_single_identity_pair(self):
        materializer = FakeCanonicalSocialIdentityMaterializer()
        identity = canonical_source_identity_from_publish_source(
            {"source_job_id": "source-job"},
            MemoryJobStore({"source-job": _canonical_job()}),
        )
        barrier = threading.Barrier(2)
        results = []

        def call():
            barrier.wait()
            results.append(materializer.resolve_or_materialize(OWNER, identity))

        threads = [threading.Thread(target=call), threading.Thread(target=call)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len({r.work_id for r in results}), 1)
        self.assertEqual(len({r.chapter_id for r in results}), 1)
        self.assertEqual(len(materializer.work_mappings), 1)
        self.assertEqual(len(materializer.chapter_mappings), 1)

    def test_supabase_materializer_calls_narrow_backend_rpc(self):
        transport = FakeTransport()
        transport.push(200, {
            "work_id": "11111111-1111-4111-8111-111111111111",
            "chapter_id": "22222222-2222-4222-8222-222222222222",
            "work_created": True,
            "chapter_created": True,
        })
        repo = SupabaseCanonicalSocialIdentityMaterializer(
            SocialConfig(url="https://example.invalid", publishable_key="server-side-rpc"),
            secret_key="sb_secret_synthetic",
            transport=transport,
        )
        identity = CanonicalSourceKeys(
            source_provider="webtoons",
            source_work_key="title_no=9001",
            source_chapter_key="title_no=9001:episode_no=12",
            source_identity_hash="b" * 64,
            identity_version=1,
            work_title="Synthetic",
            work_slug="synthetic-abc123",
            chapter_number=51,
            chapter_title="episode-51",
            source_job_id="source-job",
            source_run_id="source-run",
            reconstruction_job_id="recon-job",
            reconstruction_run_id="recon-run",
        )

        result = repo.resolve_or_materialize(OWNER, identity)

        self.assertEqual(result.chapter_id, "22222222-2222-4222-8222-222222222222")
        call = transport.calls[0]
        self.assertIn("/rest/v1/rpc/resolve_or_materialize_community_source_identity", call["url"])
        self.assertEqual(call["data"]["p_owner_id"], OWNER.user_id)
        self.assertEqual(call["data"]["p_source_work_key"], "title_no=9001")
        self.assertEqual(call["data"]["p_source_chapter_key"], "title_no=9001:episode_no=12")
        self.assertNotIn("chapter_id", call["data"])
        self.assertNotIn("work_id", call["data"])
        self.assertNotIn("sb_secret_synthetic", repr(call["headers"]))

    def test_supabase_materializer_maps_conflict_to_safe_error(self):
        transport = FakeTransport()
        transport.push(409, {"message": "duplicate key detail should not leak"})
        repo = SupabaseCanonicalSocialIdentityMaterializer(
            SocialConfig(url="https://example.invalid", publishable_key="server-side-rpc"),
            secret_key="sb_secret_synthetic",
            transport=transport,
        )
        identity = canonical_source_identity_from_publish_source(
            {"source_job_id": "source-job"},
            MemoryJobStore({"source-job": _canonical_job()}),
        )

        with self.assertRaisesRegex(
            CanonicalSocialIdentityError,
            "canonical_social_identity_conflict",
        ):
            repo.resolve_or_materialize(OWNER, identity)


if __name__ == "__main__":
    unittest.main()
