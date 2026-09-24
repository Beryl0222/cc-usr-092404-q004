import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from metric_council import (
    APPROVAL_CHAIN,
    CouncilError,
    CouncilService,
    ProposalEntry,
    ProposalRejected,
)

ENTRY = ProposalEntry()

MATERIAL_A = {
    "definition": "文化产业增加值（现价，法人单位口径）",
    "denominator": "规模以上文化法人单位数",
    "price_basis": "current_price",
    "source_authorization": "授权书-2026-001",
}

MATERIAL_B = dict(MATERIAL_A, definition="文化产业增加值（现价，含个体经营户）")


def make_proposal(record_id, department, material, revision=1, **overrides):
    payload = {
        "schema_version": 1,
        "record_id": record_id,
        "domain": "metric_council",
        "caliber_id": "culture.value_added",
        "region": "110000",
        "period": "2025",
        "occurred_at": "2026-09-20T09:00:00+08:00",
        "source": "yearbook",
        "source_version": "yb2025",
        "revision": revision,
        "department": department,
        "material": material,
    }
    payload.update(overrides)
    outcome = ENTRY.parse(payload)
    assert outcome.ok, outcome.problems
    return outcome.proposal


def proposal_kinds(exc):
    return {p.kind for p in exc.problems}


class IntakeTest(unittest.TestCase):
    def setUp(self):
        self.service = CouncilService()

    def test_identical_material_merges_into_one_receipt(self):
        first = self.service.submit(make_proposal("cul-0001", "文化统计处", MATERIAL_A))
        second = self.service.submit(make_proposal("ind-0001", "产业司", MATERIAL_A))
        self.assertFalse(first.merged)
        self.assertTrue(second.merged)
        self.assertEqual(first.receipt_id, second.receipt_id)
        receipt = self.service.receipt(first.receipt_id)
        self.assertEqual(receipt["departments"], ["文化统计处", "产业司"])
        self.assertEqual(self.service.counts()["receipts"], 1)

    def test_conflict_keeps_versions_and_opens_single_session(self):
        self.service.submit(make_proposal("cul-0001", "文化统计处", MATERIAL_A))
        result = self.service.submit(make_proposal("dev-0001", "发展规划司", MATERIAL_B))
        self.assertEqual(result.contested_aspects, ("definition",))
        self.assertEqual(len(result.sessions_started), 1)

        versions = self.service.aspect_versions("culture.value_added", "definition")
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0]["departments"], ["文化统计处"])
        self.assertEqual(versions[1]["departments"], ["发展规划司"])

        # 第三方重复提交冲突材料：归并到既有版本，不重复发起会审
        again = self.service.submit(make_proposal("ind-0001", "产业司", MATERIAL_B))
        self.assertEqual(again.sessions_started, ())
        self.assertEqual(len(self.service.open_sessions()), 1)
        self.assertEqual(len(self.service.aspect_versions("culture.value_added", "definition")), 2)

    def test_aspects_are_versioned_independently(self):
        self.service.submit(make_proposal("cul-0001", "文化统计处", MATERIAL_A))
        changed = dict(MATERIAL_A, denominator="全部文化法人单位数")
        self.service.submit(make_proposal("dev-0001", "发展规划司", changed))

        self.assertEqual(
            len(self.service.aspect_versions("culture.value_added", "denominator")), 2
        )
        for aspect in ("definition", "price_basis", "source_authorization"):
            self.assertEqual(
                len(self.service.aspect_versions("culture.value_added", aspect)),
                1,
                f"{aspect} 不应产生新版本",
            )

    def test_same_revision_with_different_material_is_invalid(self):
        self.service.submit(make_proposal("cul-0001", "文化统计处", MATERIAL_A))
        with self.assertRaises(ProposalRejected) as ctx:
            self.service.submit(make_proposal("cul-0001", "文化统计处", MATERIAL_B))
        self.assertEqual(proposal_kinds(ctx.exception), {"invalid_revision"})

    def test_stale_and_gapped_revisions_are_invalid(self):
        self.service.submit(make_proposal("cul-0001", "文化统计处", MATERIAL_A))
        self.service.submit(
            make_proposal("cul-0001", "文化统计处", MATERIAL_B, revision=2)
        )
        with self.assertRaises(ProposalRejected) as stale:
            self.service.submit(
                make_proposal("cul-0001", "文化统计处", MATERIAL_A, revision=1)
            )
        self.assertEqual(proposal_kinds(stale.exception), {"invalid_revision"})
        with self.assertRaises(ProposalRejected) as gap:
            self.service.submit(
                make_proposal("cul-0001", "文化统计处", MATERIAL_A, revision=9)
            )
        self.assertEqual(proposal_kinds(gap.exception), {"invalid_revision"})

    def test_first_submission_must_start_at_revision_one(self):
        with self.assertRaises(ProposalRejected) as ctx:
            self.service.submit(make_proposal("cul-0001", "文化统计处", MATERIAL_A, revision=3))
        self.assertEqual(proposal_kinds(ctx.exception), {"invalid_revision"})

    def test_submit_rechecks_domain_boundary(self):
        proposal = make_proposal("cul-0001", "文化统计处", MATERIAL_A)
        forged = type(proposal)(
            **{**proposal.__dict__, "domain": "agri_census"}
        )
        with self.assertRaises(ProposalRejected) as ctx:
            self.service.submit(forged)
        self.assertEqual(proposal_kinds(ctx.exception), {"wrong_domain"})


