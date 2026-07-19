"""Offline tests for generic reader profiles: hints only, never credentials or permission."""

import _test_bootstrap  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import chapter_source
from chapter_source import INCOMPLETE_DOWNLOAD, SUPPORTED_GENERIC_HIGH_CONFIDENCE, UniversalChapterAdapter
from source_profile import PROFILE_MAX_AGE_SECONDS, PROFILE_VERSION, SourceProfileStore, profile_from_analysis
from universal_chapter_adapter import (
    HIGH_CONFIDENCE, REVIEW_REQUIRED_MEDIUM_CONFIDENCE, analyse_candidates, cluster_evidence_id,
)


def public_dns(*_args, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def analysis():
    return {
        "adapter": "universal", "final_host": "reader.example.test",
        "outcome": SUPPORTED_GENERIC_HIGH_CONFIDENCE,
        "accepted": [{"id": "opaque-1"}, {"id": "opaque-2"}],
        "clusters": [{"key": cluster_evidence_id("container:chapter-reader"), "score": 0.9,
                      "signals": ["multiple_images"], "candidate_ids": ["opaque-1", "opaque-2"]}],
    }


def selection(*ids):
    return {"automatic": True, "candidate_ids": list(ids or ("opaque-1", "opaque-2"))}


class SourceProfileTests(unittest.TestCase):
    def test_profile_contains_only_sanitized_evidence(self):
        profile = profile_from_analysis(analysis(), {"automatic": False,
                                                     "candidate_ids": ["opaque-1", "opaque-2"]})
        self.assertEqual(profile["profile_version"], PROFILE_VERSION)
        self.assertEqual(profile["host"], "reader.example.test")
        self.assertEqual(profile["selection_mode"], "manual")
        self.assertNotIn("opaque-1", json.dumps(profile))
        self.assertNotIn("http", json.dumps(profile).casefold())

    def test_profile_is_exact_host_versioned_and_invalid_profiles_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SourceProfileStore(Path(folder) / "profiles.json")
            stored = store.record_success(analysis(), selection())
            self.assertEqual(store.load("reader.example.test"), stored)
            self.assertIsNone(store.load("other.example.test"))
            bad = {"reader.example.test": {**stored, "profile_version": PROFILE_VERSION - 1}}
            store.path.write_text(json.dumps(bad), encoding="utf-8")
            self.assertIsNone(store.load("reader.example.test"))

    def test_expired_profile_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SourceProfileStore(Path(folder) / "profiles.json")
            stored = store.record_success(analysis(), selection())
            stored["validated_at"] -= PROFILE_MAX_AGE_SECONDS + 1
            store.path.write_text(json.dumps({stored["host"]: stored}), encoding="utf-8")
            self.assertIsNone(store.load("reader.example.test"))

    def test_matching_profile_annotates_fresh_cluster_without_promoting_score(self):
        raw = [
            {"url": f"https://cdn.example.test/chapter/{index}.webp", "source": "currentSrc",
             "order": index, "y": index * 1000, "width": 800, "height": 1200,
             "naturalWidth": 800, "naturalHeight": 1200, "container": "chapter-reader",
             "context": "reader chapter"}
            for index in (1, 2)
        ]
        profile = profile_from_analysis(analysis(), selection())
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            result = analyse_candidates(
                "https://reader.example.test/chapter/1", raw,
                adapter=UniversalChapterAdapter("https://reader.example.test/chapter/1"),
                profile=profile,
            )
        self.assertTrue(result.profile_used)
        self.assertTrue(result.accepted)
        self.assertIn("validated_profile_evidence", result.clusters[0].signals)
        self.assertEqual(result.outcome, REVIEW_REQUIRED_MEDIUM_CONFIDENCE)
        self.assertLess(result.confidence, HIGH_CONFIDENCE)

    def test_profile_never_authorizes_an_unobserved_resource(self):
        profile = profile_from_analysis(analysis(), selection())
        with mock.patch.object(chapter_source.socket, "getaddrinfo", public_dns):
            adapter = UniversalChapterAdapter("https://reader.example.test/chapter/1")
            adapter.validate_url("https://reader.example.test/chapter/1")
            with self.assertRaises(chapter_source.SourceError):
                adapter.validate_url("https://unobserved.example.test/page.webp")
        self.assertTrue(profile)

    def test_missing_or_stale_selection_cannot_create_a_profile(self):
        self.assertIsNone(profile_from_analysis(analysis()))
        self.assertIsNone(profile_from_analysis(
            analysis(), {"candidate_ids": ["not-observed"], "automatic": True}))

    def test_incomplete_analysis_cannot_create_a_profile_even_with_a_selection(self):
        incomplete = {**analysis(), "outcome": INCOMPLETE_DOWNLOAD}
        self.assertIsNone(profile_from_analysis(incomplete, selection()))


if __name__ == "__main__":
    unittest.main()
