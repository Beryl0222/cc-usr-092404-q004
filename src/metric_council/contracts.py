"""文化指标口径审议的领域数据合同与严格读取入口。

本模块只做合同层的事：

* :func:`load_proposal` 只接受 *普通 JSON 对象自身* 的字段——顶层必须是
  ``dict``，拒绝数组、标量、``null``，也不接受嵌套对象/数组；
* 未知字段、错误领域、非法修订分别抛出可区分的异常；
* 领域（``domain``）、口径标识（``caliber_id``）、来源版本（``source`` +
  ``source_version``）、适用地区（``region``）、统计周期（``period``）与带
  时区时间（``occurred_at``）必须相互一致，否则拒绝构造。

业务流程（收件、审议、修订、回算）见 :mod:`metric_council.pipeline`。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

#: 当前文化统计域标识；其他业务条线的记录一律拒收。
DOMAIN = "metric_council"

#: 当前合同版本。v1 为历史最小合同；v1 记录读取时必须补齐 v2 一致性字段。
CURRENT_SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})

#: 合法统计周期；周期决定 :attr:`MetricProposal.period_start` 的对齐起点。
PERIODS = frozenset({"monthly", "quarterly", "annual"})

#: 文化统计统一报送时区（UTC+8）；带时区时间一律换算到此时区对齐周期。
REPORTING_TZ = timezone(timedelta(hours=8))

#: 口径标识：``cul_`` 前缀表示文化统计域，防止其他域标识混入。
_CALIBER_RE = re.compile(r"^cul_[a-z0-9]+(?:_[a-z0-9]+)*$")
#: 适用地区：CN 或 CN-XX（省级行政区）。
_REGION_RE = re.compile(r"^CN(?:-[A-Z]{2})?$")
#: 版本号：1 / 1.2 / 1.2.3，每段为非负整数且无前导零。
_VERSION_RE = re.compile(r"^(0|[1-9]\d*)(\.(0|[1-9]\d*)){0,2}$")
#: 提交部门代码。
_DEPARTMENT_RE = re.compile(r"^dept_[a-z0-9_]+$")

_CN_REGIONS = frozenset(
    {
        "CN",
        "CN-BJ", "CN-TJ", "CN-HE", "CN-SX", "CN-NM",
        "CN-LN", "CN-JL", "CN-HL",
        "CN-SH", "CN-JS", "CN-ZJ", "CN-AH", "CN-FJ", "CN-JX", "CN-SD",
        "CN-HA", "CN-HB", "CN-HN",
        "CN-GD", "CN-GX", "CN-HI",
        "CN-CQ", "CN-SC", "CN-GZ", "CN-YN", "CN-XZ",
        "CN-SN", "CN-GS", "CN-QH", "CN-NX", "CN-XJ",
    }
)

#: 合同全部已知字段；出现以外的字段即为未知字段。
_CANONICAL_FIELDS = frozenset(
    {
        # 标识与一致性字段
        "schema_version", "record_id", "domain", "occurred_at", "revision",
        "source", "caliber_id", "source_version", "region", "period",
        # 提案材料字段
        "title", "definition", "denominator", "price_basis",
        "constant_base_year", "source_authorization", "department",
    }
)

#: 必填的非空字段（constant_base_year 仅可比价时需要）。
# 注：各字段在 parse_proposal 中按语义逐项校验。


class ContractError(ValueError):
    """合同错误基类，所有读取入口的拒收都派生自它。"""

    def __init__(self, reason: str, *, record_id: str | None = None) -> None:
        self.reason = reason
        self.record_id = record_id
        super().__init__(reason if record_id is None else f"[{record_id}] {reason}")


class PayloadShapeError(ContractError):
    """载荷不是普通 JSON 对象（数组/标量/``null``），或含嵌套对象/数组。"""


class UnknownFieldError(ContractError):
    """出现合同之外的未知字段。"""

    def __init__(self, unknown: list[str], *, record_id: str | None = None) -> None:
        self.unknown = list(unknown)
        super().__init__(f"未知字段: {', '.join(sorted(unknown))}", record_id=record_id)


class DomainMismatchError(ContractError):
    """领域不是文化统计域，或口径标识/来源归属与领域相互矛盾。"""


class IllegalRevisionError(ContractError):
    """修订号非法：非正整数、回退或与期望修订号不符（跳跃/重复提交）。"""


class InconsistentRecordError(ContractError):
    """领域内字段（口径/来源版本/地区/周期/带时区时间/价格口径）相互不一致。"""


class SchemaVersionError(ContractError):
    """合同版本不受支持或类型非法。"""


class PriceBasis(str, Enum):
    """价格口径：名义价 / 可比价（必须注明基准年）。"""

    NOMINAL = "nominal"
    CONSTANT = "constant"


@dataclass(frozen=True)
class SourceAuthorization:
    """来源授权材料，与定义/分母/价格口径一样独立版本化。"""

    authority: str
    document: str
    version: str

    def __str__(self) -> str:
        return f"{self.authority}/{self.document}@{self.version}"


@dataclass(frozen=True)
class MetricProposal:
    """一条通过完整合同校验的指标提案记录。

    只暴露已校验字段；构造入口是 :func:`parse_proposal` /
    :func:`load_proposal`，外部不应直接实例化。
    """

    schema_version: int
    record_id: str
    domain: str
    occurred_at: datetime
    revision: int
    source: str
    caliber_id: str
    source_version: str
    region: str
    period: str
    title: str
    definition: str
    denominator: str
    price_basis: PriceBasis
    constant_base_year: int | None
    source_authorization: SourceAuthorization
    department: str
    period_start: str = field(compare=False, default="")

    @property
    def material_key(self) -> tuple[str, ...]:
        """完全相同材料的指纹：口径、标题、定义、分母、价格口径、来源授权。"""
        return (
            self.caliber_id,
            self.title,
            self.definition,
            self.denominator,
            f"{self.price_basis.value}:{self.constant_base_year or ''}",
            str(self.source_authorization),
        )


def _reject_non_object(payload: Any) -> None:
    """只允许普通 JSON 对象：顶层 dict，且每个自身字段都是标量。"""
    if not isinstance(payload, dict):
        raise PayloadShapeError(f"顶层必须是普通 JSON 对象，收到 {type(payload).__name__}")
    for key, value in payload.items():
        if not isinstance(key, str):
            raise PayloadShapeError("字段名必须是字符串")
        if isinstance(value, bool):
            continue  # bool 是合法标量，下面各字段自行决定是否接受
        if isinstance(value, (dict, list)):
            raise PayloadShapeError(
                f"字段 {key!r} 不允许嵌套对象或数组，只接受对象自身的标量字段"
            )


def _check_unknown_fields(payload: dict[str, Any]) -> None:
    unknown = sorted(set(payload) - _CANONICAL_FIELDS)
    if unknown:
        rid = payload.get("record_id")
        raise UnknownFieldError(
            unknown,
            record_id=rid if isinstance(rid, str) else None,
        )


def _require_str(payload: dict[str, Any], name: str, *, record_id: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InconsistentRecordError(f"字段 {name} 必须是非空字符串", record_id=record_id)
    return value.strip()


def _parse_occurred_at(value: Any, *, record_id: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise InconsistentRecordError("occurred_at 必须是非空 ISO 8601 字符串", record_id=record_id)
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise InconsistentRecordError(f"occurred_at 不是合法时间: {exc}", record_id=record_id) from exc
    if moment.tzinfo is None:
        raise InconsistentRecordError(
            "occurred_at 必须显式携带时区偏移（如 2026-09-20T09:00:00+08:00），不接受无时区时间",
            record_id=record_id,
        )
    if not (2000 <= moment.year <= 2100):
        raise InconsistentRecordError("occurred_at 超出文化统计合理时间范围", record_id=record_id)
    return moment


def _period_start(period: str, local: datetime) -> str:
    if period == "monthly":
        month = local.month
    elif period == "quarterly":
        month = ((local.month - 1) // 3) * 3 + 1
    else:  # annual
        month = 1
    return f"{local.year:04d}-{month:02d}"


def _parse_price_basis(payload: dict[str, Any], *, record_id: str) -> tuple[PriceBasis, int | None]:
    raw = _require_str(payload, "price_basis", record_id=record_id)
    try:
        basis = PriceBasis(raw)
    except ValueError as exc:
        raise InconsistentRecordError(
            f"价格口径 {raw!r} 非法，允许 nominal / constant", record_id=record_id
        ) from exc
    base_year = payload.get("constant_base_year")
    if basis is PriceBasis.CONSTANT:
        if not isinstance(base_year, int) or isinstance(base_year, bool) or not (1900 <= base_year <= 2100):
            raise InconsistentRecordError(
                "可比价(constant)必须在 constant_base_year 给出 1900..2100 的整数基准年",
                record_id=record_id,
            )
        return basis, base_year
    if base_year is not None:
        raise InconsistentRecordError(
            "名义价(nominal)不得携带 constant_base_year，价格口径与基准年不一致",
            record_id=record_id,
        )
    return basis, None


def _parse_authorization(payload: dict[str, Any], *, record_id: str) -> SourceAuthorization:
    # 扁平自身字段编码："授权机构/文件号@版本"，不引入嵌套对象。
    raw = _require_str(payload, "source_authorization", record_id=record_id)
    m = re.fullmatch(r"([^/@]+)/([^@]+)@(.+)", raw)
    if not m:
        raise InconsistentRecordError(
            "source_authorization 格式应为 '授权机构/文件号@版本'",
            record_id=record_id,
        )
    authority, document, version = (part.strip() for part in m.groups())
    if not _VERSION_RE.fullmatch(version):
        raise InconsistentRecordError(f"来源授权版本号非法: {version!r}", record_id=record_id)
    return SourceAuthorization(authority=authority, document=document, version=version)


def parse_proposal(payload: Any, *, expected_revision: int | None = None) -> MetricProposal:
    """把已反序列化的 JSON 数据严格构造成 :class:`MetricProposal`。

    ``expected_revision`` 供审议/收件入口复核修订链：传入本次应有的修订号时，
    回退、跳跃或重复提交抛 :class:`IllegalRevisionError`。

    校验顺序刻意安排为：形状 → 未知字段 → 版本 → 修订 → 领域 → 领域内一致性，
    调用方可以按异常类型区分拒收原因。
    """

    _reject_non_object(payload)
    rid = payload.get("record_id")
    record_id = rid if isinstance(rid, str) and rid.strip() else None

    _check_unknown_fields(payload)

    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise SchemaVersionError("schema_version 必须是整数", record_id=record_id)
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise SchemaVersionError(
            f"不支持的合同版本 {schema_version}，仅支持 {sorted(SUPPORTED_SCHEMA_VERSIONS)}",
            record_id=record_id,
        )

    rid_text = record_id or "?"
    revision = payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
        raise IllegalRevisionError("revision 必须是正整数", record_id=record_id)
    if expected_revision is not None and revision != expected_revision:
        wording = "回退" if revision < expected_revision else "跳跃或重复提交"
        raise IllegalRevisionError(
            f"修订号{wording}: 收到 {revision}，期望 {expected_revision}",
            record_id=record_id,
        )

    # ---- 领域边界 -------------------------------------------------------
    domain = _require_str(payload, "domain", record_id=rid_text)
    if domain != DOMAIN:
        raise DomainMismatchError(
            f"领域 {domain!r} 不属于文化统计域 {DOMAIN!r}，本入口不得构造为指标提案",
            record_id=rid_text,
        )

    # ---- 领域内相互一致 -------------------------------------------------
    caliber_id = _require_str(payload, "caliber_id", record_id=rid_text)
    if not _CALIBER_RE.fullmatch(caliber_id):
        raise DomainMismatchError(
            f"口径标识 {caliber_id!r} 不属于文化统计域（应以 cul_ 开头、小写蛇形）",
            record_id=rid_text,
        )

    source = _require_str(payload, "source", record_id=rid_text)
    source_version = _require_str(payload, "source_version", record_id=rid_text)
    if not _VERSION_RE.fullmatch(source_version):
        raise InconsistentRecordError(f"来源版本号非法: {source_version!r}", record_id=rid_text)
    # 来源必须显式归属文化域，防止其他业务条线的来源被接入历史序列。
    if not (source.startswith("文化") or source.startswith("cul:")):
        raise DomainMismatchError(
            f"来源 {source!r} 未归属文化统计域，不得接入文化指标历史序列",
            record_id=rid_text,
        )

    region = _require_str(payload, "region", record_id=rid_text)
    if not _REGION_RE.fullmatch(region) or region not in _CN_REGIONS:
        raise InconsistentRecordError(
            f"适用地区 {region!r} 不是合法的文化统计适用地区", record_id=rid_text
        )

    period = _require_str(payload, "period", record_id=rid_text)
    if period not in PERIODS:
        raise InconsistentRecordError(
            f"统计周期 {period!r} 非法，允许: {', '.join(sorted(PERIODS))}",
            record_id=rid_text,
        )

    occurred_at = _parse_occurred_at(payload.get("occurred_at"), record_id=rid_text)
    # 带时区时间换算到文化统计报送时区后对齐周期起点。
    local = occurred_at.astimezone(REPORTING_TZ)
    period_start = _period_start(period, local)
    # 周期与带时区时间必须相互一致：该瞬时在报送时区(UTC+8)与 UTC 下必须归属
    # 同一统计周期。跨周期边界的瞬时（例如 UTC 仍是 3 月 31 日、北京已是 4 月
    # 1 日）在不同时区归期不同，无法无歧义地并入历史序列，必须澄清归属期后重报。
    utc_start = _period_start(period, occurred_at.astimezone(timezone.utc))
    if utc_start != period_start:
        raise InconsistentRecordError(
            f"统计周期 {period} 与带时区时间不一致：该瞬时在报送时区归属 {period_start}，"
            f"在 UTC 归属 {utc_start}，跨越统计周期边界，须澄清归属期后重报",
            record_id=rid_text,
        )

    # ---- 提案材料 -------------------------------------------------------
    title = _require_str(payload, "title", record_id=rid_text)
    definition = _require_str(payload, "definition", record_id=rid_text)
    denominator = _require_str(payload, "denominator", record_id=rid_text)
    department = _require_str(payload, "department", record_id=rid_text)
    if not _DEPARTMENT_RE.fullmatch(department):
        raise InconsistentRecordError(
            f"部门代码 {department!r} 非法（应为 dept_ 前缀的小写蛇形代码）",
            record_id=rid_text,
        )
    price_basis, base_year = _parse_price_basis(payload, record_id=rid_text)
    authorization = _parse_authorization(payload, record_id=rid_text)

    return MetricProposal(
        schema_version=schema_version,
        record_id=rid_text,
        domain=DOMAIN,
        occurred_at=occurred_at,
        revision=revision,
        source=source,
        caliber_id=caliber_id,
        source_version=source_version,
        region=region,
        period=period,
        title=title,
        definition=definition,
        denominator=denominator,
        price_basis=price_basis,
        constant_base_year=base_year,
        source_authorization=authorization,
        department=department,
        period_start=period_start,
    )


def load_proposal(path: str | Path, *, expected_revision: int | None = None) -> MetricProposal:
    """从 JSON 文件严格读取一条指标提案。

    文件内容非法时按具体原因抛出 :mod:`metric_council.contracts` 下的可区分
    异常，而不是像旧入口那样构造出“record_id 合法但领域错误”的对象。
    """

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PayloadShapeError(f"文件无法读取: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PayloadShapeError(f"JSON 解析失败: {exc}") from exc
    return parse_proposal(payload, expected_revision=expected_revision)


# ---- 历史兼容 -------------------------------------------------------------

@dataclass(frozen=True)
class DomainRecord:
    """已废弃的 v1 最小合同，仅为向后兼容保留；新代码用 :class:`MetricProposal`。

    经由 :func:`load_record` 取得的记录同样经过严格领域校验，错误领域无法
    再构造成功。
    """

    schema_version: int
    record_id: str
    domain: str
    occurred_at: str
    revision: int
    source: str


def load_record(path: str | Path) -> DomainRecord:
    """旧入口：内部已升级为严格领域校验。"""

    proposal = load_proposal(path)
    return DomainRecord(
        schema_version=proposal.schema_version,
        record_id=proposal.record_id,
        domain=proposal.domain,
        occurred_at=proposal.occurred_at.isoformat(),
        revision=proposal.revision,
        source=proposal.source,
    )
