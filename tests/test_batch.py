import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from metric_council import CouncilService, ProposalRejected, process_batch

FIXTURES = Path(__file__).parents[1] / "fixtures"


def item_kinds(outcome):
    return {p.kind for p in outcome.problems}


class MixedBatchTest(unittest.TestCase):
    def setUp(self):
        self.service = CouncilService()
        self.report = process_batch(
            FIXTURES / "batches" / "mixed_batch.json", self.service
        )

    def test_every_item_gets_an_outcome(self):
        self.assertEqual(len(self.report.outcomes), 7)
        self.assertEqual(self.report.batch_id, "batch-2026-09-001")

    def test_partial_failures_do_not_block_others(self):
        accepted = self.report.accepted
        rejected = self.report.rejected
        # 三件合法提案全部受理，四件问题件各自驳回
        self.assertEqual(len(accepted), 3)
        self.assertEqual(len(rejected), 4)
        self.assertEqual([o.index for o in accepted], [0, 4, 5])
        self.assertEqual([o.index for o in rejected], [1, 2, 3, 6])

    def test_rejection_kinds_are_distinguishable(self):
        by_index = {o.index: o for o in self.report.outcomes}
        self.assertEqual(item_kinds(by_index[1]), {"wrong_domain"})
        self.assertEqual(item_kinds(by_index[2]), {"unknown_field"})
        self.assertEqual(item_kinds(by_index[3]), {"invalid_revision"})
        self.assertEqual(item_kinds(by_index[6]), {"naive_time"})

    def test_identical_material_merged_across_departments(self):
        by_index = {o.index: o for o in self.report.outcomes}
        self.assertFalse(by_index[0].merged)
        self.assertTrue(by_index[4].merged)
        self.assertEqual(by_index[0].receipt_id, by_index[4].receipt_id)
        self.assertEqual(self.service.counts()["receipts"], 2)

    def test_conflicting_material_opens_one_review_session(self):
        sessions = self.service.open_sessions("culture.value_added")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["aspect"], "definition")
        self.assertEqual(sessions[0]["version_ids"], [1, 2])


class BatchReplayTest(unittest.TestCase):
    def test_replay_after_restart_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CouncilService(state_dir=tmp)
            first = process_batch(FIXTURES / "batches" / "mixed_batch.json", service)
            before = service.counts()

            restored = CouncilService(state_dir=tmp)
            second = process_batch(FIXTURES / "batches" / "mixed_batch.json", restored)

            self.assertEqual(restored.counts(), before)
            self.assertEqual(
                [o.receipt_id for o in first.accepted],
                [o.receipt_id for o in second.accepted],
            )
            # 重放时合法条目全部识别为合并收件，不再发起新会审
            self.assertTrue(all(o.merged for o in second.accepted))
            self.assertEqual(len(restored.open_sessions()), 1)


class BatchEnvelopeTest(unittest.TestCase):
    def setUp(self):
        self.service = CouncilService()

    def test_bare_array_is_accepted(self):
        outcome = process_batch(
            [
                {
                    "schema_version": 1,
                    "record_id": "cul-0001",
                    "domain": "metric_council",
                    "caliber_id": "culture.value_added",
                    "region": "110000",
                    "period": "2025",
                    "occurred_at": "2026-09-20T09:00:00+08:00",
                    "source": "yearbook",
                    "source_version": "yb2025",
                    "revision": 1,
                    "department": "文化统计处",
                    "material": {
                        "definition": "文化产业增加值（现价，法人单位口径）",
                        "denominator": "规模以上文化法人单位数",
                        "price_basis": "current_price",
                        "source_authorization": "授权书-2026-001",
                    },
                }
            ],
            self.service,
        )
        self.assertEqual(len(outcome.accepted), 1)

    def test_envelope_unknown_field_rejected(self):
        with self.assertRaises(ProposalRejected) as ctx:
            process_batch({"items": [], "extra": 1}, self.service)
        self.assertEqual(
            {p.kind for p in ctx.exception.problems}, {"unknown_field"}
        )

    def test_envelope_must_contain_items(self):
        with self.assertRaises(ProposalRejected) as ctx:
            process_batch({"batch_id": "b-1"}, self.service)
        self.assertEqual(
            {p.kind for p in ctx.exception.problems}, {"missing_field"}
        )

    def test_scalar_batch_rejected(self):
        with self.assertRaises(ProposalRejected) as ctx:
            process_batch(42, self.service)
        self.assertEqual(
            {p.kind for p in ctx.exception.problems}, {"not_plain_object"}
        )

    def test_duplicate_key_inside_item_only_fails_that_item(self):
        text = """[
          {"schema_version": 1, "record_id": "cul-0001", "record_id": "cul-0002",
           "domain": "metric_council", "caliber_id": "culture.value_added",
           "region": "110000", "period": "2025",
           "occurred_at": "2026-09-20T09:00:00+08:00",
           "source": "yearbook", "source_version": "yb2025", "revision": 1,
           "department": "文化统计处",
           "material": {"definition": "定义", "denominator": "分母",
                        "price_basis": "current_price",
                        "source_authorization": "授权书"}},
          {"schema_version": 1, "record_id": "cul-0009",
           "domain": "metric_council", "caliber_id": "culture.value_added",
           "region": "110000", "period": "2025",
           "occurred_at": "2026-09-20T09:00:00+08:00",
           "source": "yearbook", "source_version": "yb2025", "revision": 1,
           "department": "文化统计处",
           "material": {"definition": "定义", "denominator": "分母",
                        "price_basis": "current_price",
                        "source_authorization": "授权书"}}
        ]"""
        report = process_batch(text, self.service)
        self.assertEqual(item_kinds(report.outcomes[0]), {"duplicate_field"})
        self.assertEqual(report.outcomes[1].status, "accepted")


if __name__ == "__main__":
    unittest.main()
