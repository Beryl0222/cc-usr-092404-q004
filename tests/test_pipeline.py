import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from metric_council import (  # noqa: E402
    AmendmentStatus,
    CaliberState,
    IntakeOutcome,
    JsonStore,
    MetricCouncil,
    ReviewError,
    ReviewKind,
    parse_proposal,
)

CALIBER = "cul_library_collection"


def base_payload(**overrides):
    payload = {
        "schema_version": 2,
        "record_id": "rec-a",
        "domain": "metric_council",
        "occurred_at": "2026-03-10T09:00:00+08:00",
        "revision": 1,
        "source": "文化和旅游部统计快报",
        "source_version": "1.0",
        "caliber_id": CALIBER,
        "region": "CN",
        "period": "monthly",
        "title": "公共图书馆总藏量",
        "definition": "各级公共图书馆已编目文献馆藏合计（万册件）",
        "denominator": "年末公共图书馆机构数",
        "price_basis": "nominal",
        "source_authorization": "文化和旅游部公共服务司/公图函〔2026〕1号@1.0",
        "department": "dept_library",
    }
    payload.update(overrides)
    return payload


def proposal(**overrides):
    return parse_proposal(base_payload(**overrides))


class CouncilTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = JsonStore(Path(self.tmp.name) / "state.json")
        self.council = MetricCouncil(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    # -- 收件：合并 / 冲突 / 四方版本化 ------------------------------------

    def test_identical_material_from_two_departments_merges_one_receipt(self):
        r1 = self.council.intake(proposal(record_id="rec-a"))
        r2 = self.council.intake(
            proposal(record_id="rec-b", department="dept_culture_bureau")
        )
        self.assertIs(r1.outcome, IntakeOutcome.ACCEPTED)
        self.assertIs(r2.outcome, IntakeOutcome.MERGED)
        self.assertEqual(r1.receipt_id, r2.receipt_id)
        caliber = self.council.caliber(CALIBER)
        self.assertEqual(len(caliber["receipts"]), 1)
        receipt = self.store.get("receipts")[r1.receipt_id]
        self.assertEqual(receipt["submitters"], ["dept_culture_bureau", "dept_library"])
        self.assertEqual(receipt["record_ids"], ["rec-a", "rec-b"])

    def test_conflicting_materials_keep_both_versions_and_await_joint_review(self):
        self.council.intake(proposal(record_id="rec-a"))
        self.council.intake(
            proposal(record_id="rec-b", department="dept_research",
                     definition="各级公共图书馆馆藏合计，含未编目文献（万册件）")
        )
        caliber = self.council.caliber(CALIBER)
        self.assertEqual(caliber["state"], CaliberState.CONFLICT.value)
        defs = self.council.component_versions(CALIBER, "definition")
        self.assertEqual([d["version"] for d in defs], [1, 2])
        self.assertEqual({d["departments"][0] for d in defs}, {"dept_library", "dept_research"})
        # 其余三方面只有一个版本。
        for component in ("denominator", "price_basis", "authorization"):
            self.assertEqual(len(self.council.component_versions(CALIBER, component)), 1)

    def test_each_of_four_components_versions_independently(self):
        self.council.intake(proposal(record_id="rec-a"))
        self.council.intake(
            proposal(record_id="rec-b", department="dept_research",
                     denominator="年末公共图书馆实际在用馆舍数",
                     price_basis="constant", constant_base_year=2020,
                     source_authorization="国家统计局/统计制度〔2026〕2号@2.1")
        )
        caliber = self.council.caliber(CALIBER)
        self.assertEqual(len(caliber["components"]["definition"]), 1)
        self.assertEqual(len(caliber["components"]["denominator"]), 2)
        self.assertEqual(len(caliber["components"]["price_basis"]), 2)
        self.assertEqual(len(caliber["components"]["authorization"]), 2)

    def test_record_revision_chain_must_be_continuous(self):
        self.council.intake(proposal(record_id="rec-a", revision=1))
        bad = self.council.intake(proposal(record_id="rec-a", revision=3))
        self.assertEqual(bad.error_type, "IllegalRevisionError")
        good = self.council.intake(proposal(record_id="rec-a", revision=2))
        self.assertIs(good.outcome, IntakeOutcome.ACCEPTED)

    def test_same_record_revision_replay_is_idempotent_duplicate(self):
        first = self.council.intake(proposal(record_id="rec-a"))
        replay = self.council.intake(proposal(record_id="rec-a"))
        self.assertIs(replay.outcome, IntakeOutcome.DUPLICATE)
        self.assertEqual(replay.receipt_id, first.receipt_id)
        # 同一修订号换内容重放 → 非法修订，且不得产生第二条收件。
        tampered = self.council.intake(
            proposal(record_id="rec-a", title="被篡改的标题")
        )
        self.assertEqual(tampered.error_type, "IllegalRevisionError")
        self.assertEqual(len(self.council.caliber(CALIBER)["receipts"]), 1)

    # -- 审议门禁 ----------------------------------------------------------

    def test_review_gate_normal_approval(self):
        self.council.intake(proposal())
        session = self.council.convene(CALIBER)
        self.assertEqual(session["kind"], ReviewKind.NORMAL.value)
        # 重复发起返回同一会话，不会生成第二次审议。
        self.assertEqual(self.council.convene(CALIBER), session)
        self.council.decide(CALIBER, True)
        self.assertEqual(self.council.caliber(CALIBER)["state"], CaliberState.APPROVED.value)
        with self.assertRaises(ReviewError):
            self.council.convene(CALIBER)

    def test_cannot_recalculate_before_approval(self):
        self.council.intake(proposal())
        with self.assertRaises(ReviewError):
            self.council.publish_recalculation(CALIBER, 1)

    def test_conflict_requires_joint_review_with_explicit_choices(self):
        self.council.intake(proposal(record_id="rec-a"))
        self.council.intake(
            proposal(record_id="rec-b", department="dept_research",
                     definition="含未编目文献的全口径馆藏（万册件）")
        )
        session = self.council.convene(CALIBER)
        self.assertEqual(session["kind"], ReviewKind.JOINT.value)
        # 冲突存在时，不逐项裁决不得通过。
        with self.assertRaises(ReviewError):
            self.council.decide(CALIBER, True)
        with self.assertRaises(ReviewError):
            self.council.decide(CALIBER, True, chosen_versions={"definition": 9})
        self.council.decide(CALIBER, True, chosen_versions={"definition": 2})
        winning = self.council.caliber(CALIBER)["winning_versions"]
        self.assertEqual(winning["definition"], 2)
        self.assertEqual(winning["denominator"], 1)

    def test_decision_without_convene_is_rejected(self):
        self.council.intake(proposal())
        with self.assertRaises(ReviewError):
            self.council.decide(CALIBER, True)

    # -- 回算与历史序列 ----------------------------------------------------

    def _seed_published_series(self):
        self.council.intake(proposal())
        self.council.convene(CALIBER)
        self.council.decide(CALIBER, True)
        for period, value in (("2025-11", 100.0), ("2025-12", 101.0), ("2026-01", 102.0)):
            self.council.publish_series_point(CALIBER, "CN", "monthly", period, value)

    def test_published_series_is_append_only(self):
        self._seed_published_series()
        with self.assertRaises(Exception):
            self.council.publish_series_point(CALIBER, "CN", "monthly", "2025-11", 999.0)
        self.assertEqual(
            self.council.get_series(CALIBER, "CN", "monthly")[0]["value"], 100.0
        )

    def test_first_recalculation_after_gate_runs_once(self):
        self._seed_published_series()
        task1 = self.council.publish_recalculation(CALIBER, 1)
        task2 = self.council.publish_recalculation(CALIBER, 1)
        self.assertEqual(task1["task_id"], task2["task_id"])
        tasks = self.store.get("recalc_tasks")
        self.assertEqual(len(tasks), 1)
        self.assertTrue(task1["published_series_mutated"] is False)
        self.assertEqual(len(task1["old_results"]), 3)

    def test_amendment_workflow_impact_chain_and_parallel_results(self):
        self._seed_published_series()

        # 修订材料先进件（revision=2），再开立修订。
        intake = self.council.intake(
            proposal(record_id="rec-a", revision=2,
                     definition="全口径馆藏（含数字资源，折算万册件）",
                     occurred_at="2026-03-15T09:00:00+08:00")
        )
        self.assertIs(intake.outcome, IntakeOutcome.ACCEPTED)
        # 已通过口径收到修订材料，状态不被打回冲突/待审。
        self.assertEqual(self.council.caliber(CALIBER)["state"], CaliberState.APPROVED.value)

        amendment = self.council.open_amendment(
            proposal(record_id="rec-a", revision=2,
                     definition="全口径馆藏（含数字资源，折算万册件）",
                     occurred_at="2026-03-15T09:00:00+08:00"),
            effective_from="2025-12",
        )
        # 影响范围：>=2025-12 的两个已发布点，不含 2025-11。
        impacted_periods = {
            p for s in amendment["impact"]["affected_series"] for p in s["periods"]
        }
        self.assertEqual(impacted_periods, {"2025-12", "2026-01"})
        self.assertEqual(amendment["impact"]["affected_point_count"], 2)

        # 审批链：跳级签署被拒。
        with self.assertRaises(Exception):
            self.council.approve_amendment_stage(
                CALIBER, 2, "分管局领导审批", "某局长"
            )
        from metric_council import APPROVAL_CHAIN
        for stage in APPROVAL_CHAIN:
            self.council.approve_amendment_stage(CALIBER, 2, stage, f"签署人-{stage}")

        # 重复签署被拒。
        with self.assertRaises(Exception):
            self.council.approve_amendment_stage(CALIBER, 2, APPROVAL_CHAIN[0], "重复人")

        # 回算：新旧并行，旧序列保持原样。
        task = self.council.publish_recalculation(
            CALIBER, 2,
            calculator=lambda p, ctx: round((ctx["old"] or 0) * 1.1, 4),
        )
        self.assertEqual(len(task["new_results"]), 2)
        self.assertTrue(all(r["value"] > r_old["value"]
                            for r, r_old in zip(task["new_results"], task["old_results"])))
        self.assertTrue(all(r["candidate_revision"] == 2 for r in task["new_results"]))
        # 历史已发布序列未被改写。
        series = self.council.get_series(CALIBER, "CN", "monthly")
        self.assertEqual([p["value"] for p in series], [100.0, 101.0, 102.0])

        stored = self.council.amendment(CALIBER, 2)
        self.assertEqual(stored["status"], AmendmentStatus.RECALC_PUBLISHED.value)
        self.assertEqual(stored["recalc_task_id"], task["task_id"])

        # 回算任务幂等：再次发布返回同一任务，旧新快照不变。
        again = self.council.publish_recalculation(CALIBER, 2)
        self.assertEqual(again, task)

    def test_recalculation_blocked_until_chain_complete(self):
        self._seed_published_series()
        rev2 = proposal(record_id="rec-a", revision=2,
                        definition="修订后的定义",
                        occurred_at="2026-03-15T09:00:00+08:00")
        self.council.intake(rev2)
        self.council.open_amendment(rev2)
        with self.assertRaises(Exception):
            self.council.publish_recalculation(CALIBER, 2)

    # -- 批量隔离 ----------------------------------------------------------

    def test_batch_partial_failure_does_not_block_others(self):
        paths = []
        specs = [
            ("good1.json", base_payload(record_id="rec-ok-1")),
            ("foreign.json", base_payload(record_id="rec-x", domain="tourism_market")),
            ("unknown.json", base_payload(record_id="rec-y", rogue_field=1)),
            ("good2.json", base_payload(record_id="rec-ok-2",
                                        department="dept_culture_bureau")),
        ]
        for name, payload in specs:
            path = Path(self.tmp.name) / name
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            paths.append(str(path))

        result = self.council.ingest_batch(paths)
        self.assertEqual(len(result.items), 4)
        self.assertEqual({i.record_id for i in result.accepted}, {"rec-ok-1"})
        self.assertEqual({i.record_id for i in result.merged}, {"rec-ok-2"})
        rejected = {i.record_id: i for i in result.rejected}
        self.assertEqual(rejected["rec-x"].error_type, "DomainMismatchError")
        self.assertEqual(rejected["rec-y"].error_type, "UnknownFieldError")

    def test_batch_survives_missing_and_unreadable_file(self):
        paths = [str(Path(self.tmp.name) / "missing.json")]
        good = Path(self.tmp.name) / "good.json"
        good.write_text(json.dumps(base_payload(record_id="rec-ok")), encoding="utf-8")
        paths.append(str(good))
        result = self.council.ingest_batch(paths)
        self.assertEqual(len(result.rejected), 1)
        self.assertEqual(result.accepted[0].record_id, "rec-ok")

    # -- 重启幂等 ----------------------------------------------------------

    def test_restart_does_not_reconvene_or_republish(self):
        self._seed_published_series()

        rev2 = proposal(record_id="rec-a", revision=2,
                        definition="修订定义",
                        occurred_at="2026-03-15T09:00:00+08:00")
        self.council.intake(rev2)
        self.council.open_amendment(rev2)
        from metric_council import APPROVAL_CHAIN
        for stage in APPROVAL_CHAIN:
            self.council.approve_amendment_stage(CALIBER, 2, stage, "签署人")
        before_task = self.council.publish_recalculation(CALIBER, 2)

        # 模拟重启：用同一状态文件重建服务。
        reborn = MetricCouncil(JsonStore(Path(self.tmp.name) / "state.json"))
        self.assertEqual(reborn.caliber(CALIBER)["state"], CaliberState.APPROVED.value)
        # 审议会话仍是已通过的同一条，没有第二条。
        self.assertEqual(len(self.store.get("reviews")), 1)
        with self.assertRaises(ReviewError):
            reborn.convene(CALIBER)
        # 修订回算任务不重复发布。
        after_task = reborn.publish_recalculation(CALIBER, 2)
        self.assertEqual(after_task["task_id"], before_task["task_id"])
        self.assertEqual(len(self.store.get("recalc_tasks")), 1)
        # 重放收件仍判为重复，不新增收件。
        replayed = reborn.intake(rev2)
        self.assertIs(replayed.outcome, IntakeOutcome.DUPLICATE)


if __name__ == "__main__":
    unittest.main()
