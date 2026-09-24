"""指标提案读取入口：只接受普通 JSON 对象，字段须相互一致。

边界规则：

* 载荷必须是普通 JSON 对象（``type(x) is dict``），数组、标量、
  映射子类一律拒绝；只有对象自身字段参与判断，合同之外不补默认值。
* 领域、口径标识、来源版本、适用地区、统计周期、带时区时间
  必须相互一致，任何一对矛盾都会得到 ``inconsistent`` 问题。
* 未知字段、错误领域、非法修订分别返回 ``unknown_field``、
  ``wrong_domain``、``invalid_revision``，调用方可直接区分。
"""

from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .problems import (
    KIND_DUPLICATE_FIELD,
    KIND_INCONSISTENT,
    KIND_INVALID_FIELD,
    KIND_INVALID_JSON,
    KIND_INVALID_REVISION,
    KIND_MISSING_FIELD,
    KIND_NAIVE_TIME,
    KIND_NOT_PLAIN_OBJECT,
    KIND_UNKNOWN_FIELD,
    KIND_UNSUPPORTED_SCHEMA,
    KIND_WRONG_DOMAIN,
    Problem,
    ProposalRejected,
)
from .registry import DomainRegistry, default_registry

SUPPORTED_SCHEMA_VERSION = 1
DEFAULT_DOMAIN = "metric_council"

TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "record_id",
        "domain",
        "caliber_id",
        "region",
        "period",
        "occurred_at",
        "source",
        "source_version",
        "revision",
        "department",
        "material",
    }
)

# 分别版本化的四项口径材料
ASPECT_FIELDS = ("definition", "denominator", "price_basis", "source_authorization")

_STRING_FIELDS = (
    "record_id",
    "domain",
    "caliber_id",
    "region",
    "period",
    "occurred_at",
    "source",
    "source_version",
    "department",
)

_PERIOD_PATTERNS = {
    "annual": re.compile(r"(\d{4})"),
    "quarterly": re.compile(r"(\d{4})-Q([1-4])"),
    "monthly": re.compile(r"(\d{4})-(0[1-9]|1[0-2])"),
}


def period_end_date(frequency: str, period: str) -> date | None:
    """统计周期的最后一日；周期写法与频率不符时返回 None。"""
    pattern = _PERIOD_PATTERNS.get(frequency)
    if pattern is None:
        return None
    match = pattern.fullmatch(period)
    if match is None:
        return None
    year = int(match.group(1))
    if frequency == "annual":
        month = 12
    elif frequency == "quarterly":
        month = int(match.group(2)) * 3
    else:
        month = int(match.group(2))
    return date(year, month, calendar.monthrange(year, month)[1])


def parse_occurred(text: str) -> datetime | None:
    """解析 ISO-8601 时间；无法解析时返回 None。"""
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class Material:
    """提案携带的口径材料，四项内容在收件时分别版本化。"""

    definition: str
    denominator: str
    price_basis: str
    source_authorization: str

    def as_dict(self) -> dict:
        return {
            "definition": self.definition,
            "denominator": self.denominator,
            "price_basis": self.price_basis,
            "source_authorization": self.source_authorization,
        }

    def canonical(self) -> str:
        """用于收件去重的规范化表示。"""
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class Proposal:
    """通过边界校验的指标提案。"""

    schema_version: int
    record_id: str
    domain: str
    caliber_id: str
    region: str
    period: str
    occurred_at: str
    source: str
    source_version: str
    revision: int
    department: str
    material: Material


@dataclass(frozen=True)
class ParseOutcome:
    """读取结果：要么得到提案，要么得到全部可区分问题。"""

    proposal: Proposal | None
    problems: tuple[Problem, ...]

    @property
    def ok(self) -> bool:
        return self.proposal is not None


