"""提案读取与审议过程中可区分的问题类型。

每个问题都带 ``kind`` 机器可读代码，调用方据此分流处理，
而不是靠解析错误文案。读取入口要求的未知字段、错误领域、
非法修订各有独立种类，互不混用。
"""

from __future__ import annotations

from dataclasses import dataclass

# 载荷不是普通 JSON 对象（数组、标量或映射子类）
KIND_NOT_PLAIN_OBJECT = "not_plain_object"
# 同一对象内出现重复键，取值有歧义
KIND_DUPLICATE_FIELD = "duplicate_field"
# 出现合同之外的字段
KIND_UNKNOWN_FIELD = "unknown_field"
# 缺少合同要求的字段
KIND_MISSING_FIELD = "missing_field"
# 字段类型或格式不符（如口径标识未知、时间无法解析）
KIND_INVALID_FIELD = "invalid_field"
# schema_version 不是本入口支持的版本
KIND_UNSUPPORTED_SCHEMA = "unsupported_schema"
# 记录属于其他业务领域或未登记领域
KIND_WRONG_DOMAIN = "wrong_domain"
# 修订号不是正整数，或与该 record_id 已受理的修订衔接不上
KIND_INVALID_REVISION = "invalid_revision"
# 时间缺少时区偏移
KIND_NAIVE_TIME = "naive_time"
# 领域、口径、来源版本、适用地区、统计周期、带时区时间之间相互矛盾
KIND_INCONSISTENT = "inconsistent"
# 文本本身不是合法 JSON
KIND_INVALID_JSON = "invalid_json"


@dataclass(frozen=True)
class Problem:
    """一条可区分的校验问题。"""

    kind: str
    field: str | None
    detail: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "field": self.field, "detail": self.detail}


class ProposalRejected(Exception):
    """提案读取或收件入口拒绝构造提案时抛出，携带全部问题。"""

    def __init__(self, problems):
        self.problems = tuple(problems)
        summary = "; ".join(
            f"{p.kind}({p.field})" if p.field else p.kind for p in self.problems
        )
        super().__init__(summary or "proposal rejected")
