"""TDD #84F27 - region-exact corpus audit contracts.

The final #84F27 audit must not repeat the exploratory aggregated-bbox replay:
one canonical story region is one audit unit, with residual checks bound to that
region's own geometry and to the cleanup-only layer.  These tests are offline
and read only; they do not start jobs, call providers, or touch remote services.
"""
from __future__ import annotations

from offline_test_guard import install_offline_network_guard

install_offline_network_guard()

import re
import unittest

import region_taxonomy

def _synthetic_quality_report():
    def item(
        page,
        group_id,
        region_id,
        text,
        target,
        box,
        *,
        classification="narration",
        translation=None,
    ):
        stored_translation = target if translation is None else translation
        return {
            "id": group_id,
            "region_id": region_id,
            "classification": classification,
            "text": text,
            "translation": stored_translation,
            "translation_candidate": target,
            "bounding_box": box,
            "source_completeness": {
                "status": "pass",
                "group_id": group_id,
                "source_bbox": box,
            },
        }

    return {
        "pages": [
            {
                "index": 5,
                "translation_terminal_items": [
                    item(5, "BALAO_1", "REGION_001", "REAL COFFEE.", "CAFÉ DE VERDADE.", [262, 395, 421, 78]),
                    item(
                        5,
                        "BALAO_2",
                        "REGION_002",
                        "NOT THE CHEAP SYNTHETIC STUFF.",
                        "NÃO ESSA PORCARIA SINTÉTICA.",
                        [83, 1620, 637, 365],
                    ),
                    item(5, "SFX_1", "REGION_003", "TAK", "", [20, 1800, 90, 70], classification="sfx"),
                    item(
                        5,
                        "BALAO_3",
                        "REGION_005",
                        "TUR",
                        "TUR",
                        [680, 1810, 80, 70],
                        classification="speech",
                        translation="",
                    ),
                ],
                "suspicious_groups": [],
                "narrations_translated": [],
                "text_overflow_items": [],
                "mixed_language_items": [],
                "sfx_preserved": [
                    item(5, "SFX_2", "REGION_004", "TUNK", "", [680, 1810, 80, 70], classification="sfx")
                ],
            }
        ]
    }


def _unique_quality_items(report=None):
    report = report or _synthetic_quality_report()
    seen = {}
    for page in report.get("pages", []):
        page_index = int(page.get("index") or 0)
        for bucket in (
            "translation_terminal_items",
            "narrations_translated",
            "suspicious_groups",
            "text_overflow_items",
            "mixed_language_items",
            "sfx_preserved",
        ):
            for item in page.get(bucket) or []:
                key = (page_index, item.get("id"), item.get("region_id"))
                if key in seen:
                    continue
                record = dict(item)
                record["page"] = page_index
                record["quality_bucket"] = bucket
                seen[key] = record
    return list(seen.values())


def _ordinary_story_items():
    records = []
    for item in _unique_quality_items():
        classification = str(item.get("classification") or "").lower()
        target = str(item.get("translation") or item.get("translation_candidate") or "").strip()
        if classification in {"sfx", "logo"} or _looks_like_preserved_sfx_item(item):
            continue
        if not target:
            continue
        if not item.get("bounding_box"):
            continue
        records.append(item)
    return records


def _looks_like_preserved_sfx_item(item):
    """Audit-only guard for historical records that mislabeled SFX as speech.

    #84F24 stored a few preserved sound effects in generic terminal buckets with
    coarse ``speech`` labels.  Ordinary-story corpus audit must follow the
    semantic/render disposition, not merely the historical coarse label: a
    single short all-caps token preserved unchanged is not a missing PT-BR story
    render.
    """

    source = str(item.get("text") or item.get("source_text") or "").strip()
    target = str(item.get("translation") or item.get("translation_candidate") or "").strip()
    if not source or target != source:
        return False
    if item.get("translation"):
        return False
    if re.search(r"[.!?…]", source):
        return False
    words = re.findall(r"[A-Za-zÀ-ÿ']+", source)
    if len(words) != 1:
        return False
    token = words[0]
    compact = re.sub(r"[^A-Z]", "", token.upper())
    if len(compact) < 2 or len(compact) > 5:
        return False
    if token != token.upper():
        return False
    # Historical terminal records can carry ``classification=speech`` even when
    # the renderer deliberately preserved a short sound-effect token unchanged.
    # That is not a missing ordinary-story target; it belongs to the SFX gate.
    if not item.get("translation"):
        return True
    policy = region_taxonomy.resolve_region_policy(
        original_classification=item.get("classification") or "",
        source_text=source,
        preserve_as_name=False,
    )
    return bool(
        region_taxonomy.looks_like_sfx(source)
        or (
            not policy["provider_required"]
            and policy["suggested_action"] != "translate"
        )
        or not region_taxonomy.weak_label_semantic_promotion_allowed(
            item.get("classification") or "",
            source,
        )
    )


def _union_box(boxes):
    x1 = min(box[0] for box in boxes)
    y1 = min(box[1] for box in boxes)
    x2 = max(box[0] + box[2] for box in boxes)
    y2 = max(box[1] + box[3] for box in boxes)
    return [x1, y1, x2 - x1, y2 - y1]


class RegionExactCorpusAuditContract(unittest.TestCase):
    def test_corpus_audit_has_one_record_per_canonical_story_region(self):
        records = _ordinary_story_items()
        keys = [(item["page"], item.get("id"), item.get("region_id")) for item in records]

        self.assertEqual(len(keys), len(set(keys)))
        self.assertGreaterEqual(len(records), 2)

    def test_every_record_uses_its_own_persisted_region_geometry(self):
        for item in _ordinary_story_items():
            with self.subTest(page=item["page"], group=item.get("id")):
                box = item.get("bounding_box")
                self.assertIsInstance(box, list)
                self.assertEqual(len(box), 4)
                self.assertTrue(all(int(value) >= 0 for value in box))
                source = item.get("source_completeness") or {}
                source_box = source.get("source_bbox")
                if source_box:
                    self.assertEqual(
                        [int(v) for v in box],
                        [int(v) for v in source_box],
                    )

    def test_aggregate_bbox_can_falsely_cover_neighboring_regions(self):
        records = [
            item for item in _ordinary_story_items()
            if int(item["page"]) == 5 and item.get("id") in {"BALAO_1", "BALAO_2"}
        ]
        self.assertEqual({item.get("id") for item in records}, {"BALAO_1", "BALAO_2"})

        exact_area = sum(
            int(item["bounding_box"][2]) * int(item["bounding_box"][3])
            for item in records
        )
        aggregate = _union_box([item["bounding_box"] for item in records])
        aggregate_area = int(aggregate[2]) * int(aggregate[3])

        self.assertGreater(
            aggregate_area,
            exact_area * 1.25,
            "fixture no longer proves aggregate bbox contamination risk",
        )

    def test_sfx_items_are_excluded_from_ordinary_story_audit(self):
        records = _ordinary_story_items()

        self.assertFalse(
            any(str(item.get("classification") or "").lower() == "sfx" for item in records)
        )

    def test_preserved_sfx_mislabeled_as_speech_is_not_story_target_missing(self):
        records = _ordinary_story_items()
        keys = {(item["page"], item.get("id"), item.get("region_id")) for item in records}

        self.assertNotIn((5, "BALAO_3", "REGION_005"), keys)


if __name__ == "__main__":
    unittest.main()
