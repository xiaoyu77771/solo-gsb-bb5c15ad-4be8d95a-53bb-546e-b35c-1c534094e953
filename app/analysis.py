"""三阶互调产物分析：枚举 2*fa - fb，聚合来源并评估最近命中对象。"""
from __future__ import annotations

from typing import Sequence

from .schemas import (
    AnalysisResult,
    ConflictCluster,
    ConflictPair,
    ConflictProduct,
    DriftRiskEntry,
    ExclusionRange,
    GuardBand,
    HitObject,
    MitigationPlan,
    ProductOut,
    RiskGroup,
    RiskHistoryEntry,
    SourcePair,
)


def enumerate_third_order_products(
    carriers_hz: list[int], scan_start_hz: int, scan_end_hz: int
) -> dict[int, set[tuple[int, int]]]:
    """枚举不同载频的三阶产物 2*fa - fb（fa != fb）。

    仅保留落在扫描范围 [scan_start_hz, scan_end_hz] 内的产物；
    同频产物聚合全部来源组合 (fa, fb)。
    """
    products: dict[int, set[tuple[int, int]]] = {}
    for fa in carriers_hz:
        for fb in carriers_hz:
            if fa == fb:
                continue
            freq = 2 * fa - fb
            if scan_start_hz <= freq <= scan_end_hz:
                products.setdefault(freq, set()).add((fa, fb))
    return products


def _distance_to_guard_band(freq: int, band: GuardBand) -> int:
    """到保护频段的距离：落在带内为 0，否则为到最近边界的距离。"""
    if band.start_hz <= freq <= band.end_hz:
        return 0
    return min(abs(freq - band.start_hz), abs(freq - band.end_hz))


def nearest_hit(
    freq: int, carriers_hz: list[int], guard_bands: list[GuardBand]
) -> tuple[int, HitObject]:
    """返回 freq 到载频或保护频段的最短距离及对应命中对象。

    距离相同时按确定性规则决胜：载频优先于保护频段，其次按参考频率
    （载频值或频段起点）升序，保证结果稳定可复现。
    """
    best: tuple[int, int, int, HitObject] | None = None
    for carrier in carriers_hz:
        candidate = (
            abs(freq - carrier),
            0,
            carrier,
            HitObject(type="carrier", carrier_hz=carrier),
        )
        if best is None or candidate[:3] < best[:3]:
            best = candidate
    for band in guard_bands:
        candidate = (
            _distance_to_guard_band(freq, band),
            1,
            band.start_hz,
            HitObject(type="guard_band", guard_band=band),
        )
        if best is None or candidate[:3] < best[:3]:
            best = candidate
    assert best is not None  # carriers_hz 至少含一个载频
    return best[0], best[3]


def analyze(
    carriers_hz: list[int],
    scan_start_hz: int,
    scan_end_hz: int,
    guard_bands: list[GuardBand],
    guard_interval_hz: int,
) -> list[ProductOut]:
    """完整分析：枚举产物 -> 过滤范围外 -> 同频聚合 -> 计算最近命中对象。

    返回按产物频率升序排列的结果（同频已聚合，频率唯一，排序稳定确定）。
    """
    grouped = enumerate_third_order_products(carriers_hz, scan_start_hz, scan_end_hz)
    results: list[ProductOut] = []
    for freq in sorted(grouped):
        distance, hit = nearest_hit(freq, carriers_hz, guard_bands)
        sources = [SourcePair(fa_hz=fa, fb_hz=fb) for fa, fb in sorted(grouped[freq])]
        results.append(
            ProductOut(
                freq_hz=freq,
                distance_hz=distance,
                hit=hit,
                violates_guard_interval=distance < guard_interval_hz,
                sources=sources,
            )
        )
    return results


def build_risk_groups(products: list[ProductOut]) -> list[RiskGroup]:
    """由已持久化的分析结果生成风险组（二部图连通分量）。

    仅 violates_guard_interval 的违规产物参与：每个违规产物是一个风险节点，
    其全部来源组合中的载频 fa_hz/fb_hz 与该节点连边，构成二部图；每个连通
    分量即一个风险组。同频产物的多来源已聚合在同一节点，共享载频的产物沿
    链式边传递归组；非违规产物不参与，不会桥接任何载频。
    组按 (最小载频, 最小产物频率) 升序排序，group_id 从 1 连续编号。
    """
    violating = [p for p in products if p.violates_guard_interval]
    if not violating:
        return []

    # 并查集：节点为 ("product", freq_hz) 或 ("carrier", carrier_hz)，
    # 两类节点分开编号，避免产物频率与载频数值相同而混淆。
    parent: dict[tuple[str, int], tuple[str, int]] = {}

    def find(node: tuple[str, int]) -> tuple[str, int]:
        while parent[node] != node:
            parent[node] = parent[parent[node]]  # 路径压缩
            node = parent[node]
        return node

    def link(a: tuple[str, int], b: tuple[str, int]) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for product in violating:
        pnode = ("product", product.freq_hz)
        parent.setdefault(pnode, pnode)
        for source in product.sources:
            for carrier_hz in (source.fa_hz, source.fb_hz):
                cnode = ("carrier", carrier_hz)
                parent.setdefault(cnode, cnode)
                link(pnode, cnode)

    components: dict[tuple[str, int], dict[str, set[int]]] = {}
    for node in list(parent):
        kind, value = node
        bucket = components.setdefault(find(node), {"product": set(), "carrier": set()})
        bucket[kind].add(value)

    by_freq = {p.freq_hz: p for p in violating}
    groups: list[RiskGroup] = []
    for bucket in components.values():
        members = [by_freq[f] for f in bucket["product"]]
        groups.append(
            RiskGroup(
                group_id=0,  # 占位，排序后统一编号
                carriers_hz=sorted(bucket["carrier"]),
                product_freqs_hz=sorted(bucket["product"]),
                source_pair_count=sum(len(p.sources) for p in members),
                distance_hz=min(p.distance_hz for p in members),
            )
        )
    # 各组载频集合互不相交，排序键唯一，编号稳定确定
    groups.sort(key=lambda g: (g.carriers_hz[0], g.product_freqs_hz[0]))
    for group_id, group in enumerate(groups, start=1):
        group.group_id = group_id
    return groups


