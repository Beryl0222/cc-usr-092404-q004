import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from metric_council import ProposalEntry, ProposalRejected, load_proposal

FIXTURES = Path(__file__).parents[1] / "fixtures"


def valid_payload(**overrides):
    payload = {
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
    payload.update(overrides)
    return payload


def kinds(outcome):
    return {problem.kind for problem in outcome.problems}


class ProposalEntryTest(unittest.TestCase):
    def setUp(self):
        self.entry = ProposalEntry()

    def test_valid_fixture_loads(self):
        proposal = load_proposal(FIXTURES / "proposals" / "valid_annual.json")
        self.assertEqual(proposal.domain, "metric_council")
        self.assertEqual(proposal.caliber_id, "culture.value_added")
        self.assertEqual(proposal.material.price_basis, "current_price")

    def test_wrong_domain_fixture_is_rejected(self):
        # 复现的问题：record_id 合法、内容完整，但 domain 是其他业务
        with self.assertRaises(ProposalRejected) as ctx:
            load_proposal(FIXTURES / "proposals" / "wrong_domain.json")
        self.assertEqual({p.kind for p in ctx.exception.problems}, {"wrong_domain"})

    def test_unknown_domain_is_wrong_domain(self):
        outcome = self.entry.parse(valid_payload(domain="tourism_board"))
        self.assertFalse(outcome.ok)
        self.assertIn("wrong_domain", kinds(outcome))

    def test_non_object_payload(self):
        for payload in ([1, 2], "text", 42, None):
            outcome = self.entry.parse(payload)
            self.assertEqual(kinds(outcome), {"not_plain_object"})

    def test_unknown_field_is_distinguishable(self):
        payload = valid_payload()
        payload["comment"] = "合同之外"
        outcome = self.entry.parse(payload)
        self.assertIn("unknown_field", kinds(outcome))
        self.assertNotIn("wrong_domain", kinds(outcome))

    def test_unknown_material_aspect(self):
        payload = valid_payload()
        payload["material"]["footnote"] = "多余方面"
        outcome = self.entry.parse(payload)
        self.assertIn("unknown_field", kinds(outcome))

    def test_missing_field(self):
        payload = valid_payload()
        del payload["source_version"]
        outcome = self.entry.parse(payload)
        self.assertIn("missing_field", kinds(outcome))

    def test_duplicate_keys_rejected(self):
        text = json.dumps(valid_payload())
        text = text.replace(
            '"record_id": "cul-0001"',
            '"record_id": "cul-0001", "record_id": "cul-9999"',
            1,
        )
        outcome = self.entry.parse_text(text)
        self.assertEqual(kinds(outcome), {"duplicate_field"})

    def test_invalid_revision_variants(self):
        for bad in (0, -2, "1", True, 1.5):
            outcome = self.entry.parse(valid_payload(revision=bad))
            self.assertIn(
                "invalid_revision", kinds(outcome), f"revision={bad!r} 应判非法"
            )

    def test_naive_time_rejected(self):
        outcome = self.entry.parse(
            valid_payload(occurred_at="2026-09-20T09:00:00")
        )
        self.assertIn("naive_time", kinds(outcome))

    def test_occurred_before_period_end_is_inconsistent(self):
        outcome = self.entry.parse(
            valid_payload(occurred_at="2025-06-01T00:00:00+08:00")
        )
        self.assertIn("inconsistent", kinds(outcome))

    def test_period_must_match_frequency(self):
        # culture.value_added 是年度口径，季度周期与之矛盾
        outcome = self.entry.parse(valid_payload(period="2026-Q1"))
        self.assertIn("inconsistent", kinds(outcome))

    def test_region_out_of_scope(self):
        outcome = self.entry.parse(valid_payload(region="999999"))
        self.assertIn("inconsistent", kinds(outcome))

    def test_unregistered_source_version(self):
        outcome = self.entry.parse(valid_payload(source_version="yb1999"))
        self.assertIn("inconsistent", kinds(outcome))

    def test_unregistered_source(self):
        outcome = self.entry.parse(valid_payload(source="unknown_source"))
        self.assertIn("inconsistent", kinds(outcome))

    def test_caliber_from_other_domain_is_inconsistent(self):
        outcome = self.entry.parse(valid_payload(caliber_id="agri.output_value"))
        self.assertIn("inconsistent", kinds(outcome))

    def test_unknown_caliber(self):
        outcome = self.entry.parse(valid_payload(caliber_id="culture.nope"))
        self.assertIn("invalid_field", kinds(outcome))

    def test_material_must_be_plain_object(self):
        outcome = self.entry.parse(valid_payload(material=["不是对象"]))
        self.assertIn("not_plain_object", kinds(outcome))

    def test_unsupported_schema_version(self):
        outcome = self.entry.parse(valid_payload(schema_version=2))
        self.assertIn("unsupported_schema", kinds(outcome))

    def test_three_problem_kinds_are_distinguishable(self):
        # 未知字段、错误领域、非法修订同时出现时各自独立可辨
        payload = valid_payload(domain="agri_census", revision=0)
        payload["extra"] = True
        outcome = self.entry.parse(payload)
        self.assertEqual(
            kinds(outcome), {"unknown_field", "wrong_domain", "invalid_revision"}
        )


if __name__ == "__main__":
    unittest.main()