class ProposalEntry:
    """绑定单一业务领域的提案读取入口。"""

    def __init__(self, registry: DomainRegistry | None = None, domain: str = DEFAULT_DOMAIN):
        self.registry = registry or default_registry()
        self.domain = domain

    def parse(self, payload) -> ParseOutcome:
        """校验已解析的 JSON 载荷，不抛异常。"""
        problems: list[Problem] = []
        if type(payload) is not dict:
            return ParseOutcome(
                None,
                (Problem(KIND_NOT_PLAIN_OBJECT, None, "提案必须是普通 JSON 对象"),),
            )

        for key in sorted(payload):
            if key not in TOP_LEVEL_FIELDS:
                problems.append(
                    Problem(KIND_UNKNOWN_FIELD, key, f"合同之外的字段 {key!r}")
                )
        for key in sorted(TOP_LEVEL_FIELDS - payload.keys()):
            problems.append(Problem(KIND_MISSING_FIELD, key, f"缺少必填字段 {key!r}"))

        schema_version = payload.get("schema_version")
        if "schema_version" in payload:
            if isinstance(schema_version, bool) or not isinstance(schema_version, int):
                problems.append(
                    Problem(KIND_INVALID_FIELD, "schema_version", "schema_version 须为整数")
                )
            elif schema_version != SUPPORTED_SCHEMA_VERSION:
                problems.append(
                    Problem(
                        KIND_UNSUPPORTED_SCHEMA,
                        "schema_version",
                        f"不支持的结构版本 {schema_version!r}",
                    )
                )

        strings: dict[str, str] = {}
        for field in _STRING_FIELDS:
            if field not in payload:
                continue
            value = payload[field]
            if not isinstance(value, str) or not value.strip():
                problems.append(
                    Problem(KIND_INVALID_FIELD, field, f"{field} 须为非空字符串")
                )
            else:
                strings[field] = value

        if "revision" in payload:
            revision = payload["revision"]
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
            ):
                problems.append(
                    Problem(KIND_INVALID_REVISION, "revision", "修订号须为正整数")
                )

        material = self._parse_material(payload, problems)

        domain = strings.get("domain")
        domain_ok = False
        if domain is not None:
            if not self.registry.knows_domain(domain):
                problems.append(
                    Problem(KIND_WRONG_DOMAIN, "domain", f"未登记的业务领域 {domain!r}")
                )
            elif domain != self.domain:
                problems.append(
                    Problem(
                        KIND_WRONG_DOMAIN,
                        "domain",
                        f"记录属于业务领域 {domain!r}，本入口只受理 {self.domain!r}",
                    )
                )
            else:
                domain_ok = True

        self._check_consistency(strings, domain_ok, problems)

        if problems:
            return ParseOutcome(None, tuple(problems))
        return ParseOutcome(
            Proposal(
                schema_version=schema_version,
                record_id=strings["record_id"],
                domain=strings["domain"],
                caliber_id=strings["caliber_id"],
                region=strings["region"],
                period=strings["period"],
                occurred_at=strings["occurred_at"],
                source=strings["source"],
                source_version=strings["source_version"],
                revision=payload["revision"],
                department=strings["department"],
                material=material,
            ),
            (),
        )

    def _parse_material(self, payload: dict, problems: list[Problem]) -> Material | None:
        if "material" not in payload:
            return None
        raw = payload["material"]
        if type(raw) is not dict:
            problems.append(
                Problem(KIND_NOT_PLAIN_OBJECT, "material", "material 必须是普通 JSON 对象")
            )
            return None
        before = len(problems)
        for key in sorted(raw):
            if key not in ASPECT_FIELDS:
                problems.append(
                    Problem(KIND_UNKNOWN_FIELD, f"material.{key}", f"合同之外的字段 {key!r}")
                )
        values: dict[str, str] = {}
        for aspect in ASPECT_FIELDS:
            if aspect not in raw:
                problems.append(
                    Problem(
                        KIND_MISSING_FIELD,
                        f"material.{aspect}",
                        f"缺少口径材料 {aspect!r}",
                    )
                )
                continue
            value = raw[aspect]
            if not isinstance(value, str) or not value.strip():
                problems.append(
                    Problem(
                        KIND_INVALID_FIELD,
                        f"material.{aspect}",
                        f"material.{aspect} 须为非空字符串",
                    )
                )
            else:
                values[aspect] = value
        if len(problems) != before:
            return None
        return Material(**values)

    def _check_consistency(
        self, strings: dict[str, str], domain_ok: bool, problems: list[Problem]
    ) -> None:
        """领域、口径、来源版本、适用地区、统计周期、带时区时间互相印证。"""
        caliber = None
        if domain_ok and "caliber_id" in strings:
            caliber_id = strings["caliber_id"]
            caliber = self.registry.caliber(self.domain, caliber_id)
            if caliber is None:
                elsewhere = self.registry.find_caliber(caliber_id)
                if elsewhere is not None:
                    problems.append(
                        Problem(
                            KIND_INCONSISTENT,
                            "caliber_id",
                            f"口径 {caliber_id!r} 属于业务领域 {elsewhere.domain!r}",
                        )
                    )
                else:
                    problems.append(
                        Problem(KIND_INVALID_FIELD, "caliber_id", f"未知口径标识 {caliber_id!r}")
                    )

        period_end = None
        if caliber is not None and "period" in strings:
            period_end = period_end_date(caliber.frequency, strings["period"])
            if period_end is None:
                problems.append(
                    Problem(
                        KIND_INCONSISTENT,
                        "period",
                        f"统计周期与口径频率 {caliber.frequency} 不一致",
                    )
                )

        if (
            caliber is not None
            and "region" in strings
            and strings["region"] not in caliber.regions
        ):
            problems.append(
                Problem(KIND_INCONSISTENT, "region", "适用地区不在口径登记范围内")
            )

        if caliber is not None and "source" in strings:
            profile = caliber.source(strings["source"])
            if profile is None:
                problems.append(
                    Problem(KIND_INCONSISTENT, "source", "来源未登记于该口径")
                )
            elif (
                "source_version" in strings
                and strings["source_version"] not in profile.versions
            ):
                problems.append(
                    Problem(KIND_INCONSISTENT, "source_version", "来源版本未登记")
                )

        if "occurred_at" in strings:
            occurred = parse_occurred(strings["occurred_at"])
            if occurred is None:
                problems.append(
                    Problem(KIND_INVALID_FIELD, "occurred_at", "时间须为 ISO-8601 格式")
                )
            elif occurred.tzinfo is None or occurred.tzinfo.utcoffset(occurred) is None:
                problems.append(
                    Problem(KIND_NAIVE_TIME, "occurred_at", "时间必须携带时区偏移")
                )
            elif period_end is not None and occurred.date() < period_end:
                problems.append(
                    Problem(
                        KIND_INCONSISTENT,
                        "occurred_at",
                        "发生时间早于统计周期结束",
                    )
                )

    def parse_text(self, text: str) -> ParseOutcome:
        """从 JSON 文本读取；同一对象内的重复键直接判为问题。"""
        duplicates: list[str] = []

        def collect_pairs(pairs):
            keys = [key for key, _ in pairs]
            for key in sorted({k for k in keys if keys.count(k) > 1}):
                duplicates.append(key)
            return dict(pairs)

        try:
            payload = json.loads(text, object_pairs_hook=collect_pairs)
        except json.JSONDecodeError as exc:
            return ParseOutcome(
                None, (Problem(KIND_INVALID_JSON, None, f"JSON 解析失败: {exc}"),)
            )
        if duplicates:
            return ParseOutcome(
                None,
                tuple(
                    Problem(KIND_DUPLICATE_FIELD, key, f"字段 {key!r} 重复出现")
                    for key in duplicates
                ),
            )
        return self.parse(payload)

    def load(self, path) -> Proposal:
        """严格读取单个提案文件；任何问题都抛出 ProposalRejected。"""
        outcome = self.parse_text(Path(path).read_text(encoding="utf-8"))
        if outcome.problems:
            raise ProposalRejected(outcome.problems)
        return outcome.proposal


def load_proposal(
    path, registry: DomainRegistry | None = None, domain: str = DEFAULT_DOMAIN
) -> Proposal:
    """便捷入口：按默认注册表严格读取提案文件。"""
    return ProposalEntry(registry, domain).load(path)