class ReviewAndBackcalcTest(unittest.TestCase):
    def setUp(self):
        self.service = CouncilService()
        self.service.submit(make_proposal("cul-0001", "文化统计处", MATERIAL_A))
        self.service.submit(make_proposal("dev-0001", "发展规划司", MATERIAL_B))

    def _settle_and_approve(self):
        (session,) = self.service.open_sessions("culture.value_added")
        self.service.rule(session["session_id"], chosen_version=1, decided_by="审议委员会")
        return self.service.approve_caliber("culture.value_added", approver="综合处")

    def test_caliber_cannot_be_approved_with_open_session(self):
        with self.assertRaises(CouncilError) as ctx:
            self.service.approve_caliber("culture.value_added", approver="综合处")
        self.assertEqual(ctx.exception.kind, "caliber_not_eligible")

    def test_backcalc_requires_approved_caliber(self):
        with self.assertRaises(CouncilError) as ctx:
            self.service.request_backcalc("dec-不存在", "2025")
        self.assertEqual(ctx.exception.kind, "caliber_not_approved")

    def test_ruling_then_approval_unlocks_backcalc(self):
        decision = self._settle_and_approve()
        self.assertEqual(decision["status"], "approved")
        self.assertEqual(
            decision["aspect_versions"],
            {
                "definition": 1,
                "denominator": 1,
                "price_basis": 1,
                "source_authorization": 1,
            },
        )
        run = self.service.request_backcalc(decision["decision_id"], "2025")
        self.assertEqual(run["status"], "pending_approval")

    def test_backcalc_keeps_parallel_results_and_history_intact(self):
        decision = self._settle_and_approve()
        self.service.seed_series("culture.value_added", "110000", "2024", 100.0)
        self.service.seed_series("culture.value_added", "110000", "2025", 110.0)
        self.service.seed_series("culture.value_added", "310000", "2025", 220.0)

        run = self.service.request_backcalc(
            decision["decision_id"],
            "2025",
            reviser=lambda region, period, old: round(old * 1.1, 2),
        )
        # 影响范围只覆盖生效期之后的单元格
        self.assertEqual(
            {(c["region"], c["period"]) for c in run["impact"]},
            {("110000", "2025"), ("310000", "2025")},
        )
        # 新旧结果并行存放
        cell = next(c for c in run["impact"] if c["region"] == "110000")
        self.assertEqual(cell["published_value"], 110.0)
        self.assertEqual(cell["revised_value"], 121.0)
        # 审批链未完成前，已发布序列保持原值
        history = self.service.series_history("culture.value_added", "110000", "2025")
        self.assertEqual([h["value"] for h in history], [110.0])

        # 审批链必须按顺序
        with self.assertRaises(CouncilError) as ctx:
            self.service.approve_run(run["run_id"], APPROVAL_CHAIN[1], "审批人")
        self.assertEqual(ctx.exception.kind, "approval_out_of_order")
        with self.assertRaises(CouncilError):
            self.service.publish_run(run["run_id"])

        for step in APPROVAL_CHAIN:
            self.service.approve_run(run["run_id"], step, "审批人")
        self.assertEqual(self.service.run(run["run_id"])["status"], "approved")

        self.service.publish_run(run["run_id"])
        history = self.service.series_history("culture.value_added", "110000", "2025")
        # 旧值留在历史里，新值追加发布，未被直接改写
        self.assertEqual([h["value"] for h in history], [110.0, 121.0])
        self.assertEqual(history[0]["origin"], "baseline")
        self.assertEqual(history[1]["origin"], run["run_id"])
        # 生效期之前的序列不受影响
        older = self.service.series_history("culture.value_added", "110000", "2024")
        self.assertEqual([h["value"] for h in older], [100.0])


class RestartTest(unittest.TestCase):
    def test_replay_after_restart_has_no_duplicate_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CouncilService(state_dir=tmp)
            service.submit(make_proposal("cul-0001", "文化统计处", MATERIAL_A))
            service.submit(make_proposal("dev-0001", "发展规划司", MATERIAL_B))
            (session,) = service.open_sessions()
            service.rule(session["session_id"], 1, "审议委员会")
            decision = service.approve_caliber("culture.value_added", "综合处")
            service.seed_series("culture.value_added", "110000", "2025", 110.0)
            run = service.request_backcalc(decision["decision_id"], "2025")
            before = service.counts()

            # 重启：同一状态目录重建服务，重放相同提交
            restored = CouncilService(state_dir=tmp)
            first = restored.submit(make_proposal("cul-0001", "文化统计处", MATERIAL_A))
            second = restored.submit(make_proposal("dev-0001", "发展规划司", MATERIAL_B))
            self.assertTrue(first.merged)
            self.assertTrue(second.merged)
            self.assertEqual(first.sessions_started, ())
            self.assertEqual(second.sessions_started, ())

            # 重复请求同一回算任务返回既有任务，不重复发布
            replayed = restored.request_backcalc(decision["decision_id"], "2025")
            self.assertEqual(replayed["run_id"], run["run_id"])
            self.assertEqual(restored.counts(), before)


if __name__ == "__main__":
    unittest.main()
