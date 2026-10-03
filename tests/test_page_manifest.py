import unittest

from page_manifest import manifest_snapshot, materialize_final_manifest


class FinalPageManifestTests(unittest.TestCase):
    def entries(self):
        return [
            {"index": 1, "path": "p1.png", "candidate_id": "source-a", "split_index": 0, "width": 480},
            {"index": 2, "path": "p2.png", "candidate_id": "source-b", "split_index": 0, "width": 480},
            {"index": 3, "path": "p3.png", "candidate_id": "source-c", "split_index": 1, "width": 480},
        ]

    def test_canonical_count_and_identity(self):
        manifest = materialize_final_manifest(self.entries())
        self.assertEqual(len(manifest), 3)
        self.assertEqual([item.logical_page_index for item in manifest], [1, 2, 3])
        self.assertEqual(len({item.stable_page_id for item in manifest}), 3)

    def test_completion_order_does_not_change_manifest(self):
        original = manifest_snapshot(self.entries())
        reordered = manifest_snapshot([self.entries()[2], self.entries()[0], self.entries()[1]])
        self.assertEqual(original, reordered)

    def test_split_child_identity_is_stable(self):
        a = manifest_snapshot(self.entries())
        b = manifest_snapshot(self.entries())
        self.assertEqual(a[2]["stable_page_id"], b[2]["stable_page_id"])
        self.assertEqual(a[2]["split_index"], 1)

    def test_discarded_entry_is_not_emitted(self):
        entries = [item for item in self.entries() if item["index"] != 2]
        manifest = materialize_final_manifest(entries)
        self.assertEqual([item.logical_page_index for item in manifest], [1, 3])

    def test_missing_path_is_fail_closed(self):
        self.assertEqual(materialize_final_manifest([{"index": 1}]), ())

    def test_source_mapping_is_preserved(self):
        manifest = materialize_final_manifest(self.entries())
        self.assertEqual([item.source_item_id for item in manifest], ["source-a", "source-b", "source-c"])

    def test_metadata_is_value_only(self):
        snapshot = manifest_snapshot(self.entries())
        self.assertEqual(snapshot[0]["metadata"], {"width": "480"})

    def test_multi_source_ranges_prevent_primary_source_collision(self):
        entries = [
            {"index": 1, "path": "a.png", "candidate_id": "source-a", "split_index": 0,
             "source_ranges": ["source-a:0-10", "source-b:0-4"]},
            {"index": 2, "path": "b.png", "candidate_id": "source-a", "split_index": 0,
             "source_ranges": ["source-a:0-10", "source-c:0-4"]},
        ]
        manifest = materialize_final_manifest(entries)
        self.assertEqual(len({item.stable_page_id for item in manifest}), 2)
        self.assertNotEqual(manifest[0].source_ranges, manifest[1].source_ranges)

    def test_same_primary_and_ranges_is_reconstructible(self):
        entry = {"index": 1, "path": "a.png", "candidate_id": "source-a", "split_index": 0,
                 "source_ranges": ["source-a:0-10", "source-b:0-4"]}
        self.assertEqual(
            manifest_snapshot([entry])[0]["stable_page_id"],
            manifest_snapshot([dict(entry)])[0]["stable_page_id"],
        )


if __name__ == "__main__":
    unittest.main()
