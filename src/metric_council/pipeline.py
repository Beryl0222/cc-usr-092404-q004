"""文化指标口径审议业务流程：收件、审议、修订与回算。

领域边界由 :mod:`metric_council.contracts` 把住；本模块负责流程语义：

* **收件版本化**——多部门提交同一口径（``caliber_id``）时，定义、分母、
  价格口径、来源授权四个方面分别版本化；完全相同的材料合并为一次收件，
  内容冲突则保留各方版本并把口径置为“待会审”。
* **审议门禁**——普通审议与会审都只发起一次（持久化幂等）；口径通过后
  才允许进入回算。
* **修订与回算**——修订生成影响范围、新旧并行结果和有序审批链；已发布
  历史序列只追加、不改写；回算任务按“口径 + 修订号”幂等发布，重启不重复。
* **批量隔离**——批量文件逐个处理，单个文件的任何合同错误都只记为该
  文件的拒收，不阻断其他提案。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable

from .contracts import (
    DOMAIN,
    MetricProposal,
    PriceBasis,
    SourceAuthorization,
    ContractError,
    load_proposal,
)
from .store import JsonStore

# ---------------------------------------------------------------------------
# 状态枚举
# ---------------------------------------------------------------------------


class CaliberState(str, Enum):
    DRAFT = "draft"                      # 仅收件，尚未安排审议
    AWAITING_REVIEW = "awaiting_review"  # 单一方材料，待普通审议
    CONFLICT = "conflict"                # 多方材料冲突，待会审
    REVIEWING = "reviewing"              # 审议（含会审）进行中
    APPROVED = "approved"                # 口径通过，可进入回算
    REJECTED = "rejected"                # 审议驳回


class ReviewKind(str, Enum):
    NORMAL = "normal"
    JOINT = "joint"  # 会审


class AmendmentStatus(str, Enum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    RECALC_PUBLISHED = "recalc_published"
    REJECTED = "rejected"


#: 修订审批链：必须严格按此顺序逐级签署，不得跳级。
APPROVAL_CHAIN = (
    "文化统计处复核",
    "来源机构会签",
    "分管局领导审批",
)

#: 口径材料的四个分别版本化方面。
COMPONENTS = ("definition", "denominator", "price_basis", "authorization")


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------


class IntakeOutcome(str, Enum):
    ACCEPTED = "accepted"    # 新材料，产生新收件
    MERGED = "merged"        # 与已有材料完全相同，合并进同一次收件
    DUPLICATE = "duplicate"  # 同一 record_id+revision 重放，幂等忽略
    REJECTED = "rejected"    # 合同/修订错误，被拒收


@dataclass(frozen=True)
class IntakeResult:
    outcome: IntakeOutcome
    record_id: str | None = None
    caliber_id: str | None = None
    receipt_id: str | None = None
    error_type: str | None = None
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is not IntakeOutcome.REJECTED


@dataclass(frozen=True)
class BatchResult:
    items: tuple[IntakeResult, ...] = ()

    @property
    def accepted(self) -> tuple[IntakeResult, ...]:
        return tuple(i for i in self.items if i.outcome is IntakeOutcome.ACCEPTED)

    @property
    def merged(self) -> tuple[IntakeResult, ...]:
        return tuple(i for i in self.items if i.outcome is IntakeOutcome.MERGED)

    @property
    def duplicates(self) -> tuple[IntakeResult, ...]:
        return tuple(i for i in self.items if i.outcome is IntakeOutcome.DUPLICATE)

    @property
    def rejected(self) -> tuple[IntakeResult, ...]:
        return tuple(i for i in self.items if i.outcome is IntakeOutcome.REJECTED)


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def proposal_to_data(p: MetricProposal) -> dict[str, Any]:
    return {
        "schema_version": p.schema_version,
        "record_id": p.record_id,
        "domain": p.domain,
        "occurred_at": p.occurred_at.isoformat(),
        "revision": p.revision,
        "source": p.source,
        "caliber_id": p.caliber_id,
        "source_version": p.source_version,
        "region": p.region,
        "period": p.period,
        "title": p.title,
        "definition": p.definition,
        "denominator": p.denominator,
        "price_basis": p.price_basis.value,
        "constant_base_year": p.constant_base_year,
        "source_authorization": {
            "authority": p.source_authorization.authority,
            "document": p.source_authorization.document,
            "version": p.source_authorization.version,
        },
        "department": p.department,
        "period_start": p.period_start,
    }


def proposal_from_data(data: dict[str, Any]) -> MetricProposal:
    auth = data["source_authorization"]
    return MetricProposal(
        schema_version=data["schema_version"],
        record_id=data["record_id"],
        domain=data["domain"],
        occurred_at=datetime_from_iso(data["occurred_at"]),
        revision=data["revision"],
        source=data["source"],
        caliber_id=data["caliber_id"],
        source_version=data["source_version"],
        region=data["region"],
        period=data["period"],
        title=data["title"],
        definition=data["definition"],
        denominator=data["denominator"],
        price_basis=PriceBasis(data["price_basis"]),
        constant_base_year=data["constant_base_year"],
        source_authorization=SourceAuthorization(
            authority=auth["authority"], document=auth["document"], version=auth["version"]
        ),
        department=data["department"],
        period_start=data.get("period_start", ""),
    )


def datetime_from_iso(text: str):
    from datetime import datetime

    return datetime.fromisoformat(text)


def _component_fingerprint(p: MetricProposal, component: str) -> str:
    if component == "definition":
        return p.definition
    if component == "denominator":
        return p.denominator
    if component == "price_basis":
        return f"{p.price_basis.value}:{p.constant_base_year or ''}"
    return str(p.source_authorization)


# ---------------------------------------------------------------------------
# 主服务
# ---------------------------------------------------------------------------


class ReviewError(ValueError):
    """流程状态不允许该审议/回算操作。"""


class AmendmentError(ValueError):
    """修订流程错误（审批跳级、链未完成、影响范围缺失等）。"""


class MetricCouncil:
    """文化指标口径审议服务；状态全部落在给定 :class:`JsonStore` 中。"""

    def __init__(self, store: JsonStore) -> None:
        self.store = store
        # 初始化各状态段（已存在则不动），重启后沿用磁盘状态。
        store.require("records", {})       # record_id -> {revision: receipt_id}
        store.require("receipts", {})      # receipt_id -> receipt
        store.require("calibers", {})      # caliber_id -> 口径聚合状态
        store.require("reviews", {})      # caliber_id -> 审议会话
        store.require("amendments", {})   # caliber_id -> {revision: 修订}
        store.require("recalc_tasks", {}) # task_id -> 回算任务
        store.require("series", {})       # series_key -> [point]，只追加

    # ------------------------------------------------------------------
    # 收件
    # ------------------------------------------------------------------

    def ingest_file(self, path: str) -> IntakeResult:
        """读取并登记一个文件；任何错误都收敛为 :class:`IntakeResult`。"""
        try:
            proposal = load_proposal(path)
        except ContractError as exc:
            return IntakeResult(
                outcome=IntakeOutcome.REJECTED,
                record_id=getattr(exc, "record_id", None),
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        return self.intake(proposal)

    def ingest_batch(self, paths: Iterable[str]) -> BatchResult:
        """批量收件：单个文件失败只记录该文件拒收，绝不阻断其他提案。"""
        results: list[IntakeResult] = []
        for path in paths:
            try:
                results.append(self.ingest_file(path))
            except Exception as exc:  # 兜底：未预期错误同样隔离在本文件内
                results.append(
                    IntakeResult(
                        outcome=IntakeOutcome.REJECTED,
                        error_type=type(exc).__name__,
                        error_message=f"{path}: {exc}",
                    )
                )
        return BatchResult(tuple(results))

    def intake(self, proposal: MetricProposal) -> IntakeResult:
        """登记一条已通过合同校验的提案（幂等）。"""
        if proposal.domain != DOMAIN:  # 双保险：流程层也不接受外域对象
            return IntakeResult(
                outcome=IntakeOutcome.REJECTED,
                record_id=proposal.record_id,
                error_type="DomainMismatchError",
                error_message="流程层拒绝非文化统计域提案",
            )

        with self.store.lock:
            # 一次取齐所有相关状态段（同一把锁内无人能改），末尾统一落盘，
            # 避免 helper 各自 require/put 产生的旧拷贝覆盖。
            records = self.store.require("records", {})
            receipts = self.store.require("receipts", {})
            calibers = self.store.require("calibers", {})
            record_revs = records.setdefault(proposal.record_id, {})
            rev_key = str(proposal.revision)  # JSON 往返后键恒为字符串

            # 1) 同一 record_id+revision 重放：内容一致→幂等忽略；被篡改→非法修订。
            if rev_key in record_revs:
                receipt_id = record_revs[rev_key]
                prior_proposal = proposal_from_data(receipts[receipt_id]["proposal"])
                if prior_proposal.material_key == proposal.material_key:
                    return IntakeResult(
                        outcome=IntakeOutcome.DUPLICATE,
                        record_id=proposal.record_id,
                        caliber_id=proposal.caliber_id,
                        receipt_id=receipt_id,
                    )
                return IntakeResult(
                    outcome=IntakeOutcome.REJECTED,
                    record_id=proposal.record_id,
                    caliber_id=proposal.caliber_id,
                    error_type="IllegalRevisionError",
                    error_message=(
                        f"record_id={proposal.record_id} revision={proposal.revision} "
                        "已收件，同一修订号不得携带不同内容重新提交"
                    ),
                )

            # 2) 修订链必须连续且不得回退（首次必须是 1）。
            expected = (max(int(k) for k in record_revs) + 1) if record_revs else 1
            if proposal.revision != expected:
                return IntakeResult(
                    outcome=IntakeOutcome.REJECTED,
                    record_id=proposal.record_id,
                    caliber_id=proposal.caliber_id,
                    error_type="IllegalRevisionError",
                    error_message=(
                        f"修订号{'回退' if proposal.revision < expected else '跳跃'}: "
                        f"收到 {proposal.revision}，期望 {expected}"
                    ),
                )

            caliber = calibers.setdefault(proposal.caliber_id, self._new_caliber())
            prior_state = caliber["state"]

            # 3) 完全相同材料（含同口径）→ 合并为一次收件。
            merged_into = self._find_identical_receipt(caliber, receipts, proposal)
            if merged_into is not None:
                self._merge_into_receipt(merged_into, proposal, receipts, caliber)
                record_revs[rev_key] = merged_into
                self._transition_after_material(caliber, prior_state)
                self.store.put("records", records)
                self.store.put("receipts", receipts)
                self.store.put("calibers", calibers)
                return IntakeResult(
                    outcome=IntakeOutcome.MERGED,
                    record_id=proposal.record_id,
                    caliber_id=proposal.caliber_id,
                    receipt_id=merged_into,
                )

            # 4) 新材料：四个方面分别登记新版本，生成一次收件。
            receipt_id = self._register_material(caliber, proposal, receipts)
            record_revs[rev_key] = receipt_id

            # 5) 状态迁移（不触碰已通过/驳回/审议中的口径，修订走修订流程）。
            self._transition_after_material(caliber, prior_state)

            self.store.put("records", records)
            self.store.put("receipts", receipts)
            self.store.put("calibers", calibers)
            return IntakeResult(
                outcome=IntakeOutcome.ACCEPTED,
                record_id=proposal.record_id,
                caliber_id=proposal.caliber_id,
                receipt_id=receipt_id,
            )

    def _transition_after_material(self, caliber: dict[str, Any], prior_state: str) -> None:
        """收件后口径状态迁移；仅在审议前的收件状态之间流转。"""
        if prior_state not in (
            CaliberState.DRAFT.value,
            CaliberState.AWAITING_REVIEW.value,
            CaliberState.CONFLICT.value,
        ):
            # 已通过/驳回/审议中：新材料照样登记留痕，但不改变当前状态。
            return
        caliber["state"] = (
            CaliberState.CONFLICT.value
            if self._has_conflict(caliber)
            else CaliberState.AWAITING_REVIEW.value
        )

    def _new_caliber(self) -> dict[str, Any]:
        return {
            "state": CaliberState.DRAFT.value,
            "components": {
                name: {
                    # 指纹 -> {version, departments, first_record_id, first_received_at}
                }
                for name in COMPONENTS
            },
            "receipts": [],
        }

    def _find_identical_receipt(self, caliber: dict[str, Any], receipts: dict[str, Any],
                                p: MetricProposal) -> str | None:
        # 合并只针对跨记录/跨部门的完全相同材料；同一 record_id 的后续修订号
        # 是修订链事件，即使内容未变也产生新收件，不在这里合并。
        for receipt_id in caliber["receipts"]:
            receipt = receipts[receipt_id]
            if p.record_id in receipt["record_ids"]:
                continue
            other = proposal_from_data(receipt["proposal"])
            if other.material_key == p.material_key:
                return receipt_id
        return None

    def _merge_into_receipt(self, receipt_id: str, p: MetricProposal,
                            receipts: dict[str, Any], caliber: dict[str, Any]) -> None:
        receipt = receipts[receipt_id]
        submitters: set[str] = set(receipt["submitters"])
        submitters.add(p.department)
        receipt["submitters"] = sorted(submitters)
        receipt["record_ids"].append(p.record_id)
        # 相同材料合并：四个方面的既有版本把新提交部门记为共同提交方。
        for component in COMPONENTS:
            fp = _component_fingerprint(p, component)
            version_box = caliber["components"][component].get(fp)
            if version_box is not None:
                depts = set(version_box["departments"]) | {p.department}
                version_box["departments"] = sorted(depts)

    def _register_material(self, caliber: dict[str, Any], p: MetricProposal,
                           receipts: dict[str, Any]) -> str:
        # 四方面分别取号：相同指纹共享版本号，新指纹顺延新版本。
        for component in COMPONENTS:
            fp = _component_fingerprint(p, component)
            versions = caliber["components"][component]
            if fp not in versions:
                next_version = len(versions) + 1
                versions[fp] = {
                    "version": next_version,
                    "fingerprint": fp,
                    "departments": [p.department],
                    "first_record_id": p.record_id,
                    "first_received_at": p.occurred_at.isoformat(),
                }
            else:
                depts = set(versions[fp]["departments"]) | {p.department}
                versions[fp]["departments"] = sorted(depts)

        receipt_id = f"rcpt-{p.caliber_id}-r{p.revision}-{p.record_id}"
        receipts[receipt_id] = {
            "receipt_id": receipt_id,
            "caliber_id": p.caliber_id,
            "record_ids": [p.record_id],
            "submitters": [p.department],
            "proposal": proposal_to_data(p),
            "component_versions": {
                component: caliber["components"][component][_component_fingerprint(p, component)]["version"]
                for component in COMPONENTS
            },
        }
        caliber["receipts"].append(receipt_id)
        return receipt_id

    def _has_conflict(self, caliber: dict[str, Any]) -> bool:
        # 任一材料方面存在两个及以上彼此不同的版本，即为内容冲突。
        return any(len(caliber["components"][c]) > 1 for c in COMPONENTS)

    # ------------------------------------------------------------------
    # 审议 / 会审
    # ------------------------------------------------------------------

    def convene(self, caliber_id: str) -> dict[str, Any]:
        """发起审议；冲突口径自动为会审。已发起则原样返回，绝不重复发起。"""
        with self.store.lock:
            calibers = self.store.require("calibers", {})
            if caliber_id not in calibers:
                raise ReviewError(f"口径 {caliber_id} 尚未收件，无法发起审议")
            caliber = calibers[caliber_id]

            # 终态检查先于会话重放：已通过/驳回的口径不得重新发起审议。
            if caliber["state"] in (CaliberState.APPROVED.value, CaliberState.REJECTED.value):
                raise ReviewError(
                    f"口径 {caliber_id} 已"
                    f"{('通过' if caliber['state'] == CaliberState.APPROVED.value else '驳回')}，"
                    "不得重复发起审议；修订请走修订流程"
                )

            reviews = self.store.require("reviews", {})
            if caliber_id in reviews:
                return dict(reviews[caliber_id])  # 幂等：重启/重放拿到同一会话

            is_conflict = self._has_conflict(caliber)
            session = {
                "caliber_id": caliber_id,
                "kind": ReviewKind.JOINT.value if is_conflict else ReviewKind.NORMAL.value,
                "state": CaliberState.REVIEWING.value,
                "convened": True,
            }
            reviews[caliber_id] = session
            caliber["state"] = CaliberState.REVIEWING.value
            self.store.put("reviews", reviews)
            self.store.put("calibers", calibers)
            return dict(session)

    def decide(
        self,
        caliber_id: str,
        approved: bool,
        *,
        chosen_versions: dict[str, int] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """审议决定。会审裁决冲突时用 ``chosen_versions`` 为每个方面选定版本。"""
        with self.store.lock:
            calibers = self.store.require("calibers", {})
            reviews = self.store.require("reviews", {})
            if caliber_id not in calibers:
                raise ReviewError(f"口径 {caliber_id} 不存在")
            session = reviews.get(caliber_id)
            if session is None or not session.get("convened"):
                raise ReviewError("必须先 convene() 发起审议，禁止不经审议直接通过")
            if session["state"] != CaliberState.REVIEWING.value:
                raise ReviewError(f"审议已结束: {session['state']}")

            caliber = calibers[caliber_id]
            if not approved:
                caliber["state"] = CaliberState.REJECTED.value
                session["state"] = CaliberState.REJECTED.value
                session["note"] = note
                self.store.put("calibers", calibers)
                self.store.put("reviews", reviews)
                return dict(session)

            # 通过：确认裁决版本合法；冲突口径必须逐方面选定一个现存版本。
            winning: dict[str, int] = {}
            for component in COMPONENTS:
                versions = caliber["components"][component]
                available = {box["version"] for box in versions.values()}
                if chosen_versions and component in chosen_versions:
                    picked = chosen_versions[component]
                    if picked not in available:
                        raise ReviewError(
                            f"会审裁决的 {component} 版本 v{picked} 不存在（可选 {sorted(available)}）"
                        )
                    winning[component] = picked
                else:
                    if len(available) > 1:
                        raise ReviewError(
                            f"{component} 存在多方冲突版本 {sorted(available)}，会审必须逐项选定，禁止默认通过"
                        )
                    winning[component] = next(iter(available))

            caliber["state"] = CaliberState.APPROVED.value
            caliber["winning_versions"] = winning
            session["state"] = CaliberState.APPROVED.value
            session["winning_versions"] = winning
            session["note"] = note
            self.store.put("calibers", calibers)
            self.store.put("reviews", reviews)
            return dict(session)

    # ------------------------------------------------------------------
    # 已发布历史序列（只追加）
    # ------------------------------------------------------------------

    @staticmethod
    def _series_key(caliber_id: str, region: str, period: str) -> str:
        return f"{caliber_id}|{region}|{period}"

    def publish_series_point(self, caliber_id: str, region: str, period: str,
                             point_period: str, value: float, *, revision: int = 1) -> dict[str, Any]:
        """向历史序列追加一个发布点；已存在的期间拒绝改写。"""
        with self.store.lock:
            series = self.store.require("series", {})
            key = self._series_key(caliber_id, region, period)
            points = series.setdefault(key, [])
            if any(pt["period"] == point_period for pt in points):
                raise AmendmentError(
                    f"历史已发布序列 {key} 的期间 {point_period} 已存在，禁止直接改写；请发起修订"
                )
            point = {"period": point_period, "value": value, "revision": revision}
            points.append(point)
            points.sort(key=lambda pt: pt["period"])
            self.store.put("series", series)
            return dict(point)

    def get_series(self, caliber_id: str, region: str, period: str) -> tuple[dict[str, Any], ...]:
        series = self.store.require("series", {})
        return tuple(dict(pt) for pt in series.get(self._series_key(caliber_id, region, period), []))

    # ------------------------------------------------------------------
    # 修订：影响范围 + 并行旧新结果 + 审批链
    # ------------------------------------------------------------------

    def open_amendment(self, proposal: MetricProposal, *,
                       effective_from: str | None = None) -> dict[str, Any]:
        """对已通过口径登记一次修订（口径新版本），生成影响范围与审批链。

        修订本身同样要先进件——调用方通常先 :meth:`intake` 得到 ACCEPTED，
        再调用本方法；``effective_from`` 为受影响起始期间（含），默认取
        提案自身的 ``period_start``。
        """
        with self.store.lock:
            calibers = self.store.require("calibers", {})
            caliber = calibers.get(proposal.caliber_id)
            if caliber is None:
                raise AmendmentError("原口径不存在，应走首次提案而非修订")
            if caliber["state"] != CaliberState.APPROVED.value:
                raise AmendmentError(
                    f"原口径状态为 {caliber['state']}，只有已通过口径才能受理修订"
                )
            if proposal.revision <= 1:
                raise AmendmentError("revision=1 是首次提案，不是修订")

            amendments = self.store.require("amendments", {})
            per_caliber = amendments.setdefault(proposal.caliber_id, {})
            if str(proposal.revision) in per_caliber:
                return dict(per_caliber[str(proposal.revision)])  # 幂等重放

            start = effective_from or proposal.period_start
            impact = self._impact_scope(proposal.caliber_id, start, proposal.region)
            amendment = {
                "caliber_id": proposal.caliber_id,
                "revision": proposal.revision,
                "record_id": proposal.record_id,
                "effective_from": start,
                "region": proposal.region,
                "status": AmendmentStatus.PENDING_APPROVAL.value,
                "impact": impact,
                "approval_chain": [
                    {"stage": stage, "approved": False, "approver": None, "comment": ""}
                    for stage in APPROVAL_CHAIN
                ],
                "recalc_task_id": None,
                "parallel_results": None,
                "proposal": proposal_to_data(proposal),
            }
            per_caliber[str(proposal.revision)] = amendment
            self.store.put("amendments", amendments)
            return dict(amendment)

    def _impact_scope(self, caliber_id: str, start_period: str, region: str) -> dict[str, Any]:
        """受影响范围：该口径（含更窄地区）已发布序列中 >= 起始期间的点。"""
        series = self.store.require("series", {})
        affected: list[dict[str, Any]] = []
        prefix = f"{caliber_id}|"
        for key, points in series.items():
            if not key.startswith(prefix):
                continue
            _cid, point_region, period_kind = key.split("|", 2)
            # CN 全国修订覆盖省级点；省级修订只覆盖本省。
            if region != "CN" and point_region != region:
                continue
            hits = [pt for pt in points if pt["period"] >= start_period]
            if hits:
                affected.append(
                    {
                        "series_key": key,
                        "region": point_region,
                        "period": period_kind,
                        "periods": [pt["period"] for pt in hits],
                        "point_count": len(hits),
                    }
                )
        return {
            "caliber_id": caliber_id,
            "effective_from": start_period,
            "region": region,
            "affected_series": affected,
            "affected_point_count": sum(a["point_count"] for a in affected),
        }

    def approve_amendment_stage(self, caliber_id: str, revision: int,
                                stage: str, approver: str, comment: str = "") -> dict[str, Any]:
        """按 :data:`APPROVAL_CHAIN` 顺序逐级签署；跳级/重复签署都拒绝。"""
        with self.store.lock:
            amendments = self.store.require("amendments", {})
            per_caliber = amendments.get(caliber_id, {})
            amendment = per_caliber.get(str(revision))
            if amendment is None:
                raise AmendmentError(f"修订 {caliber_id} r{revision} 不存在")
            if amendment["status"] != AmendmentStatus.PENDING_APPROVAL.value:
                raise AmendmentError(f"修订已审结: {amendment['status']}")

            chain = amendment["approval_chain"]
            idx = next((i for i, step in enumerate(chain) if step["stage"] == stage), None)
            if idx is None:
                raise AmendmentError(f"审批链中不存在环节 {stage!r}")
            if idx > 0 and not chain[idx - 1]["approved"]:
                raise AmendmentError(
                    f"审批链必须逐级签署：{chain[idx - 1]['stage']} 尚未通过，不得签署 {stage}"
                )
            if chain[idx]["approved"]:
                raise AmendmentError(f"{stage} 已签署，禁止重复签署")
            chain[idx].update(approved=True, approver=approver, comment=comment)

            if all(step["approved"] for step in chain):
                amendment["status"] = AmendmentStatus.APPROVED.value
            self.store.put("amendments", amendments)
            return dict(amendment)

    def publish_recalculation(
        self,
        caliber_id: str,
        revision: int,
        *,
        calculator: Callable[[MetricProposal, dict[str, Any]], float] | None = None,
    ) -> dict[str, Any]:
        """发布回算任务（幂等）。

        * 首次回算（``revision=1``）要求口径已通过普通/会审审议；
        * 修订回算（``revision>=2``）要求修订审批链全部签署完成；
        * 任务按“口径 + 修订号”只发布一次，重启重放返回既有任务；
        * 回算产出 *新旧并行* 结果快照，已发布历史序列原样保留，绝不改写。
        """
        with self.store.lock:
            calibers = self.store.require("calibers", {})
            if caliber_id not in calibers:
                raise AmendmentError(f"口径 {caliber_id} 不存在")
            caliber = calibers[caliber_id]

            if revision == 1:
                if caliber["state"] != CaliberState.APPROVED.value:
                    raise ReviewError(
                        f"口径状态 {caliber['state']}：口径通过审议前不得进入回算"
                    )
                impact = None
                proposal_data = self._latest_proposal_data(caliber_id, revision)
            else:
                amendments = self.store.require("amendments", {})
                amendment = amendments.get(caliber_id, {}).get(str(revision))
                if amendment is None:
                    raise AmendmentError(f"修订 {caliber_id} r{revision} 尚未开立")
                if amendment["status"] not in (
                    AmendmentStatus.APPROVED.value,
                    AmendmentStatus.RECALC_PUBLISHED.value,
                ):
                    raise AmendmentError(
                        "修订审批链未全部完成，回算不得提前发布"
                    )
                impact = amendment["impact"]
                proposal_data = amendment["proposal"]

            proposal = proposal_from_data(proposal_data)
            task_id = f"recalc-{caliber_id}-r{revision}"
            tasks = self.store.require("recalc_tasks", {})
            if task_id in tasks:
                return dict(tasks[task_id])  # 幂等：重启后绝不重复发布

            # 旧结果：直接取已发布序列快照；新结果：按影响范围逐点并行试算。
            old_results, new_results = self._parallel_results(
                caliber_id, revision, impact, proposal, calculator
            )
            task = {
                "task_id": task_id,
                "caliber_id": caliber_id,
                "revision": revision,
                "impact": impact,
                "old_results": old_results,
                "new_results": new_results,
                "published_series_mutated": False,
            }
            tasks[task_id] = task
            self.store.put("recalc_tasks", tasks)

            if revision >= 2:
                amendments = self.store.require("amendments", {})
                amendment = amendments[caliber_id][str(revision)]
                amendment["status"] = AmendmentStatus.RECALC_PUBLISHED.value
                amendment["recalc_task_id"] = task_id
                amendment["parallel_results"] = {"old": old_results, "new": new_results}
                self.store.put("amendments", amendments)
            return dict(task)

    def _latest_proposal_data(self, caliber_id: str, revision: int) -> dict[str, Any]:
        receipts = self.store.require("receipts", {})
        calibers = self.store.require("calibers", {})
        for receipt_id in reversed(calibers[caliber_id]["receipts"]):
            data = receipts[receipt_id]["proposal"]
            if data["revision"] == revision:
                return data
        raise AmendmentError(f"找不到 {caliber_id} r{revision} 的收件材料")

    def _parallel_results(
        self,
        caliber_id: str,
        revision: int,
        impact: dict[str, Any] | None,
        proposal: MetricProposal,
        calculator: Callable[[MetricProposal, dict[str, Any]], float] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        series = self.store.require("series", {})
        if revision == 1:
            scope_keys = [
                key for key in series
                if key.startswith(f"{caliber_id}|")
                and (proposal.region == "CN" or key.split("|")[1] == proposal.region)
            ]
            scope = [
                {"series_key": key, "periods": [pt["period"] for pt in series[key]]}
                for key in sorted(scope_keys)
            ]
        else:
            scope = [
                {"series_key": a["series_key"], "periods": a["periods"]}
                for a in impact["affected_series"]
            ]

        old_results: list[dict[str, Any]] = []
        new_results: list[dict[str, Any]] = []
        for item in scope:
            key = item["series_key"]
            points = {pt["period"]: pt for pt in series.get(key, [])}
            for period in item["periods"]:
                old_point = points.get(period)
                old_value = old_point["value"] if old_point else None
                # 并行试算：默认计算器保持原值（真实环境替换为口径重算函数），
                # 无论结果如何，只写入任务快照，不触碰已发布序列。
                new_value = (
                    calculator(proposal, {"series_key": key, "period": period, "old": old_value})
                    if calculator is not None
                    else old_value
                )
                row = {"series_key": key, "period": period}
                old_results.append({**row, "value": old_value, "revision_of_record": (
                    old_point["revision"] if old_point else None)})
                new_results.append({**row, "value": new_value, "candidate_revision": revision})
        return old_results, new_results

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def caliber(self, caliber_id: str) -> dict[str, Any]:
        calibers = self.store.require("calibers", {})
        if caliber_id not in calibers:
            raise ReviewError(f"口径 {caliber_id} 不存在")
        return dict(calibers[caliber_id])

    def component_versions(self, caliber_id: str, component: str) -> tuple[dict[str, Any], ...]:
        """查看某一方面保留的各方版本（会审依据）。"""
        caliber = self.caliber(caliber_id)
        if component not in COMPONENTS:
            raise ReviewError(f"未知材料方面 {component!r}，允许 {COMPONENTS}")
        return tuple(
            dict(box) for box in sorted(
                caliber["components"][component].values(), key=lambda b: b["version"]
            )
        )

    def amendment(self, caliber_id: str, revision: int) -> dict[str, Any]:
        amendments = self.store.require("amendments", {})
        try:
            return dict(amendments[caliber_id][str(revision)])
        except KeyError as exc:
            raise AmendmentError(f"修订 {caliber_id} r{revision} 不存在") from exc

    def recalc_task(self, task_id: str) -> dict[str, Any]:
        tasks = self.store.require("recalc_tasks", {})
        if task_id not in tasks:
            raise AmendmentError(f"回算任务 {task_id} 不存在")
        return dict(tasks[task_id])
