import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from metric_council import (  # noqa: E402
    DomainMismatchError,
    IllegalRevisionError,
    InconsistentRecordError,
    PayloadShapeError,
    SchemaVersionError,
    UnknownFieldError,
    load_proposal,
    load_record,
    parse_proposal,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "metric_proposal.json"


def valid_payload(**overrides):
    with FIXTURE.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    payload.update(overrides)
    return payload


class ContractTest(unittest.TestCase):
    def test_example_uses_current_contract(self):
        # 历史测试：样例仍能经旧入口读取，领域与修订合法。
        item = load_record(FIXTURE)
        self.assertEqual(item.domain, "metric_council")
        self.assertGreater(item.revision, 0)

    def test_fixture_passes_strict_contract(self):
        proposal = load_proposal(FIXTURE)
        self.assertEqual(proposal.domain, "metric_council")
        self.assertEqual(proposal.caliber_id, "cul_cultural_enterprise_operating_revenue")
        self.assertEqual(proposal.period_start, "2026-09")
        self.assertEqual(proposal.source_authorization.version, "1.0")

    def test_foreign_domain_is_rejected_even_with_valid_record_id(self):
        # 本次事故场景：record_id 合法，但 domain 写成其他业务。
        payload = valid_payload(domain="tourism_market")
        with self.assertRaises(DomainMismatchError) as ctx:
            parse_proposal(payload)
        self.assertEqual(ctx.exception.record_id, "sample-007")

    def test_non_object_payloads_rejected(self):
        for bad in ([], "text", 123, 4.5, True, None):
            with self.subTest(bad=bad):
                with self.assertRaises(PayloadShapeError):
                    parse_proposal(bad)

    def test_nested_structure_rejected_only_self_fields(self):
        payload = valid_payload(extra={"nested": 1})
        with self.assertRaises(PayloadShapeError):
            parse_proposal(payload)
        payload = valid_payload(items=[1, 2])
        with self.assertRaises(PayloadShapeError):
            parse_proposal(payload)

    def test_unknown_field_is_distinguishable(self):
        payload = valid_payload(unexpected_business_flag="x")
        with self.assertRaises(UnknownFieldError) as ctx:
            parse_proposal(payload)
        self.assertEqual(ctx.exception.unknown, ["unexpected_business_flag"])

    def test_illegal_revisions_distinguishable(self):
        for bad_rev in (0, -1, "1", 1.0, True):
            with self.subTest(bad_rev=bad_rev):
                with self.assertRaises(IllegalRevisionError):
                    parse_proposal(valid_payload(revision=bad_rev))

    def test_expected_revision_detects_rollback_and_replay(self):
        with self.assertRaises(IllegalRevisionError):
            parse_proposal(valid_payload(revision=1), expected_revision=2)  # 回退
        with self.assertRaises(IllegalRevisionError):
            parse_proposal(valid_payload(revision=3), expected_revision=2)  # 跳跃

    def test_caliber_prefix_must_match_domain(self):
        with self.assertRaises(DomainMismatchError):
            parse_proposal(valid_payload(caliber_id="tour_visitors"))

    def test_source_must_belong_to_cultural_domain(self):
        with self.assertRaises(DomainMismatchError):
            parse_proposal(valid_payload(source="海关进出口数据库"))

    def test_region_must_be_known_cn_region(self):
        with self.assertRaises(InconsistentRecordError):
            parse_proposal(valid_payload(region="CN-XX"))
        with self.assertRaises(InconsistentRecordError):
            parse_proposal(valid_payload(region="US-CA"))

    def test_period_and_tz_aware_time_must_align(self):
        # 同一瞬时在报送时区与 UTC 下归属同一统计周期：允许。
        utc_time = "2026-04-01T00:30:00+00:00"  # = 北京时间 08:30
        p = parse_proposal(valid_payload(occurred_at=utc_time, period="monthly", region="CN-BJ"))
        self.assertEqual(p.period_start, "2026-04")
        p_q = parse_proposal(valid_payload(occurred_at=utc_time, period="quarterly", region="CN-BJ"))
        self.assertEqual(p_q.period_start, "2026-04")
        # 跨周期边界瞬时（UTC 仍是 3 月/Q1，北京已是 4 月/Q2）：周期与时间不一致，拒收。
        boundary = "2026-03-31T17:30:00+00:00"
        with self.assertRaises(InconsistentRecordError):
            parse_proposal(valid_payload(occurred_at=boundary, period="monthly"))
        with self.assertRaises(InconsistentRecordError):
            parse_proposal(valid_payload(occurred_at=boundary, period="quarterly"))
        # 该瞬时仍在同一自然年，年度周期不受影响。
        p_year = parse_proposal(valid_payload(occurred_at=boundary, period="annual"))
        self.assertEqual(p_year.period_start, "2026-01")
        # 跨年边界（UTC 仍是 2026 年，北京已是 2027 年）：年度也拒收。
        year_boundary = "2026-12-31T17:30:00+00:00"
        with self.assertRaises(InconsistentRecordError):
            parse_proposal(valid_payload(occurred_at=year_boundary, period="annual"))

    def test_naive_datetime_rejected(self):
        with self.assertRaises(InconsistentRecordError):
            parse_proposal(valid_payload(occurred_at="2026-09-20T09:00:00"))

    def test_price_basis_requires_base_year_for_constant(self):
        with self.assertRaises(InconsistentRecordError):
            parse_proposal(valid_payload(price_basis="constant"))
        with self.assertRaises(InconsistentRecordError):
            parse_proposal(
                valid_payload(price_basis="nominal", constant_base_year=2020)
            )
        p = parse_proposal(
            valid_payload(price_basis="constant", constant_base_year=2020)
        )
        self.assertEqual(p.constant_base_year, 2020)

    def test_source_version_and_authorization_version_format(self):
        with self.assertRaises(InconsistentRecordError):
            parse_proposal(valid_payload(source_version="v1"))
        with self.assertRaises(InconsistentRecordError):
            parse_proposal(
                valid_payload(source_authorization="财务司/文号@latest")
            )

    def test_unsupported_schema_version(self):
        with self.assertRaises(SchemaVersionError):
            parse_proposal(valid_payload(schema_version=99))

    def test_broken_json_file_is_shape_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{不是 json", encoding="utf-8")
            with self.assertRaises(PayloadShapeError):
                load_proposal(path)


if __name__ == "__main__":
    unittest.main()
