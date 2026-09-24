"""多部门收件、会审与回算的持久化服务。

* 定义、分母、价格口径、来源授权按口径分别版本化；完全相同的
  材料合并为一次收件，内容冲突时各方版本都保留并发起会审。
* 口径经会审裁决并正式通过后，才允许发起回算。
* 回算不改写已发布序列：只产出影响范围、并行的新旧结果，
  走完审批链后才以追加方式发布，历史版本全部保留。
* 全部状态落盘（state.json），重启后重放相同提交不会重复
  发起会审，也不会重复发布回算任务。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .problems import (
    KIND_INVALID_FIELD,
    KIND_INVALID_REVISION,
    KIND_WRONG_DOMAIN,
    Problem,
    ProposalRejected,
)
from .proposals import ASPECT_FIELDS, DEFAULT_DOMAIN, Proposal, period_end_date
from .registry import DomainRegistry, default_registry

STATE_VERSION = 1
STATE_FILENAME = "state.json"

# 回算结果的审批链，必须按顺序逐级签署
APPROVAL_CHAIN = ("division_check", "council_signoff", "publish_approval")


class CouncilError(Exception):
    """审议流程错误，``kind`` 供调用方区分。"""

    def __init__(self, kind: str, detail: str):
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}")


@dataclass(frozen=True)
class SubmitResult:
    """一次收件的结果。"""

    receipt_id: str
    merged: bool  # True 表示与既有收件完全相同，合并处理
    contested_aspects: tuple[str, ...]  # 本次提交后仍处于冲突的方面
    sessions_started: tuple[str, ...]  # 本次新发起的会审


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_state(domain: str) -> dict:
    return {
        "state_version": STATE_VERSION,
        "domain": domain,
        "receipts": {},
        "lineage": {},
        "aspects": {},
        "sessions": {},
        "decisions": {},
        "runs": {},
        "series": {},
    }


class CouncilService:
    """文化指标口径审议服务；可选 state_dir 持久化以支持重启。"""

    def __init__(
        self,
        registry: DomainRegistry | None = None,
        domain: str = DEFAULT_DOMAIN,
        state_dir=None,
        clock=None,
    ):
        self.registry = registry or default_registry()
        self.domain = domain
        self.clock = clock or _utc_now
        self._state_path = (
            Path(state_dir) / STATE_FILENAME if state_dir is not None else None
        )
        self._state = self._load() if self._state_path else _empty_state(self.domain)

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _load(self) -> dict:
        path = self._state_path
        if not path.exists():
            return _empty_state(self.domain)
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("state_version") != STATE_VERSION:
            raise CouncilError(
                "unsupported_state",
                f"状态文件版本 {state.get('state_version')!r} 无法迁移",
            )
        if state.get("domain") != self.domain:
            raise CouncilError(
                KIND_WRONG_DOMAIN,
                f"状态文件属于业务领域 {state.get('domain')!r}",
            )
        return state

    def _save(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, self._state_path)

    # ------------------------------------------------------------------
    # 收件
    # ------------------------------------------------------------------
    def submit(self, proposal: Proposal) -> SubmitResult:
        """受理一份提案；领域不符或修订非法时抛出 ProposalRejected。"""
        if proposal.domain != self.domain:
            raise ProposalRejected(
                [
                    Problem(
                        KIND_WRONG_DOMAIN,
                        "domain",
                        f"记录属于业务领域 {proposal.domain!r}，本入口只受理 {self.domain!r}",
                    )
                ]
            )
        if self.registry.caliber(self.domain, proposal.caliber_id) is None:
            raise ProposalRejected(
                [
                    Problem(
                        KIND_INVALID_FIELD,
                        "caliber_id",
                        f"未知口径标识 {proposal.caliber_id!r}",
                    )
                ]
            )

        receipt_id = self._receipt_id(proposal)
        self._check_lineage(proposal, receipt_id)

        receipts = self._state["receipts"]
        if receipt_id in receipts:
            # 完全相同的材料：合并为一次收件，不产生任何新副作用
            receipt = receipts[receipt_id]
            if proposal.department not in receipt["departments"]:
                receipt["departments"].append(proposal.department)
            if proposal.record_id not in receipt["record_ids"]:
                receipt["record_ids"].append(proposal.record_id)
            self._state["lineage"][proposal.record_id] = {
                "revision": proposal.revision,
                "receipt_id": receipt_id,
            }
            self._save()
            return SubmitResult(receipt_id, True, (), ())

        receipts[receipt_id] = {
            "receipt_id": receipt_id,
            "caliber_id": proposal.caliber_id,
            "departments": [proposal.department],
            "record_ids": [proposal.record_id],
            "material": proposal.material.as_dict(),
            "received_at": self.clock(),
        }
        self._state["lineage"][proposal.record_id] = {
            "revision": proposal.revision,
            "receipt_id": receipt_id,
        }

        contested: list[str] = []
        sessions_started: list[str] = []
        aspects = self._state["aspects"].setdefault(
            proposal.caliber_id,
            {aspect: {"versions": [], "settled": None} for aspect in ASPECT_FIELDS},
        )
        for aspect in ASPECT_FIELDS:
            content = getattr(proposal.material, aspect)
            bucket = aspects[aspect]
            version = self._find_version(bucket, content)
            if version is None:
                version = {
                    "version": len(bucket["versions"]) + 1,
                    "content": content,
                    "departments": [],
                    "receipt_ids": [],
                }
                bucket["versions"].append(version)
                if len(bucket["versions"]) > 1:
                    # 出现新的冲突版本，此前裁决失效，等待重新会审
                    bucket["settled"] = None
            if proposal.department not in version["departments"]:
                version["departments"].append(proposal.department)
            if receipt_id not in version["receipt_ids"]:
                version["receipt_ids"].append(receipt_id)
            if len(bucket["versions"]) > 1:
                contested.append(aspect)
                session_id = self._ensure_session(
                    proposal.caliber_id, aspect, bucket
                )
                if session_id is not None:
                    sessions_started.append(session_id)

        self._save()
        return SubmitResult(
            receipt_id, False, tuple(contested), tuple(sessions_started)
        )

    def _receipt_id(self, proposal: Proposal) -> str:
        digest = hashlib.sha256(
            f"{proposal.caliber_id}\n{proposal.material.canonical()}".encode("utf-8")
        ).hexdigest()
        return f"rcpt-{digest[:16]}"

    def _check_lineage(self, proposal: Proposal, receipt_id: str) -> None:
        known = self._state["lineage"].get(proposal.record_id)
        if known is None:
            if proposal.revision != 1:
                raise ProposalRejected(
                    [
                        Problem(
                            KIND_INVALID_REVISION,
                            "revision",
                            "首次提交的修订号须为 1",
                        )
                    ]
                )
            return
        if proposal.revision < known["revision"]:
            raise ProposalRejected(
                [
                    Problem(
                        KIND_INVALID_REVISION,
                        "revision",
                        f"修订号 {proposal.revision} 早于已受理的 {known['revision']}",
                    )
                ]
            )
        if proposal.revision == known["revision"] and known["receipt_id"] != receipt_id:
            raise ProposalRejected(
                [
                    Problem(
                        KIND_INVALID_REVISION,
                        "revision",
                        "同一修订号携带了不同材料",
                    )
                ]
            )
        if proposal.revision > known["revision"] + 1:
            raise ProposalRejected(
                [
                    Problem(
                        KIND_INVALID_REVISION,
                        "revision",
                        f"修订号从 {known['revision']} 跳到 {proposal.revision}",
                    )
                ]
            )

    @staticmethod
    def _find_version(bucket: dict, content: str) -> dict | None:
        for version in bucket["versions"]:
            if version["content"] == content:
                return version
        return None

    def _ensure_session(self, caliber_id: str, aspect: str, bucket: dict) -> str | None:
        """冲突方面只保留一个进行中的会审；已存在则归并，不重复发起。"""
        sessions = self._state["sessions"]
        for session in sessions.values():
            if (
                session["caliber_id"] == caliber_id
                and session["aspect"] == aspect
                and session["status"] == "open"
            ):
                for version in bucket["versions"]:
                    if version["version"] not in session["version_ids"]:
                        session["version_ids"].append(version["version"])
                return None
        seq = 1 + sum(
            1
            for session in sessions.values()
            if session["caliber_id"] == caliber_id and session["aspect"] == aspect
        )
        session_id = f"review-{caliber_id}-{aspect}-{seq}"
        sessions[session_id] = {
            "session_id": session_id,
            "caliber_id": caliber_id,
            "aspect": aspect,
            "status": "open",
            "version_ids": [version["version"] for version in bucket["versions"]],
            "ruling": None,
            "opened_at": self.clock(),
        }
        return session_id

    # ------------------------------------------------------------------
    # 会审与口径通过
    # ------------------------------------------------------------------
    def rule(self, session_id: str, chosen_version: int, decided_by: str) -> dict:
        """会审裁决：为冲突方面选定保留版本。"""
        session = self._state["sessions"].get(session_id)
        if session is None:
            raise CouncilError("unknown_session", f"未知会审 {session_id!r}")
        if session["status"] != "open":
            raise CouncilError("session_closed", f"会审 {session_id!r} 已结案")
        if chosen_version not in session["version_ids"]:
            raise CouncilError(
                "unknown_version",
                f"版本 {chosen_version} 不在会审 {session_id!r} 范围内",
            )
        session["status"] = "ruled"
        session["ruling"] = {
            "chosen_version": chosen_version,
            "decided_by": decided_by,
            "at": self.clock(),
        }
        bucket = self._state["aspects"][session["caliber_id"]][session["aspect"]]
        bucket["settled"] = chosen_version
        self._save()
        return session

    def approve_caliber(self, caliber_id: str, approver: str) -> dict:
        """口径正式通过；存在未结会审或未决冲突时拒绝。"""
        aspects = self._state["aspects"].get(caliber_id)
        if aspects is None:
            raise CouncilError("unknown_caliber", f"口径 {caliber_id!r} 尚未收件")
        open_sessions = [
            s["session_id"]
            for s in self._state["sessions"].values()
            if s["caliber_id"] == caliber_id and s["status"] == "open"
        ]
        if open_sessions:
            raise CouncilError(
                "caliber_not_eligible", f"存在未结会审 {open_sessions}"
            )
        aspect_versions: dict[str, int] = {}
        for aspect, bucket in aspects.items():
            if not bucket["versions"]:
                raise CouncilError(
                    "caliber_not_eligible", f"方面 {aspect!r} 没有任何版本"
                )
            if len(bucket["versions"]) > 1 and bucket["settled"] is None:
                raise CouncilError(
                    "caliber_not_eligible", f"方面 {aspect!r} 的冲突尚未裁决"
                )
            aspect_versions[aspect] = bucket["settled"] or 1
        fingerprint = hashlib.sha256(
            json.dumps(aspect_versions, sort_keys=True).encode("utf-8")
        ).hexdigest()
        decision_id = f"dec-{caliber_id}-{fingerprint[:10]}"
        decisions = self._state["decisions"]
        if decision_id not in decisions:
            decisions[decision_id] = {
                "decision_id": decision_id,
                "caliber_id": caliber_id,
                "aspect_versions": aspect_versions,
                "status": "approved",
                "approved_by": approver,
                "at": self.clock(),
            }
            self._save()
        return decisions[decision_id]

    # ------------------------------------------------------------------
    # 回算
    # ------------------------------------------------------------------
    def request_backcalc(
        self, decision_id: str, effective_from: str, reviser=None
    ) -> dict:
        """为已通过的口径发起回算；同一决定与生效期只发布一次任务。"""
        decision = self._state["decisions"].get(decision_id)
        if decision is None:
            raise CouncilError(
                "caliber_not_approved", f"口径决定 {decision_id!r} 不存在或未通过"
            )
        caliber_id = decision["caliber_id"]
        caliber = self.registry.caliber(self.domain, caliber_id)
        if period_end_date(caliber.frequency, effective_from) is None:
            raise CouncilError(
                "invalid_period",
                f"生效期 {effective_from!r} 与口径频率 {caliber.frequency} 不一致",
            )
        run_id = f"run-{decision_id}-{effective_from}"
        runs = self._state["runs"]
        if run_id in runs:
            return runs[run_id]

        impact = []
        for key, versions in self._state["series"].items():
            cell_caliber, region, period = key.split("|")
            if cell_caliber != caliber_id or period < effective_from:
                continue
            published = versions[-1]["value"]
            revised = reviser(region, period, published) if reviser else published
            impact.append(
                {
                    "region": region,
                    "period": period,
                    "published_value": published,
                    "revised_value": revised,
                }
            )
        impact.sort(key=lambda cell: (cell["region"], cell["period"]))
        runs[run_id] = {
            "run_id": run_id,
            "decision_id": decision_id,
            "caliber_id": caliber_id,
            "effective_from": effective_from,
            "impact": impact,
            "approvals": [],
            "status": "pending_approval",
            "created_at": self.clock(),
        }
        self._save()
        return runs[run_id]

    def approve_run(self, run_id: str, step: str, approver: str) -> dict:
        """按审批链顺序签署；跳级或乱序一律拒绝。"""
        run = self._state["runs"].get(run_id)
        if run is None:
            raise CouncilError("unknown_run", f"未知回算任务 {run_id!r}")
        if run["status"] != "pending_approval":
            raise CouncilError(
                "invalid_state", f"回算任务 {run_id!r} 当前状态为 {run['status']}"
            )
        expected = APPROVAL_CHAIN[len(run["approvals"])]
        if step != expected:
            raise CouncilError(
                "approval_out_of_order",
                f"当前应签署 {expected!r}，收到 {step!r}",
            )
        run["approvals"].append(
            {"step": step, "approver": approver, "at": self.clock()}
        )
        if len(run["approvals"]) == len(APPROVAL_CHAIN):
            run["status"] = "approved"
        self._save()
        return run

    def publish_run(self, run_id: str) -> dict:
        """审批链走完后发布：以追加方式写入序列，历史版本保留。"""
        run = self._state["runs"].get(run_id)
        if run is None:
            raise CouncilError("unknown_run", f"未知回算任务 {run_id!r}")
        if run["status"] != "approved":
            raise CouncilError(
                "invalid_state",
                f"回算任务 {run_id!r} 未走完审批链，当前状态为 {run['status']}",
            )
        series = self._state["series"]
        for cell in run["impact"]:
            key = f"{run['caliber_id']}|{cell['region']}|{cell['period']}"
            series.setdefault(key, []).append(
                {
                    "value": cell["revised_value"],
                    "origin": run_id,
                    "at": self.clock(),
                }
            )
        run["status"] = "published"
        self._save()
        return run

    # ------------------------------------------------------------------
    # 已发布序列（只增不改）
    # ------------------------------------------------------------------
    def seed_series(self, caliber_id: str, region: str, period: str, value) -> None:
        """登记基线已发布值；发布后的单元格不会被回算直接改写。"""
        caliber = self.registry.caliber(self.domain, caliber_id)
        if caliber is None:
            raise CouncilError("unknown_caliber", f"未知口径 {caliber_id!r}")
        if period_end_date(caliber.frequency, period) is None:
            raise CouncilError(
                "invalid_period",
                f"统计周期 {period!r} 与口径频率 {caliber.frequency} 不一致",
            )
        key = f"{caliber_id}|{region}|{period}"
        self._state["series"].setdefault(key, []).append(
            {"value": value, "origin": "baseline", "at": self.clock()}
        )
        self._save()

    def series_history(self, caliber_id: str, region: str, period: str) -> list:
        key = f"{caliber_id}|{region}|{period}"
        return [dict(v) for v in self._state["series"].get(key, [])]

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def receipt(self, receipt_id: str) -> dict | None:
        receipt = self._state["receipts"].get(receipt_id)
        return dict(receipt) if receipt else None

    def aspect_versions(self, caliber_id: str, aspect: str) -> list:
        bucket = self._state["aspects"].get(caliber_id, {}).get(aspect)
        return [dict(v) for v in bucket["versions"]] if bucket else []

    def open_sessions(self, caliber_id: str | None = None) -> list:
        return [
            dict(s)
            for s in self._state["sessions"].values()
            if s["status"] == "open"
            and (caliber_id is None or s["caliber_id"] == caliber_id)
        ]

    def session(self, session_id: str) -> dict | None:
        session = self._state["sessions"].get(session_id)
        return dict(session) if session else None

    def decision(self, decision_id: str) -> dict | None:
        decision = self._state["decisions"].get(decision_id)
        return dict(decision) if decision else None

    def run(self, run_id: str) -> dict | None:
        run = self._state["runs"].get(run_id)
        return dict(run) if run else None

    def counts(self) -> dict:
        """各类记录的数量，用于核对重启后没有重复副作用。"""
        return {
            name: len(self._state[name])
            for name in (
                "receipts",
                "sessions",
                "decisions",
                "runs",
                "series",
            )
        }