def build_mitigation_plan(
    products: list[ProductOut], analysis_id: int, max_disabled: int
) -> MitigationPlan:
    """仅基于已持久化的分析结果，为全部违规产物求精确的最小停用载频集合。

    将每个违规产物的每个来源组合 (fa, fb) 视为无向边 {fa, fb}：停用集合 S
    消除一个产物，当且仅当该产物的每条来源边都至少有一个端点在 S 中，即 S
    是违规来源图的点覆盖。采用 FPT 分支限界（选边 -> 停用其两个端点之一），
    上限 max_disabled（1～8）时至多 2**max_disabled 个叶节点，保证精确；
    迭代加深先保证基数最小，同基数枚举后按载频升序列表的字典序择优。

    上限内无点覆盖（feasible=false）时，用三路分支树（停用 u / 停用 v /
    放弃该边并禁用其两端）枚举限制内可得到的全部部分覆盖，选出消除违规
    产物最多的尽力方案，并报告仍未消除的产物频率。
    """
    violating = sorted(
        (p for p in products if p.violates_guard_interval), key=lambda p: p.freq_hz
    )

    # edge=(min 载频, max 载频) -> 该边对应的产物频率集合（同一来源组合可为
    # 不同频率的产物产生；两个方向 fa/fb 只记一条无向边）
    edge_to_freqs: dict[tuple[int, int], set[int]] = {}
    freq_to_edges: dict[int, set[tuple[int, int]]] = {}
    for product in violating:
        edges = {
            tuple(sorted((source.fa_hz, source.fb_hz))) for source in product.sources
        }
        freq_to_edges[product.freq_hz] = edges
        for edge in edges:
            edge_to_freqs.setdefault(edge, set()).add(product.freq_hz)

    all_edges: list[tuple[int, int]] = sorted(edge_to_freqs)
    # 每条边的端点载频 bitmask（顶点按载频升序编号）
    vertices = sorted({v for edge in all_edges for v in edge})
    index = {v: i for i, v in enumerate(vertices)}
    edge_vertex_masks = [
        (1 << index[u]) | (1 << index[v]) for u, v in all_edges
    ]
    # 每个产物的全部来源边 bitmask
    edge_index = {edge: i for i, edge in enumerate(all_edges)}
    product_edge_masks = {
        freq: sum(1 << edge_index[e] for e in edges)
        for freq, edges in freq_to_edges.items()
    }
    # 每个载频端点所在的边 bitmask
    incident = [0] * len(vertices)
    for i, (u, v) in enumerate(all_edges):
        incident[index[u]] |= 1 << i
        incident[index[v]] |= 1 << i
    all_edge_mask = (1 << len(all_edges)) - 1

    def eliminated_freqs(chosen_mask: int) -> list[int]:
        """chosen 覆盖了某产物全部来源边时，该产物即被消除。"""
        covered = 0
        for i, vmask in enumerate(edge_vertex_masks):
            if chosen_mask & vmask:
                covered |= 1 << i
        return [
            freq
            for freq, emask in product_edge_masks.items()
            if emask & ~covered == 0
        ]

    def matching_lower_bound(chosen_mask: int, budget: int) -> int:
        """对未覆盖边贪心地取极大匹配，匹配边数即所需停用语数的下界。"""
        covered = 0
        for i, vmask in enumerate(edge_vertex_masks):
            if chosen_mask & vmask:
                covered |= 1 << i
        size = 0
        touched_vertices = 0
        for i, vmask in enumerate(edge_vertex_masks):
            if (covered >> i) & 1:
                continue
            if touched_vertices & vmask:
                continue
            size += 1
            touched_vertices |= vmask
            if size > budget:
                return size
        return size

    def pick_edge(chosen_mask: int) -> int | None:
        """选一条未覆盖边用于分支：优先端点关联未覆盖边最多的边（剪枝更强）。"""
        covered = _covered_edges(chosen_mask)
        best_i: int | None = None
        best_key: tuple[int, tuple[int, int]] | None = None
        for i, vmask in enumerate(edge_vertex_masks):
            if chosen_mask & vmask:
                continue
            u, v = all_edges[i]
            uncovered_incident = (incident[index[u]] | incident[index[v]]) & ~covered
            key = (-uncovered_incident.bit_count(), (u, v))
            if best_key is None or key < best_key:
                best_key, best_i = key, i
        return best_i

    def _covered_edges(chosen_mask: int) -> int:
        covered = 0
        for i, vmask in enumerate(edge_vertex_masks):
            if chosen_mask & vmask:
                covered |= 1 << i
        return covered

    def add_carrier(chosen: tuple[int, ...], vertex: int) -> tuple[int, ...]:
        """向升序元组中插入载频，保持升序以支持字典序比较与输出。"""
        items = list(chosen)
        lo, hi = 0, len(items)
        while lo < hi:
            mid = (lo + hi) // 2
            if items[mid] < vertex:
                lo = mid + 1
            else:
                hi = mid
        items.insert(lo, vertex)
        return tuple(items)

    # ------------------------------------------------ 可行解：迭代加深的点覆盖分支
    def search_exact_cover(k: int) -> tuple[int, ...] | None:
        """在恰好多至 k 次选择内枚举全部最小点覆盖，返回字典序最小的一个。"""
        best: tuple[int, ...] | None = None

        def dfs(chosen: tuple[int, ...], chosen_mask: int, budget: int) -> None:
            nonlocal best
            remaining = all_edge_mask & ~_covered_edges(chosen_mask)
            if remaining == 0:
                if best is None or chosen < best:
                    best = chosen
                return
            if budget == 0 or matching_lower_bound(chosen_mask, budget) > budget:
                return
            edge_i = pick_edge(chosen_mask)
            assert edge_i is not None
            u, v = all_edges[edge_i]
            # pick_edge 保证边未覆盖，故不可能两个端点都已选；已选的一端直接跳过，
            # 只对另一端分支（不能两端都跳过，否则该边永远无法覆盖）
            candidates = [
                vertex
                for vertex in (u, v)
                if not ((chosen_mask >> index[vertex]) & 1)
            ]
            for vertex in sorted(candidates):  # 载频升序尝试；仍需全枚举保证字典序最优
                dfs(
                    add_carrier(chosen, vertex),
                    chosen_mask | (1 << index[vertex]),
                    budget - 1,
                )

        dfs((), 0, k)
        return best

    solution: tuple[int, ...] | None = None
    if not violating:
        solution = ()
    else:
        for budget in range(0, max_disabled + 1):
            solution = search_exact_cover(budget)
            if solution is not None:
                break

    if solution is not None:
        chosen_mask = sum(1 << index[v] for v in solution)
        return MitigationPlan(
            analysis_id=analysis_id,
            max_disabled=max_disabled,
            feasible=True,
            disabled_carriers_hz=list(solution),
            eliminated_product_freqs_hz=eliminated_freqs(chosen_mask),
            remaining_product_freqs_hz=[],
        )

    # ------------------------------------------------ 不可行：枚举上限内的尽力方案
    def search_best_effort(
        k: int,
    ) -> tuple[tuple[int, int, int], tuple[int, ...]]:
        """在停用数至多 k 的全部部分点覆盖中求 (head, 已选载频升序元组) 最优。

        记忆化分支限界。状态为 (已选载频掩码 C, 已禁用载频掩码 F, 剩余预算
        b)；已覆盖来源边掩码 covered 由 C 唯一决定，作为参数透传避免重算。
        每个 DFS 返回其子树最优解，head =
        (消除产物数, 覆盖来源边数, -停用数)：前两项越大越好、停用越少越好，
        head 全相同时按已选元组字典序取小。分支沿一条未覆盖边展开为
        “停用 u / 停用 v / 放弃该边（两端禁用）”；放弃边不消耗预算，因此
        至多 k 个载频的任何部分覆盖都会被枚举到。
        """
        memo: dict[tuple[int, int, int], tuple[tuple[int, int, int], tuple[int, ...]]] = {}
        product_masks = list(product_edge_masks.values())
        # 全局最优：用与目标一致的贪心解初始化，供“两维上界都必败”的剪枝使用
        global_best: tuple[tuple[int, int, int], tuple[int, ...]] | None = None

        def eliminated_count_of(covered: int) -> int:
            return sum(1 for emask in product_masks if emask & ~covered == 0)

        def _min_cover_size(edge_bits: int, available_mask: int) -> int | None:
            """覆盖给定边集所需的最少可选载频数；不可行返回 None。

            对边集做精确的点覆盖分支（选一条边 -> 取其两个可选端点之一）。
            单个产物的来源边数量很少，分支规模可忽略。
            """
            best: list[int] = [10**9]

            def dfs(mask: int, forbidden: int, used: int) -> None:
                if used >= best[0]:
                    return
                if mask == 0:
                    best[0] = used
                    return
                b0 = mask & -mask
                i = b0.bit_length() - 1
                opts = edge_vertex_masks[i] & available_mask & ~forbidden
                if opts == 0:
                    return
                o = opts
                while o:
                    bit = o & -o
                    j = bit.bit_length() - 1
                    dfs(mask & ~incident[j], forbidden | bit, used + 1)
                    o &= o - 1

            dfs(edge_bits, 0, 0)
            return None if best[0] == 10**9 else best[0]

        def optimistic_elim_upper_bound(
            covered: int, forbidden_mask: int, budget: int
        ) -> int:
            """至多还能消除多少个当前未消除产物（合法上界）。

            逐产物独立计算：该产物尚缺的来源边能否被至多 budget 个可选载频
            精确覆盖（对单产物缺边求最小点覆盖）。各产物独立计算时同一批载频
            可被多个产物重复计入，故“可在 budget 内消除”的产物计数之和必然
            不小于真实选择下的消除数，是合法上界。
            """
            available_mask = ~forbidden_mask & ((1 << len(vertices)) - 1)
            count = 0
            for emask in product_masks:
                missing = emask & ~covered
                if missing == 0:
                    count += 1  # 已消除
                    continue
                need = _min_cover_size(missing, available_mask)
                if need is not None and need <= budget:
                    count += 1
            return count

        def optimistic_edge_upper_bound(
            covered: int, chosen_mask: int, forbidden_mask: int, budget: int
        ) -> int:
            """至多可覆盖的来源边总数（合法上界）：剩余 budget 步每步至多新增
            当时关联未覆盖边最多的可选载频所覆盖的边数；这是标准贪心边际收益
            上界（每步最优边际收益不小于任何同预算序列中对应步的边际收益）。
            """
            sim_covered = covered
            sim_forbidden = forbidden_mask | chosen_mask
            for _ in range(budget):
                best_gain, best_j = 0, -1
                for j in range(len(vertices)):
                    if (sim_forbidden >> j) & 1:
                        continue
                    gain = (incident[j] & ~sim_covered).bit_count()
                    if gain > best_gain:
                        best_gain, best_j = gain, j
                if best_j < 0:
                    break
                sim_forbidden |= 1 << best_j
                sim_covered |= incident[best_j]
            return sim_covered.bit_count()

        def pick_branch_edge(
            covered: int, chosen_mask: int, forbidden_mask: int
        ) -> tuple[int, int | None]:
            """返回 (边编号或 None, 唯一可选端点载频值；无则 None)。

            优先只有一个可选端点的边（强制单边分支）；否则取两端关联未覆盖
            边最多的边。所有未覆盖边两端均已禁用时返回 (None, None)。
            """
            forced: tuple[int, tuple[int, int]] | None = None  # (键, (边号, 端点))
            binary: tuple[tuple[int, int], int] | None = None  # ((键), 边号)
            for i, vmask in enumerate(edge_vertex_masks):
                if (covered >> i) & 1:
                    continue
                available = vmask & ~forbidden_mask & ~chosen_mask
                if available == 0:
                    continue
                if (available & (available - 1)) == 0:
                    key = (0, i)
                    if forced is None or key < forced[0]:
                        forced = (key, (i, vertices[available.bit_length() - 1]))
                    continue
                u, v = all_edges[i]
                degree = ((incident[index[u]] | incident[index[v]]) & ~covered).bit_count()
                key = (1, -degree, i)
                if binary is None or key < binary[0]:
                    binary = (key, i)
            if forced is not None:
                edge_i, vertex = forced[1]
                return edge_i, vertex
            if binary is not None:
                return binary[1], None
            return None, None

        def dfs(
            chosen: tuple[int, ...],
            chosen_mask: int,
            forbidden_mask: int,
            budget: int,
            covered: int,
        ) -> tuple[tuple[int, int, int], tuple[int, ...]]:
            nonlocal global_best
            state = (chosen_mask, forbidden_mask, budget)
            cached = memo.get(state)
            if cached is not None:
                if cached[0] > global_best[0] or (
                    cached[0] == global_best[0] and cached[1] < global_best[1]
                ):
                    global_best = cached
                return cached

            elim_now = eliminated_count_of(covered)
            best: tuple[tuple[int, int, int], tuple[int, ...]] = (
                (elim_now, covered.bit_count(), -len(chosen)),
                chosen,
            )

            if budget > 0:
                edge_i, forced_vertex = pick_branch_edge(
                    covered, chosen_mask, forbidden_mask
                )
                if edge_i is not None:
                    # 局部乐观上界剪枝：只与本状态的裸结果 best 比较，保证
                    # 记忆化缓存与访问路径（全局下界强弱）无关，可安全复用。
                    ub_elim = optimistic_elim_upper_bound(
                        covered, forbidden_mask | chosen_mask, budget
                    )
                    ub_edges = optimistic_edge_upper_bound(
                        covered, chosen_mask, forbidden_mask, budget
                    )
                    le, le_edges = best[0][0], best[0][1]
                    # 全局剪枝：两维上界都不超过全局当前最优且至少一维严格
                    # 更小时，子树头部分数必败（停用数/字典序无法补偿前两维
                    # 的严格劣势）。该剪枝不破坏记忆化的正确性：被剪掉的解
                    # 头部严格劣于某个已被实际找到的解，对任何调用上下文都
                    # 不可能成为最优，故缓存缺省它们无影响。
                    ge, ge_edges = global_best[0][0], global_best[0][1]
                    dominated = (ub_elim < ge and ub_edges <= ge_edges) or (
                        ub_elim <= ge and ub_edges < ge_edges
                    )
                    if not dominated and (ub_elim > le or ub_edges > le_edges):
                        next_states: list[
                            tuple[tuple[int, ...], int, int, int, int]
                        ] = []
                        if forced_vertex is not None:
                            j = index[forced_vertex]
                            bit = 1 << j
                            next_states.append(
                                (
                                    add_carrier(chosen, forced_vertex),
                                    chosen_mask | bit,
                                    forbidden_mask,
                                    budget - 1,
                                    covered | incident[j],
                                )
                            )
                            # 放弃：唯一可用端点禁用，该边在本分支无法覆盖
                            next_states.append(
                                (chosen, chosen_mask, forbidden_mask | bit, budget, covered)
                            )
                        else:
                            u, v = all_edges[edge_i]
                            for vertex in (u, v):  # 载频升序尝试
                                j = index[vertex]
                                bit = 1 << j
                                if (chosen_mask | forbidden_mask) & bit:
                                    continue
                                next_states.append(
                                    (
                                        add_carrier(chosen, vertex),
                                        chosen_mask | bit,
                                        forbidden_mask,
                                        budget - 1,
                                        covered | incident[j],
                                    )
                                )
                            # 放弃该边：两端在本分支内都不再选择
                            next_states.append(
                                (
                                    chosen,
                                    chosen_mask,
                                    forbidden_mask | edge_vertex_masks[edge_i],
                                    budget,
                                    covered,
                                )
                            )
                        for args in next_states:
                            sub = dfs(*args)
                            sh, sc = sub
                            bh, bc = best
                            if sh > bh or (sh == bh and sc < bc):
                                best = sub
                            if sh > global_best[0] or (
                                sh == global_best[0] and sc < global_best[1]
                            ):
                                global_best = sub

            memo[state] = best
            return best

        # 初始可行解：贪心逐选“新消除违规产物最多”的载频（并列时新覆盖来源
        # 边多者、载频小者优先），为全局剪枝提供与目标一致的高质量下界
        greedy_mask = 0
        greedy_chosen: tuple[int, ...] = ()
        for _ in range(k):
            covered = _covered_edges(greedy_mask)
            current_elim = eliminated_count_of(covered)
            pick_j = -1
            pick_key: tuple[int, int, int] | None = None
            for j in range(len(vertices)):
                if (greedy_mask >> j) & 1:
                    continue
                new_covered = covered | incident[j]
                gain_elim = eliminated_count_of(new_covered) - current_elim
                gain_edges = (incident[j] & ~covered).bit_count()
                key = (gain_elim, gain_edges, -j)  # j 越小（载频越小）优先
                if gain_elim == 0 and gain_edges == 0:
                    continue
                if pick_key is None or key > pick_key:
                    pick_key, pick_j = key, j
            if pick_j < 0:
                break
            greedy_mask |= 1 << pick_j
            greedy_chosen = add_carrier(greedy_chosen, vertices[pick_j])
        greedy_covered = _covered_edges(greedy_mask)
        global_best = (
            (
                eliminated_count_of(greedy_covered),
                greedy_covered.bit_count(),
                -len(greedy_chosen),
            ),
            greedy_chosen,
        )

        return dfs((), 0, 0, k, 0)

    best_head, chosen = search_best_effort(max_disabled)
    chosen_mask = sum(1 << index[v] for v in chosen)
    eliminated = set(eliminated_freqs(chosen_mask))
    return MitigationPlan(
        analysis_id=analysis_id,
        max_disabled=max_disabled,
        feasible=False,
        disabled_carriers_hz=list(chosen),
        eliminated_product_freqs_hz=sorted(eliminated),
        remaining_product_freqs_hz=[
            p.freq_hz for p in violating if p.freq_hz not in eliminated
        ],
    )


