"""批量提案处理：局部失败不阻断其他提案，重放不产生重复副作用。

批量文件可以是提案数组，也可以是 ``{"schema_version", "batch_id",
"items"}`` 信封。每个条目独立走提案读取与收件入口，失败只记录在
该条目名下；配合 CouncilService 的持久化，重启后重放同一批文件
不会重复发起会审或重复发布回算任务。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .council import CouncilError, CouncilService
from .problems import (
    KIND_DUPLICATE_FIELD,
    KIND_INVALID_FIELD,
    KIND_INVALID_JSON,
    KIND_MISSING_FIELD,
    KIND_NOT_PLAIN_OBJECT,
    KIND_UNKNOWN_FIELD,
    Problem,
    ProposalRejected,
)
from .proposals import ProposalEntry

BATCH_ENVELOPE_FIELDS = frozenset({"schema_version", "batch_id", "items"})

STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"


@dataclass(frozen=True)
class ItemOutcome:
    """批量文件中单个条目的处理结果。"""

    index: int
    record_id: str | None
    status: str
    receipt_id: str | None
    merged: bool
    problems: tuple[Problem, ...]

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "record_id": self.record_id,
            "status": self.status,
            "receipt_id": self.receipt_id,
            "merged": self.merged,
            "problems": [p.to_dict() for p in self.problems],
        }


@dataclass(frozen=True)
class BatchReport:
    batch_id: str | None
    outcomes: tuple[ItemOutcome, ...]

    @property
    def accepted(self) -> tuple[ItemOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == STATUS_ACCEPTED)

    @property
    def rejected(self) -> tuple[ItemOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == STATUS_REJECTED)

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


def _load_payload(source):
    """读取批量来源：Path 按文件处理，str 按 JSON 文本处理。

    返回 (payload, duplicated_object_ids)。解析时记录含有重复键的
    对象，处理条目时按 id 归属到具体条目，不阻断整批。
    """
    if isinstance(source, Path):
        text = source.read_text(encoding="utf-8")
    elif isinstance(source, str):
        text = source
    else:
        return source, set()

    duplicated: set[int] = set()

    def collect_pairs(pairs):
        keys = [key for key, _ in pairs]
        obj = dict(pairs)
        if len(set(keys)) != len(keys):
            duplicated.add(id(obj))
        return obj

    try:
        payload = json.loads(text, object_pairs_hook=collect_pairs)
    except json.JSONDecodeError as exc:
        raise ProposalRejected(
            [Problem(KIND_INVALID_JSON, None, f"JSON 解析失败: {exc}")]
        )
    return payload, duplicated


def _contains_duplicated(obj, duplicated: set[int]) -> bool:
    """条目内任意层级出现重复键，都归属到该条目。"""
    if id(obj) in duplicated:
        return True
    if isinstance(obj, dict):
        return any(_contains_duplicated(value, duplicated) for value in obj.values())
    if isinstance(obj, list):
        return any(_contains_duplicated(value, duplicated) for value in obj)
    return False


def process_batch(
    source, service: CouncilService, entry: ProposalEntry | None = None
) -> BatchReport:
    """逐条处理批量文件；任何单条失败都不影响其他条目。"""
    entry = entry or ProposalEntry(service.registry, service.domain)
    payload, duplicated = _load_payload(source)

    if isinstance(payload, list):
        batch_id = None
        items = payload
    elif type(payload) is dict:
        unknown = sorted(set(payload) - BATCH_ENVELOPE_FIELDS)
        if unknown:
            raise ProposalRejected(
                [
                    Problem(KIND_UNKNOWN_FIELD, key, f"批量信封之外的字段 {key!r}")
                    for key in unknown
                ]
            )
        if "items" not in payload:
            raise ProposalRejected(
                [Problem(KIND_MISSING_FIELD, "items", "批量信封缺少 items")]
            )
        if not isinstance(payload["items"], list):
            raise ProposalRejected(
                [Problem(KIND_INVALID_FIELD, "items", "items 须为数组")]
            )
        batch_id = payload.get("batch_id")
        items = payload["items"]
    else:
        raise ProposalRejected(
            [Problem(KIND_NOT_PLAIN_OBJECT, None, "批量文件须为数组或信封对象")]
        )

    outcomes: list[ItemOutcome] = []
    for index, item in enumerate(items):
        record_id = None
        if type(item) is dict and isinstance(item.get("record_id"), str):
            record_id = item["record_id"]

        if _contains_duplicated(item, duplicated):
            outcomes.append(
                ItemOutcome(
                    index,
                    record_id,
                    STATUS_REJECTED,
                    None,
                    False,
                    (Problem(KIND_DUPLICATE_FIELD, None, "条目内存在重复字段"),),
                )
            )
            continue

        parsed = entry.parse(item)
        if parsed.problems:
            outcomes.append(
                ItemOutcome(index, record_id, STATUS_REJECTED, None, False, parsed.problems)
            )
            continue
        try:
            result = service.submit(parsed.proposal)
        except ProposalRejected as exc:
            outcomes.append(
                ItemOutcome(index, record_id, STATUS_REJECTED, None, False, exc.problems)
            )
            continue
        except CouncilError as exc:
            outcomes.append(
                ItemOutcome(
                    index,
                    record_id,
                    STATUS_REJECTED,
                    None,
                    False,
                    (Problem(exc.kind, None, exc.detail),),
                )
            )
            continue
        outcomes.append(
            ItemOutcome(
                index, record_id, STATUS_ACCEPTED, result.receipt_id, result.merged, ()
            )
        )
    return BatchReport(batch_id, tuple(outcomes))
