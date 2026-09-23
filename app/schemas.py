"""Pydantic 模型：请求与响应结构。

所有频率均为整数 Hz。任一字段校验失败都会使整个请求被拒绝（整批拒绝），
服务端不会写入任何部分数据。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class GuardBand(BaseModel):
    """保护频段，闭区间 [start_hz, end_hz]。"""

    start_hz: int = Field(..., gt=0, description="频段起点（含），整数 Hz")
    end_hz: int = Field(..., gt=0, description="频段终点（含），整数 Hz")

    @model_validator(mode="after")
    def _check_order(self) -> "GuardBand":
        if self.end_hz <= self.start_hz:
            raise ValueError("guard band end_hz must be greater than start_hz")
        return self


class SiteCreate(BaseModel):
    """创建站点（频率方案）的请求体。"""

    name: str = Field(..., min_length=1, max_length=128, description="方案名称，全局唯一")
    scan_start_hz: int = Field(..., gt=0, description="扫描范围起点（含），整数 Hz")
    scan_end_hz: int = Field(..., gt=0, description="扫描范围终点（含），整数 Hz")
    guard_interval_hz: int = Field(
        ..., ge=0, description="保护间隔：产物到命中对象的距离小于该值判定为违规"
    )
    carriers_hz: list[int] = Field(..., min_length=1, description="载频列表，整数 Hz，不允许重复")
    guard_bands: list[GuardBand] = Field(default_factory=list, description="保护频段列表")

    @field_validator("carriers_hz")
    @classmethod
    def _check_carriers(cls, v: list[int]) -> list[int]:
        if any(f <= 0 for f in v):
            raise ValueError("carrier frequencies must be positive integers (Hz)")
        if len(set(v)) != len(v):
            raise ValueError("duplicate carrier frequencies are not allowed")
        return v

    @model_validator(mode="after")
    def _check_scan(self) -> "SiteCreate":
        if self.scan_end_hz <= self.scan_start_hz:
            raise ValueError("scan_end_hz must be greater than scan_start_hz")
        return self


class SiteOut(BaseModel):
    id: int
    name: str
    scan_start_hz: int
    scan_end_hz: int
    guard_interval_hz: int
    carriers_hz: list[int]
    guard_bands: list[GuardBand]
    created_at: str


class SourcePair(BaseModel):
    """一个来源组合：2*fa_hz - fb_hz = 产物频率。"""

    fa_hz: int
    fb_hz: int


class HitObject(BaseModel):
    """产物命中的最近对象：某个载频或某个保护频段。"""

    type: Literal["carrier", "guard_band"]
    carrier_hz: int | None = None
    guard_band: GuardBand | None = None


class ProductOut(BaseModel):
    freq_hz: int = Field(..., description="互调产物频率，整数 Hz")
    distance_hz: int = Field(..., description="到命中对象的最短距离，整数 Hz")
    hit: HitObject
    violates_guard_interval: bool = Field(..., description="distance_hz < guard_interval_hz")
    sources: list[SourcePair] = Field(..., description="产生该频率的全部来源组合")


class AnalysisResult(BaseModel):
    analysis_id: int
    site_id: int
    created_at: str
    product_count: int
    products: list[ProductOut]


class AnalysisMeta(BaseModel):
    analysis_id: int
    site_id: int
    created_at: str
    product_count: int


class BatchAnalysisRequest(BaseModel):
    """多站点原子批量分析请求：1～10 个不重复的已保存站点 ID。"""

    site_ids: list[int] = Field(
        ...,
        min_length=1,
        max_length=10,
        description="已保存站点 ID 列表，取 1～10 个正整数，不允许重复；分析结果按输入顺序返回",
    )

    @field_validator("site_ids")
    @classmethod
    def _check_site_ids(cls, v: list[int]) -> list[int]:
        if any(site_id <= 0 for site_id in v):
            raise ValueError("site ids must be positive integers")
        if len(set(v)) != len(v):
            raise ValueError("duplicate site ids are not allowed")
        return v


class RetentionResult(BaseModel):
    """分析历史保留清理结果：按 (created_at, id) 倒序保留最近 keep 条后的删除情况。"""

    site_id: int
    keep: int = Field(..., description="保留的最近分析条数（1～100）")
    deleted_analysis_ids: list[int] = Field(
        ..., description="被删除的旧分析记录 ID，按 id 升序"
    )
    remaining_count: int = Field(
        ..., description="清理后该站点剩余的分析记录总数"
    )


class RiskHistoryEntry(BaseModel):
    """站点最近 N 条已保存分析中，某曾违规产物频率的历史风险统计。"""

    freq_hz: int = Field(..., description="曾违规的互调产物频率，整数 Hz；结果按该字段升序")
    violation_count: int = Field(
        ...,
        description="最近 N 条分析中该频率违规（violates_guard_interval=true）的分析条数",
    )
    last_violated_at: str = Field(
        ..., description="该频率最近一次违规所在分析的创建时间（created_at）"
    )
    consecutive_count: int = Field(
        ...,
        description=(
            "从最新一条分析起向旧逐条统计的连续违规条数；任一条分析中该频率未违规"
            "（产物不出现或 violates_guard_interval=false）即终止计数，可为 0"
        ),
    )


class RiskGroup(BaseModel):
    """一个风险组：违规产物与其来源载频构成二部图后的一个连通分量。"""

    group_id: int = Field(..., description="组编号：按 (最小载频, 最小产物频率) 排序后从 1 连续编号")
    carriers_hz: list[int] = Field(..., description="组内全部来源载频（fa_hz/fb_hz），升序")
    product_freqs_hz: list[int] = Field(..., description="组内违规产物频率，升序")
    source_pair_count: int = Field(..., description="组内全部产物的来源组合总数")
    distance_hz: int = Field(..., description="组内产物到命中对象的最小距离")


class MitigationPlan(BaseModel):
    """违规产物停用方案：在停用载频数上限内精确求解最小停用集合（最小点覆盖）。

    判定规则：某违规产物的每个来源组合 (fa_hz, fb_hz) 中至少一个载频被停用，
    该产物才算被消除。feasible=true 时给出基数最小的停用集合，多个最小解按
    载频升序列表的字典序择优；feasible=false 时给出上限内消除违规产物最多的
    尽力方案及仍未消除的产物频率。
    """

    analysis_id: int
    max_disabled: int = Field(..., description="本次求解允许停用的最大载频数（1～8）")
    feasible: bool = Field(..., description="上限内是否存在可消除全部违规产物的停用集合")
    disabled_carriers_hz: list[int] = Field(
        ...,
        description="建议停用的载频（升序）。feasible=true 时为基数最小、字典序择优的精确解；"
        "feasible=false 时为上限内消除违规产物最多（并列时覆盖来源组合最多、再按字典序）的方案",
    )
    eliminated_product_freqs_hz: list[int] = Field(
        ..., description="该方案下可消除的违规产物频率（升序）；feasible=true 时为全部违规产物"
    )
    remaining_product_freqs_hz: list[int] = Field(
        ..., description="该方案下仍未消除的违规产物频率（升序）；feasible=true 时恒为空列表"
    )


class ExclusionRange(BaseModel):
    """一段不宜新增载频的闭区间 [start_hz, end_hz]（整数 Hz）。"""

    start_hz: int = Field(..., description="区间起点（含），整数 Hz")
    end_hz: int = Field(..., description="区间终点（含），整数 Hz")


class CarrierExclusionResult(BaseModel):
    """候选范围内不宜新增载频区间的最小并集（不执行也不保存分析）。"""

    site_id: int
    candidate_start_hz: int = Field(..., description="候选范围起点（含），整数 Hz")
    candidate_end_hz: int = Field(..., description="候选范围终点（含），整数 Hz")
    exclusion_ranges: list[ExclusionRange] = Field(
        ..., description="不宜新增载频的闭区间，按起点升序，互不重叠且互不相邻"
    )


# ------------------------------------------------ 单载频迁移规划

class CarrierRelocationRequest(BaseModel):
    """单载频迁移规划请求：待替换载频与候选整数闭区间。"""

    old_hz: int = Field(..., gt=0, description="待替换的现有载频，整数 Hz")
    start_hz: int = Field(..., gt=0, description="候选区间起点（含），整数 Hz")
    end_hz: int = Field(..., gt=0, description="候选区间终点（含），整数 Hz")

    @model_validator(mode="after")
    def _check_order(self) -> "CarrierRelocationRequest":
        if self.end_hz < self.start_hz:
            raise ValueError("end_hz must not be less than start_hz")
        return self


class CarrierRelocationResult(BaseModel):
    """单载频迁移规划结果：距 old_hz 最近的可行替换频率；不修改站点、不保存分析。"""

    site_id: int
    old_hz: int = Field(..., description="待替换的现有载频，整数 Hz")
    start_hz: int = Field(..., description="候选区间起点（含），整数 Hz")
    end_hz: int = Field(..., description="候选区间终点（含），整数 Hz")
    feasible: bool = Field(..., description="候选区间内是否存在可行替换位置")
    new_hz: int | None = Field(
        ...,
        description="距 old_hz 最近的可行频率（等距取较小值），整数 Hz；无可行位置时为 null",
    )
    distance_hz: int | None = Field(
        ...,
        description="new_hz 与 old_hz 的距离 |new_hz - old_hz|，整数 Hz；无可行位置时为 null",
    )


# ------------------------------------------------ 载频漂移风险评估

class CarrierTolerance(BaseModel):
    """单个载频的漂移容差条目。"""

    carrier_hz: int = Field(..., gt=0, description="载频，整数 Hz")
    tolerance_hz: int = Field(
        ..., ge=0, le=1_000_000, description="漂移容差：0～1000000 的整数 Hz"
    )


class DriftRiskRequest(BaseModel):
    """漂移风险评估请求：为该站点每个载频给出唯一且完整的容差映射。"""

    tolerances: list[CarrierTolerance] = Field(
        ...,
        min_length=1,
        description="每个载频一条容差条目；须与站点载频一一对应，不得缺失、重复或多余",
    )

    @field_validator("tolerances")
    @classmethod
    def _check_unique_carriers(cls, v: list[CarrierTolerance]) -> list[CarrierTolerance]:
        carriers = [t.carrier_hz for t in v]
        if len(set(carriers)) != len(carriers):
            raise ValueError("duplicate carrier_hz in tolerances")
        return v


class DriftRiskEntry(BaseModel):
    """一个有序来源组合在独立漂移下的潜在互调风险。"""

    source: SourcePair = Field(
        ..., description="有序来源组合 (fa_hz, fb_hz)，产物为 2*fa_hz - fb_hz"
    )
    nominal_freq_hz: int = Field(..., description="标称产物频率 2*fa_hz - fb_hz，整数 Hz")
    reachable_start_hz: int = Field(..., description="漂移下产物可达区间起点（含），整数 Hz")
    reachable_end_hz: int = Field(..., description="漂移下产物可达区间终点（含），整数 Hz")
    min_distance_hz: int = Field(
        ..., description="全部漂移组合下产物到任一载频或保护频段的最小可能距离，整数 Hz"
    )
    hits: list[HitObject] = Field(
        ...,
        description="取得该最小距离的全部并列命中对象：载频升序在前，"
        "保护频段按 (start_hz, end_hz) 升序在后",
    )


class DriftRiskResult(BaseModel):
    """漂移风险评估结果：仅含最小可能距离严格小于保护间隔的来源；不保存分析。"""

    site_id: int
    guard_interval_hz: int = Field(..., description="判定阈值：站点配置的保护间隔，整数 Hz")
    risk_count: int = Field(..., description="风险来源数量，等于 risks 的长度")
    risks: list[DriftRiskEntry] = Field(
        ..., description="风险来源列表，按 (标称频率, fa_hz, fb_hz) 升序，稳定可复现"
    )


# ------------------------------------------------ 跨站点冲突检测

class CrossSiteConflictsRequest(BaseModel):
    """跨站点互调冲突检测请求：2～8 个已保存站点 ID 与频率容差。"""

    site_ids: list[int] = Field(
        ..., min_length=2, max_length=8, description="已保存站点 ID 列表，取 2～8 个，不允许重复"
    )
    tolerance_hz: int = Field(
        ...,
        ge=0,
        description="频率容差（整数 Hz）：不同站点产物频率差不超过该值即判为冲突配对；0 表示仅同频配对",
    )

    @field_validator("site_ids")
    @classmethod
    def _check_site_ids(cls, v: list[int]) -> list[int]:
        if any(site_id <= 0 for site_id in v):
            raise ValueError("site ids must be positive integers")
        if len(set(v)) != len(v):
            raise ValueError("duplicate site ids are not allowed")
        return v


class ConflictProduct(BaseModel):
    """冲突簇中的一个产物节点：来自某站点最近一次分析的一条产物。"""

    site_id: int = Field(..., description="产物所属站点 ID")
    freq_hz: int = Field(..., description="产物频率，整数 Hz")
    distance_hz: int = Field(..., description="该产物在其所属站点分析中到命中对象的最短距离")


class ConflictPair(BaseModel):
    """一条跨站点冲突配对：两个不同站点的产物频率差不超过容差。"""

    site_a_id: int = Field(..., description="配对中较小的站点 ID")
    product_a_freq_hz: int = Field(..., description="站点 A 的产物频率，整数 Hz")
    site_b_id: int = Field(..., description="配对中较大的站点 ID")
    product_b_freq_hz: int = Field(..., description="站点 B 的产物频率，整数 Hz")
    frequency_delta_hz: int = Field(
        ..., description="两个产物的频率差绝对值 |fa - fb|，不超过请求容差"
    )


class ConflictCluster(BaseModel):
    """一个冲突簇：跨站点冲突配对图上的一个连通分量。

    节点为各站点产物 (site_id, freq_hz)，仅在不同站点的近频产物之间连边；
    同一站点的产物即使频率相近也不直接相连，只能经其他站点的配对链式传递归簇。
    """

    cluster_id: int = Field(
        ..., description="簇编号：按簇内最小 (站点 ID, 产物频率) 节点升序后从 1 连续编号"
    )
    site_ids: list[int] = Field(..., description="簇内涉及的全部站点 ID，升序去重")
    products: list[ConflictProduct] = Field(
        ..., description="簇内全部产物节点，按 (site_id, freq_hz) 升序"
    )
    min_distance_hz: int = Field(
        ..., description="簇内全部产物到各自命中对象的最小距离（distance_hz 的最小值）"
    )
    pair_count: int = Field(..., description="簇内冲突配对数量，等于 pairs 的长度")
    pairs: list[ConflictPair] = Field(
        ...,
        description="簇内全部跨站点配对明细，按 (站点 A, 产物 A, 站点 B, 产物 B) 升序",
    )


class SiteAnalysisRef(BaseModel):
    """本次检测实际读取的站点最近一次分析的引用。"""

    site_id: int
    analysis_id: int = Field(..., description="该站点最近一次（按分析 id 最大）分析记录 ID")


class CrossSiteConflictsResponse(BaseModel):
    """跨站点冲突检测结果：无冲突时 clusters 为空列表。"""

    site_ids: list[int] = Field(..., description="回显请求中的站点 ID（保持请求顺序）")
    tolerance_hz: int
    analyses: list[SiteAnalysisRef] = Field(
        ..., description="各站点实际使用的最近一次分析引用，与请求 site_ids 同序"
    )
    cluster_count: int = Field(..., description="冲突簇数量，等于 clusters 的长度")
    clusters: list[ConflictCluster] = Field(..., description="冲突簇列表，cluster_id 从 1 连续编号")