def build_risk_history(analyses: Sequence[AnalysisResult]) -> list[RiskHistoryEntry]:
    """基于已持久化的最近若干条分析，汇总每个曾违规产物频率的历史风险。

    analyses 须按从新到旧（created_at、id 倒序）排列。对在窗口内至少违规
    （violates_guard_interval=true）过一次的产物频率统计：违规出现次数、
    最近一次违规所在分析的创建时间，以及从最新一条分析起的连续违规条数
    ——逐条向旧计数，任一条分析中该频率未违规（产物未出现或标记为
    非违规）即终止连续计数。结果按产物频率升序，稳定可复现；无任何违规
    （含空历史）时返回空列表。
    """
    violating_per_analysis: list[set[int]] = []
    violation_count: dict[int, int] = {}
    last_violated_at: dict[int, str] = {}
    for result in analyses:  # 从新到旧
        freqs = {
            p.freq_hz
            for p in result.products
            if p.violates_guard_interval
        }
        violating_per_analysis.append(freqs)
        for freq in freqs:
            violation_count[freq] = violation_count.get(freq, 0) + 1
            # analyses 从新到旧：首次见到违规即最近一次违规，无需更新
            last_violated_at.setdefault(freq, result.created_at)

    entries: list[RiskHistoryEntry] = []
    for freq in sorted(violation_count):
        streak = 0
        for freqs in violating_per_analysis:  # 从最新分析起向旧计数
            if freq in freqs:
                streak += 1
            else:
                break  # 未违规（产物不出现或标记非违规）终止连续计数
        entries.append(
            RiskHistoryEntry(
                freq_hz=freq,
                violation_count=violation_count[freq],
                last_violated_at=last_violated_at[freq],
                consecutive_count=streak,
            )
        )
    return entries


