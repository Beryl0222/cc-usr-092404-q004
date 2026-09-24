"""口径注册表：业务领域、口径、来源版本与适用地区的登记信息。

提案读取入口只能校验"登记过"的事实，因此领域、口径、来源、
适用地区、统计频率都集中登记在这里。其他业务域（如农业普查）
也登记在册，这样"domain 写成其他业务"的记录才能被识别为
错误领域，而不是被当成未知输入放行。
"""

from __future__ import annotations

from dataclasses import dataclass

FREQUENCIES = ("annual", "quarterly", "monthly")


@dataclass(frozen=True)
class SourceProfile:
    """一个登记来源及其已登记的版本号。"""

    source_id: str
    versions: tuple[str, ...]


@dataclass(frozen=True)
class CaliberProfile:
    """一个统计口径的登记档案。"""

    caliber_id: str
    domain: str
    frequency: str  # annual / quarterly / monthly
    regions: frozenset[str]  # 适用地区代码
    sources: tuple[SourceProfile, ...]

    def source(self, source_id: str) -> SourceProfile | None:
        for profile in self.sources:
            if profile.source_id == source_id:
                return profile
        return None


class DomainRegistry:
    """按领域索引的口径登记簿。"""

    def __init__(self, calibers):
        self._by_domain: dict[str, dict[str, CaliberProfile]] = {}
        for caliber in calibers:
            self._by_domain.setdefault(caliber.domain, {})[
                caliber.caliber_id
            ] = caliber

    @property
    def domains(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_domain))

    def knows_domain(self, domain: str) -> bool:
        return domain in self._by_domain

    def caliber(self, domain: str, caliber_id: str) -> CaliberProfile | None:
        return self._by_domain.get(domain, {}).get(caliber_id)

    def find_caliber(self, caliber_id: str) -> CaliberProfile | None:
        """在全部领域中查找口径，用于识别"口径属于其他业务域"。"""
        for calibers in self._by_domain.values():
            if caliber_id in calibers:
                return calibers[caliber_id]
        return None


def default_registry() -> DomainRegistry:
    """文化统计处当前受理的口径，以及用于边界识别的其他业务域。"""
    return DomainRegistry(
        [
            CaliberProfile(
                caliber_id="culture.value_added",
                domain="metric_council",
                frequency="annual",
                regions=frozenset({"000000", "110000", "310000", "440000"}),
                sources=(
                    SourceProfile("yearbook", ("yb2024", "yb2025")),
                    SourceProfile("admin", ("ad1",)),
                ),
            ),
            CaliberProfile(
                caliber_id="culture.services_revenue",
                domain="metric_council",
                frequency="quarterly",
                regions=frozenset({"000000", "110000", "310000", "440000"}),
                sources=(SourceProfile("survey", ("sv1", "sv2")),),
            ),
            # 其他业务域：仅用于识别"投错门"的记录，本处不受理。
            CaliberProfile(
                caliber_id="agri.output_value",
                domain="agri_census",
                frequency="annual",
                regions=frozenset({"000000"}),
                sources=(SourceProfile("farm", ("f1",)),),
            ),
        ]
    )
