"""FastAPI 入口：射频三阶互调（2fa - fb）分析 API。"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query, Response, status

from . import database
from .analysis import (
    analyze,
    build_cross_site_conflicts,
    build_mitigation_plan,
    build_risk_groups,
    build_risk_history,
    carrier_exclusion_ranges,
    carrier_relocation,
    drift_risk_assessment,
)
from .schemas import (
    AnalysisMeta,
    AnalysisResult,
    BatchAnalysisRequest,
    CarrierExclusionResult,
    CarrierRelocationRequest,
    CarrierRelocationResult,
    CrossSiteConflictsRequest,
    CrossSiteConflictsResponse,
    DriftRiskRequest,
    DriftRiskResult,
    MitigationPlan,
    ProductOut,
    RetentionResult,
    RiskGroup,
    RiskHistoryEntry,
    SiteAnalysisRef,
    SiteCreate,
    SiteOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()  # 启动时初始化数据库（幂等）
    yield


app = FastAPI(
    title="RF Intermodulation Analysis API",
    summary="射频三阶互调产物（2fa - fb）分析：站点管理、产物枚举与命中评估",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/sites",
    response_model=SiteOut,
    status_code=status.HTTP_201_CREATED,
    tags=["sites"],
    summary="创建站点（频率方案）",
    description="任一载频重复或频段非法时整批拒绝（422），不写入任何数据；名称重复返回 409。",
)
def create_site(payload: SiteCreate) -> SiteOut:
    try:
        site_id = database.insert_site(payload)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"site name already exists: {payload.name}"
        ) from exc
    site = database.fetch_site(site_id)
    assert site is not None
    return site


@app.get("/sites", response_model=list[SiteOut], tags=["sites"], summary="列出全部已保存方案")
def list_sites() -> list[SiteOut]:
    return database.list_sites()


@app.get("/sites/{site_id}", response_model=SiteOut, tags=["sites"], summary="查询单个已保存方案")
def get_site(site_id: int) -> SiteOut:
    site = database.fetch_site(site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"site {site_id} not found")
    return site


@app.get(
    "/sites/{site_id}/carrier-exclusion-ranges",
    response_model=CarrierExclusionResult,
    tags=["sites"],
    summary="候选范围内不宜新增载频的最小区间并集（不执行或保存分析）",
    description=(
        "对候选闭区间内的每个整数 Hz 位置 x：若新增载频 x 会与某个现有载频 c 产生"
        "违规三阶产物（2*x-c 或 2*c-x 落在扫描范围内，且到任一现有载频或保护频段的"
        "距离严格小于保护间隔），或 x 本身是现有载频，则 x 不宜新增。返回全部禁用"
        "位置的最小区间并集：裁剪到候选范围，重叠或相邻区间已合并，按起点升序。"
        "candidate_start_hz 大于 candidate_end_hz 时返回 422。"
    ),
)
def get_carrier_exclusion_ranges(
    site_id: int,
    candidate_start_hz: int = Query(..., gt=0, description="候选范围起点（含），正整数 Hz"),
    candidate_end_hz: int = Query(..., gt=0, description="候选范围终点（含），正整数 Hz"),
) -> CarrierExclusionResult:
    if candidate_start_hz > candidate_end_hz:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "candidate_start_hz must not be greater than candidate_end_hz",
        )
    site = database.fetch_site(site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"site {site_id} not found")
    ranges = carrier_exclusion_ranges(
        carriers_hz=site.carriers_hz,
        scan_start_hz=site.scan_start_hz,
        scan_end_hz=site.scan_end_hz,
        guard_bands=site.guard_bands,
        guard_interval_hz=site.guard_interval_hz,
        candidate_start_hz=candidate_start_hz,
        candidate_end_hz=candidate_end_hz,
    )
    return CarrierExclusionResult(
        site_id=site_id,
        candidate_start_hz=candidate_start_hz,
        candidate_end_hz=candidate_end_hz,
        exclusion_ranges=ranges,
    )


@app.post(
    "/sites/{site_id}/drift-risk",
    response_model=DriftRiskResult,
    tags=["sites"],
    summary="载频独立漂移下的全频域潜在互调风险评估（不保存分析）",
    description=(
        "请求体须为该站点**每个载频**提供唯一且完整的漂移容差 tolerance_hz"
        "（0～1000000 的整数 Hz；映射缺失、重复、多余或容差非法 → 422；"
        "站点不存在 → 404）。把载频漂移建模为共享变量：对每个有序来源 "
        "fa≠fb 计算 2fa−fb 的可达区间 [2fa−fb−(2t_a+t_b), 2fa−fb+(2t_a+t_b)]；"
        "与漂移载频比较时合并同一变量的线性系数（c==fa 时 δ_a 的系数为 "
        "2−1=1），与固定保护频段比较时按区间距离计算，不把相关区间当作独立"
        "区间重复放宽。枚举全部有序来源（全频域，不按扫描范围过滤），仅返回"
        "最小可能距离严格小于 guard_interval_hz 的来源，包含来源、标称频率、"
        "可达区间、最小距离与全部并列命中对象，按 (标称频率, fa_hz, fb_hz) "
        "升序排序。评估结果不保存。"
    ),
)
def evaluate_drift_risk(site_id: int, payload: DriftRiskRequest) -> DriftRiskResult:
    site = database.fetch_site(site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"site {site_id} not found")
    provided = {t.carrier_hz: t.tolerance_hz for t in payload.tolerances}
    expected = set(site.carriers_hz)
    missing = sorted(expected - provided.keys())
    extra = sorted(provided.keys() - expected)
    if missing or extra:
        parts = []
        if missing:
            parts.append(
                "missing tolerance for carrier(s): " + ", ".join(map(str, missing))
            )
        if extra:
            parts.append(
                "tolerance for unknown carrier(s): " + ", ".join(map(str, extra))
            )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "; ".join(parts))
    risks = drift_risk_assessment(
        carriers_hz=site.carriers_hz,
        tolerances_hz=provided,
        guard_bands=site.guard_bands,
        guard_interval_hz=site.guard_interval_hz,
    )
    return DriftRiskResult(
        site_id=site_id,
        guard_interval_hz=site.guard_interval_hz,
        risk_count=len(risks),
        risks=risks,
    )


@app.post(
    "/sites/{site_id}/carrier-relocation",
    response_model=CarrierRelocationResult,
    tags=["sites"],
    summary="单载频迁移规划：求距 old_hz 最近的可行替换频率（不修改站点、不保存分析）",
    description=(
        "将现有载频 old_hz 替换为候选闭区间 [start_hz, end_hz] 内的整数频率 x，"
        "按与分析一致的语义（现有扫描范围、三阶产物 2fa−fb、严格保护间隔）判定"
        "新载频集合：全部产物均不违规且 x 不与其余载频重复时 x 可行。约束全部"
        "反解为关于 x 的线性禁用整数区间并合并（含未变来源产物命中新载频的"
        "情况），不逐 Hz 枚举。返回距 old_hz 最近的可行频率（等距取较小值）；"
        "无可行位置时 feasible=false 且 new_hz 为 null。old_hz 不是该站点载频"
        "或区间无效（start_hz 大于 end_hz）返回 422，站点不存在返回 404。"
        "不修改站点配置，也不保存分析。"
    ),
)
def plan_carrier_relocation(
    site_id: int, payload: CarrierRelocationRequest
) -> CarrierRelocationResult:
    site = database.fetch_site(site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"site {site_id} not found")
    if payload.old_hz not in site.carriers_hz:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"old_hz {payload.old_hz} is not a carrier of site {site_id}",
        )
    new_hz = carrier_relocation(
        carriers_hz=site.carriers_hz,
        old_hz=payload.old_hz,
        scan_start_hz=site.scan_start_hz,
        scan_end_hz=site.scan_end_hz,
        guard_bands=site.guard_bands,
        guard_interval_hz=site.guard_interval_hz,
        start_hz=payload.start_hz,
        end_hz=payload.end_hz,
    )
    return CarrierRelocationResult(
        site_id=site_id,
        old_hz=payload.old_hz,
        start_hz=payload.start_hz,
        end_hz=payload.end_hz,
        feasible=new_hz is not None,
        new_hz=new_hz,
        distance_hz=abs(new_hz - payload.old_hz) if new_hz is not None else None,
    )


@app.post(
    "/sites/{site_id}/analysis",
    response_model=AnalysisResult,
    status_code=status.HTTP_201_CREATED,
    tags=["analysis"],
    summary="对方案执行三阶互调分析并保存结果",
    description=(
        "执行分析并保存结果，返回 201。\n\n"
        "可选请求头 **Idempotency-Key**：提供后按 (站点, 键) 原子登记请求——"
        "首次请求执行分析并保存；同一站点重复使用该键时不再次执行分析、"
        "不新增分析记录，直接返回首次结果（200）；并发的同键请求也只会产生"
        "一份分析结果。不同站点使用相同键互不影响。键为空白（空字符串或仅含"
        "空白字符）返回 422。未提供该头时行为不变：每次请求都执行并保存一次"
        "新的分析（201）。站点不存在返回 404。"
    ),
    responses={
        200: {"model": AnalysisResult, "description": "命中 Idempotency-Key，返回首次分析结果"},
        201: {"model": AnalysisResult, "description": "首次执行并保存分析结果"},
    },
)
def run_analysis(
    site_id: int,
    response: Response,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description=(
            "可选幂等键：同一站点重复使用相同键直接返回首次结果（200），"
            "不新增分析记录；空白键返回 422"
        ),
    ),
) -> AnalysisResult:
    if idempotency_key is not None and not idempotency_key.strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Idempotency-Key header must not be blank",
        )
    site = database.fetch_site(site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"site {site_id} not found")

    def compute_products() -> list[ProductOut]:
        return analyze(
            carriers_hz=site.carriers_hz,
            scan_start_hz=site.scan_start_hz,
            scan_end_hz=site.scan_end_hz,
            guard_bands=site.guard_bands,
            guard_interval_hz=site.guard_interval_hz,
        )

    result, created = database.save_analysis_once(
        site_id, idempotency_key, compute_products
    )
    response.status_code = (
        status.HTTP_201_CREATED if created else status.HTTP_200_OK
    )
    return result


@app.post(
    "/sites/analyses/batch",
    response_model=list[AnalysisResult],
    status_code=status.HTTP_201_CREATED,
    tags=["analysis"],
    summary="多站点原子批量分析：按输入顺序逐站执行并保存",
    description=(
        "接收 1～10 个**不重复**的已保存站点 ID（正整数；数量越界、重复或"
        "非正 → 422），按输入顺序逐站执行与 `POST /sites/{site_id}/analysis`"
        "完全相同的分析逻辑并保存结果。\n\n"
        "**整批原子**：站点存在性检查、各站分析计算与全部分析记录写入都在"
        "**同一个 SQLite 事务**内完成——任一站点不存在即整批返回 404 且不"
        "执行分析、不保存任何记录；任一计算或写入步骤失败则整批回滚，不会"
        "出现部分站点已保存的中间状态。成功时返回 `201` 与**顺序与请求"
        "`site_ids` 一致**的分析结果列表（每个站点新增一条分析记录，不支持"
        "`Idempotency-Key` 幂等登记；单站接口行为不受影响）。"
    ),
)
def run_analyses_batch(
    payload: BatchAnalysisRequest,
) -> list[AnalysisResult]:
    site_ids = payload.site_ids

    def compute_for_site(site: SiteOut) -> list[ProductOut]:
        return analyze(
            carriers_hz=site.carriers_hz,
            scan_start_hz=site.scan_start_hz,
            scan_end_hz=site.scan_end_hz,
            guard_bands=site.guard_bands,
            guard_interval_hz=site.guard_interval_hz,
        )

    results = database.save_analyses_batch(site_ids, compute_for_site)
    if results is None:
        existing = database.fetch_existing_site_ids(site_ids)
        missing = sorted(set(site_ids) - existing)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "site(s) not found: " + ", ".join(str(s) for s in missing),
        )
    return results


@app.get(
    "/sites/{site_id}/analysis",
    response_model=AnalysisResult,
    tags=["analysis"],
    summary="查询该方案最近一次分析结果",
)
def get_latest_analysis(site_id: int) -> AnalysisResult:
    if database.fetch_site(site_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"site {site_id} not found")
    result = database.fetch_latest_analysis(site_id)
    if result is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no analysis for site {site_id} yet; POST /sites/{site_id}/analysis first",
        )
    return result


@app.get(
    "/sites/{site_id}/analyses",
    response_model=list[AnalysisMeta],
    tags=["analysis"],
    summary="列出该方案的全部分析记录",
)
def list_site_analyses(site_id: int) -> list[AnalysisMeta]:
    if database.fetch_site(site_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"site {site_id} not found")
    return database.list_analyses(site_id)


@app.get(
    "/sites/{site_id}/risk-history",
    response_model=list[RiskHistoryEntry],
    tags=["analysis"],
    summary="站点历史风险：最近 N 条已保存分析中各曾违规产物频率的违规统计",
    description=(
        "只读已保存分析结果，**不触发新分析**：按 **created_at、id 倒序**取该站点最近 "
        "`limit` 条已保存分析（created_at 并列时以 id 较大者为更新），对在这些分析中"
        "**至少违规过一次**（violates_guard_interval=true）的产物频率，返回：\n\n"
        "- `violation_count`：窗口内该频率违规的分析条数；\n"
        "- `last_violated_at`：该频率最近一次违规所在分析的创建时间；\n"
        "- `consecutive_count`：从最新一条分析起向旧逐条统计的连续违规条数，"
        "任一条分析中该频率未违规（产物未出现或标记为非违规）即终止计数，可为 0。\n\n"
        "结果按 `freq_hz` 升序；窗口内无任何违规（含站点尚无分析记录）时返回空列表。"
        "`limit` 取 1～100 的整数（缺失、越界或非整数 → 422）；站点不存在 → 404。"
    ),
)
def get_site_risk_history(
    site_id: int,
    limit: int = Query(
        ...,
        ge=1,
        le=100,
        description="取最近 N 条已保存分析（按 created_at、id 倒序），取 1～100 的整数",
    ),
) -> list[RiskHistoryEntry]:
    site = database.fetch_site(site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"site {site_id} not found")
    analyses = database.fetch_recent_analyses(site_id, limit)
    return build_risk_history(analyses)


@app.delete(
    "/sites/{site_id}/analyses/retention",
    response_model=RetentionResult,
    tags=["analysis"],
    summary="清理分析历史：仅保留最近 keep 条分析",
    description=(
        "按 **created_at、id 倒序**保留该站点最近 keep 条分析，删除更旧的分析"
        "记录及其在 idempotency_keys 中的幂等登记行。删除在**同一个数据库事务**"
        "内完成：任一步失败整批回滚，不会出现分析已删而幂等键残留（或反之）的"
        "中间状态。响应含 deleted_analysis_ids（按 id 升序）与 remaining_count"
        "（清理后剩余条数）。分析总数不超过 keep 时删除列表为空（不报错）。"
        "keep 取 1～100 的正整数（缺失、越界或非整数 → 422）；站点不存在 → 404。"
    ),
)
def prune_site_analyses(
    site_id: int,
    keep: int = Query(
        ...,
        ge=1,
        le=100,
        description="保留的最近分析条数，取 1～100 的正整数（按 created_at、id 倒序保留）",
    ),
) -> RetentionResult:
    result = database.prune_analyses(site_id, keep)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"site {site_id} not found")
    deleted_ids, remaining_count = result
    return RetentionResult(
        site_id=site_id,
        keep=keep,
        deleted_analysis_ids=deleted_ids,
        remaining_count=remaining_count,
    )


@app.get(
    "/analyses/{analysis_id}",
    response_model=AnalysisResult,
    tags=["analysis"],
    summary="按 id 查询已保存的分析结果",
)
def get_analysis(analysis_id: int) -> AnalysisResult:
    result = database.fetch_analysis(analysis_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"analysis {analysis_id} not found")
    return result


@app.get(
    "/analyses/{analysis_id}/risk-groups",
    response_model=list[RiskGroup],
    tags=["analysis"],
    summary="按已保存分析结果生成风险分组（违规产物与来源载频的二部图连通分量）",
    description=(
        "仅使用已持久化的分析结果：每个违规产物为一个风险节点，其全部来源组合中的 "
        "fa_hz/fb_hz 载频与该节点连边，按连通分量归组；非违规产物不参与。"
        "无违规产物时返回空列表。"
    ),
)
def get_risk_groups(analysis_id: int) -> list[RiskGroup]:
    result = database.fetch_analysis(analysis_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"analysis {analysis_id} not found")
    return build_risk_groups(result.products)


@app.get(
    "/analyses/{analysis_id}/mitigation-plan",
    response_model=MitigationPlan,
    tags=["analysis"],
    summary="为已保存分析的全部违规产物求最小停用载频集合（精确最小点覆盖）",
    description=(
        "仅使用已持久化的分析结果，不重新扫描。每个违规产物的每个来源组合 "
        "(fa_hz, fb_hz) 中至少停用一个载频，该产物才算被消除；在停用数不超过 "
        "max_disabled（1～8）的前提下精确求基数最小的停用集合，存在多个最小解时"
        "按载频升序列表的字典序择优。上限内无解时 feasible=false，并给出上限内"
        "消除产物最多的尽力方案与仍未消除的产物频率。分析不存在返回 404。"
    ),
)
def get_mitigation_plan(
    analysis_id: int,
    max_disabled: int = Query(
        ..., ge=1, le=8, description="允许停用的最大载频数，取 1～8 的整数"
    ),
) -> MitigationPlan:
    result = database.fetch_analysis(analysis_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"analysis {analysis_id} not found")
    return build_mitigation_plan(result.products, analysis_id, max_disabled)


@app.post(
    "/cross-site-conflicts",
    response_model=CrossSiteConflictsResponse,
    tags=["cross-site"],
    summary="跨站点互调冲突检测：近频产物配对并按连通分量聚合为冲突簇",
    description=(
        "接收 2～8 个已保存站点 ID 与频率容差 tolerance_hz（非负整数 Hz，0 表示仅"
        "同频配对），读取各站点**最近一次**分析结果（不重新扫描）。在**不同站点**"
        "的产物之间匹配频率差绝对值不超过容差的全部配对；同一站点的产物即使近频也"
        "不直接配对，只能经其他站点的配对链式传递。配对图（节点为 (站点, 产物频率)）"
        "的每个连通分量即一个冲突簇，响应列出每簇涉及站点、产物频率、簇内最小距离与"
        "全部配对明细。簇按簇内最小 (站点 ID, 产物频率) 升序编号，配对明细按"
        "(站点 A, 产物 A, 站点 B, 产物 B) 升序，结果稳定可复现。\n\n"
        "站点 ID 数量不在 2～8、重复、非正整数，或 tolerance_hz 缺失/为负/非整数时"
        "返回 422；任一站点不存在返回 404；存在但尚无分析记录的站点返回 404。"
        "无跨站近频配对时返回 200 且 clusters 为空列表。"
    ),
)
def detect_cross_site_conflicts(
    payload: CrossSiteConflictsRequest,
) -> CrossSiteConflictsResponse:
    site_ids = payload.site_ids
    existing = database.fetch_existing_site_ids(site_ids)
    missing = sorted(set(site_ids) - existing)
    if missing:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"site(s) not found: {', '.join(str(s) for s in missing)}",
        )
    latest = database.fetch_latest_analyses_for_sites(site_ids)
    no_analysis = sorted(set(site_ids) - set(latest))
    if no_analysis:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no analysis yet for site(s): "
            + ", ".join(
                f"{s} (POST /sites/{s}/analysis first)" for s in no_analysis
            ),
        )
    # 保持与请求 site_ids 一致的顺序读取
    analyses = [latest[site_id] for site_id in site_ids]
    clusters = build_cross_site_conflicts(analyses, payload.tolerance_hz)
    return CrossSiteConflictsResponse(
        site_ids=site_ids,
        tolerance_hz=payload.tolerance_hz,
        analyses=[
            SiteAnalysisRef(site_id=site_id, analysis_id=latest[site_id].analysis_id)
            for site_id in site_ids
        ],
        cluster_count=len(clusters),
        clusters=clusters,
    )