def carrier_exclusion_ranges(
    carriers_hz: list[int],
    scan_start_hz: int,
    scan_end_hz: int,
    guard_bands: list[GuardBand],
    guard_interval_hz: int,
    candidate_start_hz: int,
    candidate_end_hz: int,
) -> list[ExclusionRange]:
    """反解候选范围内不宜新增载频的最小区间并集（不执行也不保存分析）。

    在 x 处新增载频会与每个现有载频 c 产生两类新三阶产物：2*x - c 与
    2*c - x。产物违规当且仅当它落在扫描范围 [scan_start_hz, scan_end_hz]
    内，且到任一现有载频或保护频段的距离严格小于 guard_interval_hz
    （与分析语义一致）。先把违规产物位置折算为整数 Hz 闭区间：开区间
    (ref - g, ref + g) 的整数点为 [ref - g + 1, ref + g - 1]；再按两种
    公式分别反解 x 的整数区间——公式一除以 2 时起点向上取整、终点向下
    取整，公式二为镜像区间无需取整。现有载频点同样禁用。结果裁剪到
    [candidate_start_hz, candidate_end_hz]，合并重叠或相邻（整数 Hz 下
    下一区间起点 <= 当前终点 + 1）的区间，按起点升序返回。
    """
    g = guard_interval_hz
    # 违规产物位置（整数 Hz 闭区间）：到载频 f 距离 < g 即开区间 (f-g, f+g)，
    # 到保护频段 [s, e] 距离 < g 即开区间 (s-g, e+g)。g = 0 时距离 < 0 恒不
    # 成立（保护频段内部距离为 0 也不违规），无违规产物位置。
    zones: list[tuple[int, int]] = []
    if g > 0:
        raw = [(f - g + 1, f + g - 1) for f in carriers_hz]
        raw += [(b.start_hz - g + 1, b.end_hz + g - 1) for b in guard_bands]
        for lo, hi in raw:
            lo, hi = max(lo, scan_start_hz), min(hi, scan_end_hz)
            if lo <= hi:
                zones.append((lo, hi))

    intervals: list[tuple[int, int]] = []
    for c in carriers_hz:
        for p_lo, p_hi in zones:
            # 公式一：p = 2*x - c ∈ [p_lo, p_hi]
            #   -> x ∈ [ceil((p_lo + c) / 2), floor((p_hi + c) / 2)]
            lo = -(-(p_lo + c) // 2)
            hi = (p_hi + c) // 2
            if lo <= hi:
                intervals.append((lo, hi))
            # 公式二：p = 2*c - x ∈ [p_lo, p_hi] -> x ∈ [2*c - p_hi, 2*c - p_lo]
            lo, hi = 2 * c - p_hi, 2 * c - p_lo
            if lo <= hi:
                intervals.append((lo, hi))
    # 现有载频点也视为禁用
    intervals.extend((f, f) for f in carriers_hz)

    # 裁剪到候选范围，合并重叠或相邻区间（结果已按起点升序）
    clipped: list[tuple[int, int]] = []
    for lo, hi in intervals:
        lo, hi = max(lo, candidate_start_hz), min(hi, candidate_end_hz)
        if lo <= hi:
            clipped.append((lo, hi))
    clipped.sort()
    merged: list[ExclusionRange] = []
    for lo, hi in clipped:
        if merged and lo <= merged[-1].end_hz + 1:
            merged[-1].end_hz = max(merged[-1].end_hz, hi)
        else:
            merged.append(ExclusionRange(start_hz=lo, end_hz=hi))
    return merged


def carrier_relocation(
    carriers_hz: list[int],
    old_hz: int,
    scan_start_hz: int,
    scan_end_hz: int,
    guard_bands: list[GuardBand],
    guard_interval_hz: int,
    start_hz: int,
    end_hz: int,
) -> int | None:
    """反解单载频迁移的可行位置：距 old_hz 最近的可行频率（等距取较小值）。

    把 old_hz 替换为候选整数 x ∈ [start_hz, end_hz] 后，新载频集合
    R ∪ {x}（R 为其余载频）按与分析一致的语义判定：每个落在扫描范围
    [scan_start_hz, scan_end_hz] 内的三阶产物 2*fa - fb（fa ≠ fb），到
    任一（新）载频或保护频段的距离都不得严格小于 guard_interval_hz，
    且 x 不得与 R 中载频重复。

    全部约束反解为关于 x 的禁用整数区间再合并，不逐 Hz 枚举：
    - x 与 R 中载频重复：单点禁用；
    - 未变来源产物 p = 2a - b（a, b ∈ R）落在扫描范围内：若 p 到 R 与
      保护频段的距离已违规，则任何 x 都不可行（直接返回 None）；否则
      禁用 |p - x| < g 的位置（未变来源产物命中新载频）；
    - 含 x 的产物 2x - c 与 2c - x（c ∈ R）：仅在产物落在扫描范围内的
      x 窗口内，分别按“到 x 本身 / 到其余载频 / 到保护频段的距离 < g”
      反解线性约束（开区间 (ref-g, ref+g) 的整数点端点内缩 1 Hz；含 2x
      的约束除以 2 时起点向上取整、终点向下取整）。

    禁用区间裁剪到候选范围并合并重叠或相邻（整数 Hz 下起点相差 <= 1）
    区间后，在可行缺口中取距 old_hz 最近的整数（等距取较小值）；无可行
    位置时返回 None。
    """
    remaining = [c for c in carriers_hz if c != old_hz]
    g = guard_interval_hz
    forbidden: list[tuple[int, int]] = [(c, c) for c in remaining]

    if g > 0:
        # 未变来源产物 p = 2a - b（a, b ∈ R）：p 到 R ∪ 保护频段的距离与
        # x 无关，已违规时任何 x 都不可行；否则禁用会被新载频 x 命中的位置。
        for a in remaining:
            for b in remaining:
                if a == b:
                    continue
                p = 2 * a - b
                if not scan_start_hz <= p <= scan_end_hz:
                    continue
                fixed = min(
                    [abs(p - c) for c in remaining]
                    + [_distance_to_guard_band(p, band) for band in guard_bands]
                )
                if fixed < g:
                    return None
                forbidden.append((p - g + 1, p + g - 1))

        for c in remaining:
            # 产物 2x - c 的扫描窗口：x ∈ [ceil((S+c)/2), floor((E+c)/2)]
            win_lo = -(-(scan_start_hz + c) // 2)
            win_hi = (scan_end_hz + c) // 2
            zones = [(c - g + 1, c + g - 1)]  # 到 x 本身：|x - c| < g
            for c2 in remaining:  # 到 c2：|2x - (c + c2)| < g
                k = c + c2
                zones.append((-(-(k - g + 1) // 2), (k + g - 1) // 2))
            for band in guard_bands:  # 到频段：2x - c ∈ (s-g, e+g)
                zones.append(
                    (
                        -(-(c + band.start_hz - g + 1) // 2),
                        (c + band.end_hz + g - 1) // 2,
                    )
                )
            for lo, hi in zones:
                lo, hi = max(lo, win_lo), min(hi, win_hi)
                if lo <= hi:
                    forbidden.append((lo, hi))

            # 产物 2c - x 的扫描窗口：x ∈ [2c-E, 2c-S]
            win_lo, win_hi = 2 * c - scan_end_hz, 2 * c - scan_start_hz
            half = (g - 1) // 2  # 到 x 本身：2|x - c| < g
            zones = [(c - half, c + half)]
            for c2 in remaining:  # 到 c2：|x - (2c - c2)| < g
                zones.append((2 * c - c2 - g + 1, 2 * c - c2 + g - 1))
            for band in guard_bands:  # 到频段：x ∈ (2c-e-g, 2c-s+g)
                zones.append(
                    (2 * c - band.end_hz - g + 1, 2 * c - band.start_hz + g - 1)
                )
            for lo, hi in zones:
                lo, hi = max(lo, win_lo), min(hi, win_hi)
                if lo <= hi:
                    forbidden.append((lo, hi))

    # 裁剪到候选范围，合并重叠或相邻区间（结果按起点升序）
    clipped: list[tuple[int, int]] = []
    for lo, hi in forbidden:
        lo, hi = max(lo, start_hz), min(hi, end_hz)
        if lo <= hi:
            clipped.append((lo, hi))
    clipped.sort()
    merged: list[list[int]] = []
    for lo, hi in clipped:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])

    # 逐可行缺口取距 old_hz 最近的整数（等距取较小值）
    best: int | None = None
    cursor = start_hz
    gaps: list[tuple[int, int]] = []
    for lo, hi in merged:
        if cursor < lo:
            gaps.append((cursor, lo - 1))
        cursor = max(cursor, hi + 1)
    if cursor <= end_hz:
        gaps.append((cursor, end_hz))
    for gap_lo, gap_hi in gaps:
        cand = min(max(old_hz, gap_lo), gap_hi)
        if best is None or (abs(cand - old_hz), cand) < (abs(best - old_hz), best):
            best = cand
    return best


def drift_risk_assessment(
    carriers_hz: list[int],
    tolerances_hz: dict[int, int],
    guard_bands: list[GuardBand],
    guard_interval_hz: int,
) -> list[DriftRiskEntry]:
    """独立载频漂移下的全频域潜在互调风险评估（不执行也不保存分析）。

    每个载频 c 的实际频率为 c + δ_c，δ_c ∈ [-t_c, +t_c]，不同载频的漂移
    互相独立，但同一载频的漂移是共享变量。对每个有序来源 (fa, fb)
    （fa != fb），产物 2*(fa+δ_a) - (fb+δ_b) 的可达区间为
    [2fa-fb - (2t_a+t_b), 2fa-fb + (2t_a+t_b)]（δ_a、δ_b 各出现一次，
    区间精确无放大）。枚举全部有序来源，不按扫描范围过滤（全频域）。

    最小可能距离按对象分别计算后取最小：
    - 漂移载频 c：差值 (2fa-fb-c) + 2δ_a - δ_b - δ_c 中同一漂移变量
      合并线性系数（c==fa 时 δ_a 的系数为 2-1=1，c==fb 时 δ_b 为 -2），
      可达半径为 Σ|系数|·t，最小距离 = max(0, |2fa-fb-c| - 半径)；
      不把产物区间与载频区间当作独立区间重复放宽。
    - 固定保护频段 [s, e]：可达区间 [lo, hi] 与频段的区间距离
      max(s-hi, lo-e, 0)（重叠为 0）。

    仅返回最小可能距离严格小于 guard_interval_hz 的来源；命中对象为取得
    该最小距离的全部并列对象（载频升序在前，保护频段按 (start_hz, end_hz)
    升序在后）。结果按 (标称频率, fa_hz, fb_hz) 升序排序，稳定可复现。
    """
    carriers = sorted(carriers_hz)
    bands = sorted(guard_bands, key=lambda b: (b.start_hz, b.end_hz))
    entries: list[DriftRiskEntry] = []
    for fa in carriers:
        t_a = tolerances_hz[fa]
        for fb in carriers:
            if fa == fb:
                continue
            t_b = tolerances_hz[fb]
            nominal = 2 * fa - fb
            spread = 2 * t_a + t_b
            lo, hi = nominal - spread, nominal + spread

            candidates: list[tuple[int, HitObject]] = []
            for c in carriers:
                # 差值 (nominal - c) + 2δ_a - δ_b - δ_c：同一漂移变量合并系数
                coef = {fa: 2, fb: -1}
                coef[c] = coef.get(c, 0) - 1
                radius = sum(abs(k) * tolerances_hz[carrier] for carrier, k in coef.items())
                distance = max(0, abs(nominal - c) - radius)
                candidates.append((distance, HitObject(type="carrier", carrier_hz=c)))
            for band in bands:
                distance = max(band.start_hz - hi, lo - band.end_hz, 0)
                candidates.append((distance, HitObject(type="guard_band", guard_band=band)))

            min_distance = min(distance for distance, _ in candidates)
            if min_distance >= guard_interval_hz:
                continue
            entries.append(
                DriftRiskEntry(
                    source=SourcePair(fa_hz=fa, fb_hz=fb),
                    nominal_freq_hz=nominal,
                    reachable_start_hz=lo,
                    reachable_end_hz=hi,
                    min_distance_hz=min_distance,
                    hits=[hit for distance, hit in candidates if distance == min_distance],
                )
            )
    entries.sort(key=lambda e: (e.nominal_freq_hz, e.source.fa_hz, e.source.fb_hz))
    return entries


def build_cross_site_conflicts(
    analyses: Sequence[AnalysisResult], tolerance_hz: int
) -> list[ConflictCluster]:
    """在不同站点最近一次分析的产物之间匹配近频配对，并按连通分量聚合为冲突簇。

    仅在**不同站点**、频率差绝对值不超过 tolerance_hz 的产物之间建立配对；
    同一站点的产物即使近频也不直接连边，只能经其他站点的配对链式传递归簇。
    配对图（节点为 (site_id, freq_hz)）的每个连通分量即一个冲突簇。
    各站点产物在单站点分析内同频已聚合，频率唯一；逐对站点取两列升序产物，
    滑动窗口枚举容差窗口内的跨站配对，避免两两全比较。簇按簇内最小节点
    (site_id, freq_hz) 升序编号，配对明细按 (站点 A, 产物 A, 站点 B, 产物 B)
    升序，结果稳定可复现；无任何跨站近频配对时返回空列表。
    """
    products_by_site: dict[int, list[ProductOut]] = {
        result.site_id: sorted(result.products, key=lambda p: p.freq_hz)
        for result in analyses
    }
    distance_of: dict[tuple[int, int], int] = {
        (result.site_id, p.freq_hz): p.distance_hz
        for result in analyses
        for p in result.products
    }
    site_ids = sorted(products_by_site)

    # 并查集：只有至少参与一条跨站配对的产物节点才会注册
    parent: dict[tuple[int, int], tuple[int, int]] = {}

    def find(node: tuple[int, int]) -> tuple[int, int]:
        while parent[node] != node:
            parent[node] = parent[parent[node]]  # 路径压缩
            node = parent[node]
        return node

    def link(a: tuple[int, int], b: tuple[int, int]) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    pair_keys: set[tuple[int, int, int, int]] = set()

    # 逐对站点：两列产物均按频率升序，j_lo 随当前产物频率单调右移，枚举
    # fb ∈ [fa - t, fa + t] 内的全部跨站配对（配对无向，站点对只遍历一次）
    for ai in range(len(site_ids)):
        for bi in range(ai + 1, len(site_ids)):
            sa, sb = site_ids[ai], site_ids[bi]
            prods_a, prods_b = products_by_site[sa], products_by_site[sb]
            j_lo = 0
            for product_a in prods_a:
                lower = product_a.freq_hz - tolerance_hz
                while j_lo < len(prods_b) and prods_b[j_lo].freq_hz < lower:
                    j_lo += 1
                j = j_lo
                while (
                    j < len(prods_b)
                    and prods_b[j].freq_hz <= product_a.freq_hz + tolerance_hz
                ):
                    node_a = (sa, product_a.freq_hz)
                    node_b = (sb, prods_b[j].freq_hz)
                    parent.setdefault(node_a, node_a)
                    parent.setdefault(node_b, node_b)
                    link(node_a, node_b)
                    pair_keys.add((sa, product_a.freq_hz, sb, prods_b[j].freq_hz))
                    j += 1

    components: dict[tuple[int, int], set[tuple[int, int]]] = {}
    for node in list(parent):
        components.setdefault(find(node), set()).add(node)

    clusters: list[ConflictCluster] = []
    for cluster_id, root in enumerate(
        sorted(components, key=lambda r: min(components[r])), start=1
    ):
        nodes = sorted(components[root])
        pairs = sorted(
            (
                ConflictPair(
                    site_a_id=sa,
                    product_a_freq_hz=fa,
                    site_b_id=sb,
                    product_b_freq_hz=fb,
                    frequency_delta_hz=abs(fa - fb),
                )
                for sa, fa, sb, fb in pair_keys
                if find((sa, fa)) == root
            ),
            key=lambda p: (
                p.site_a_id,
                p.product_a_freq_hz,
                p.site_b_id,
                p.product_b_freq_hz,
            ),
        )
        clusters.append(
            ConflictCluster(
                cluster_id=cluster_id,
                site_ids=sorted({site_id for site_id, _ in nodes}),
                products=[
                    ConflictProduct(
                        site_id=site_id,
                        freq_hz=freq,
                        distance_hz=distance_of[(site_id, freq)],
                    )
                    for site_id, freq in nodes
                ],
                min_distance_hz=min(distance_of[node] for node in nodes),
                pair_count=len(pairs),
                pairs=pairs,
            )
        )
    return clusters
