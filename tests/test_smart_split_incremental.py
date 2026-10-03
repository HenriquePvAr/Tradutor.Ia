from hashlib import sha256

import pytest
from PIL import Image

from smart_split_incremental import IncrementalSmartSplitter
from smart_split_incremental import SourceOrderBuffer
from page_manifest import to_image_entry, to_ocr_job


def safe_finder(image, *, target_height, min_height, max_height, protected):
    if image.height >= target_height:
        return target_height, {"safe_band": True, "gutter": True, "reason": "fixture_safe"}
    return image.height, {"safe_band": False, "gutter": False}


def fixture(height=3, color=(30, 40, 50)):
    return Image.new("RGB", (4, height), color)


def test_push_waits_then_emits_finalized_prefix(tmp_path):
    splitter = IncrementalSmartSplitter(tmp_path, target_height=3, min_height=3,
                                        max_height=5, split_finder=safe_finder)
    assert splitter.push("s1", fixture()) == ()
    emitted = splitter.push("s2", fixture())
    assert len(emitted) == 1
    assert emitted[0].logical_page_index == 1
    assert dict(emitted[0].metadata)["height"] == "3"
    assert splitter.pending_height == 3


def test_finish_flushes_only_residual_and_is_idempotent(tmp_path):
    splitter = IncrementalSmartSplitter(tmp_path, target_height=3, min_height=3,
                                        max_height=5, split_finder=safe_finder)
    splitter.push("s1", fixture())
    assert splitter.finish()[0].logical_page_index == 1
    residual = splitter.finish()
    assert residual == ()
    assert len(list(tmp_path.glob("page_*.png"))) == 1


def test_emitted_bytes_never_mutate_after_later_push(tmp_path):
    splitter = IncrementalSmartSplitter(tmp_path, target_height=3, min_height=3,
                                        max_height=5, split_finder=safe_finder)
    first = splitter.push("s1", fixture())
    assert first == ()
    emitted = splitter.push("s2", fixture(height=3))
    assert emitted
    digest_before = sha256(open(emitted[0].path, "rb").read()).hexdigest()
    splitter.push("s3", fixture(color=(90, 90, 90)))
    digest_after = sha256(open(emitted[0].path, "rb").read()).hexdigest()
    assert digest_before == digest_after


def test_cancel_does_not_flush_residual(tmp_path):
    splitter = IncrementalSmartSplitter(tmp_path, target_height=3, min_height=3,
                                        max_height=5, split_finder=safe_finder)
    splitter.push("s1", fixture())
    splitter.cancel()
    assert splitter.finish() == ()
    assert not list(tmp_path.glob("page_*.png"))


def test_push_after_finish_is_explicit_error(tmp_path):
    splitter = IncrementalSmartSplitter(tmp_path, target_height=3, min_height=3,
                                        max_height=5, split_finder=safe_finder)
    splitter.finish()
    with pytest.raises(RuntimeError, match="already finished"):
        splitter.push("s1", fixture())


def test_source_provenance_survives_carry_over(tmp_path):
    splitter = IncrementalSmartSplitter(tmp_path, target_height=5, min_height=5,
                                        max_height=7, split_finder=safe_finder)
    emitted = splitter.push("first", fixture(height=4))
    assert emitted == ()
    emitted = splitter.push("second", fixture(height=4))
    assert emitted
    metadata = dict(emitted[0].metadata)
    assert "first:" in metadata["source_ranges"]
    assert "second:" in metadata["source_ranges"]


def test_reorder_buffer_releases_only_logical_order_and_is_bounded():
    gate = SourceOrderBuffer(max_items=2)
    assert gate.add(2, "s2", "two") == ()
    assert gate.add(1, "s1", "one") == (("s1", "one"), ("s2", "two"))
    assert gate.max_pending == 2
    with pytest.raises(ValueError):
        gate.add(1, "again", "duplicate")


def test_manifest_adapters_preserve_identity():
    # Adapter shape is tested with a representative immutable manifest item.
    from page_manifest import FinalPageManifestItem
    item = FinalPageManifestItem(1, "page-a", "source", 0, "page.png",
                                 (("height", "3"),))
    image_entry = to_image_entry(item)
    ocr_job = to_ocr_job(item)
    assert image_entry["index"] == 1
    assert image_entry["stable_page_id"] == "page-a"
    assert ocr_job == {"index": 1, "image_path": "page.png", "stable_page_id": "page-a"}
