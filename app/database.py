"""SQLite 持久化层：建表、站点与分析结果读写。

数据库路径默认 ./intermod.db，可用环境变量 IMD_DB_PATH 覆盖。
所有多行写入都在单个事务中完成：任何一步失败即整体回滚（整批拒绝）。
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from typing import Iterator

from .schemas import (
    AnalysisMeta,
    AnalysisResult,
    GuardBand,
    ProductOut,
    SiteCreate,
    SiteOut,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL UNIQUE,
    scan_start_hz     INTEGER NOT NULL CHECK (scan_start_hz > 0),
    scan_end_hz       INTEGER NOT NULL CHECK (scan_end_hz > scan_start_hz),
    guard_interval_hz INTEGER NOT NULL CHECK (guard_interval_hz >= 0),
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS carriers (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    freq_hz INTEGER NOT NULL CHECK (freq_hz > 0),
    UNIQUE (site_id, freq_hz)
);

CREATE TABLE IF NOT EXISTS guard_bands (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id  INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    start_hz INTEGER NOT NULL CHECK (start_hz > 0),
    end_hz   INTEGER NOT NULL CHECK (end_hz > start_hz)
);

CREATE TABLE IF NOT EXISTS analyses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id       INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    product_count INTEGER NOT NULL,
    result_json   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    site_id     INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    key         TEXT    NOT NULL,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (site_id, key)
);

CREATE INDEX IF NOT EXISTS idx_idempotency_analysis
    ON idempotency_keys (analysis_id);
"""


def db_path() -> str:
    return os.environ.get("IMD_DB_PATH", "intermod.db")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """短连接上下文：正常结束提交，抛异常回滚。"""
    con = sqlite3.connect(db_path(), timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 30000")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    """启动时初始化数据库（幂等建表）。"""
    path = db_path()
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA)


# ---------------------------------------------------------------- sites

def insert_site(payload: SiteCreate) -> int:
    """单事务写入站点 + 载频 + 保护频段，返回站点 id。

    重名抛出 sqlite3.IntegrityError（由路由层映射为 409）。
    """
    with connect() as con:
        cur = con.execute(
            "INSERT INTO sites (name, scan_start_hz, scan_end_hz, guard_interval_hz)"
            " VALUES (?, ?, ?, ?)",
            (payload.name, payload.scan_start_hz, payload.scan_end_hz, payload.guard_interval_hz),
        )
        site_id = cur.lastrowid
        con.executemany(
            "INSERT INTO carriers (site_id, freq_hz) VALUES (?, ?)",
            [(site_id, f) for f in payload.carriers_hz],
        )
        con.executemany(
            "INSERT INTO guard_bands (site_id, start_hz, end_hz) VALUES (?, ?, ?)",
            [(site_id, b.start_hz, b.end_hz) for b in payload.guard_bands],
        )
    return site_id


def _row_to_site(con: sqlite3.Connection, row: sqlite3.Row) -> SiteOut:
    carriers = con.execute(
        "SELECT freq_hz FROM carriers WHERE site_id = ? ORDER BY freq_hz", (row["id"],)
    ).fetchall()
    bands = con.execute(
        "SELECT start_hz, end_hz FROM guard_bands WHERE site_id = ? ORDER BY start_hz, end_hz",
        (row["id"],),
    ).fetchall()
    return SiteOut(
        id=row["id"],
        name=row["name"],
        scan_start_hz=row["scan_start_hz"],
        scan_end_hz=row["scan_end_hz"],
        guard_interval_hz=row["guard_interval_hz"],
        carriers_hz=[r["freq_hz"] for r in carriers],
        guard_bands=[GuardBand(start_hz=r["start_hz"], end_hz=r["end_hz"]) for r in bands],
        created_at=row["created_at"],
    )


def fetch_site(site_id: int) -> SiteOut | None:
    with connect() as con:
        row = con.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
        if row is None:
            return None
        return _row_to_site(con, row)


def list_sites() -> list[SiteOut]:
    with connect() as con:
        rows = con.execute("SELECT * FROM sites ORDER BY id").fetchall()
        return [_row_to_site(con, row) for row in rows]


# ---------------------------------------------------------------- analyses

def insert_analysis(site_id: int, products: list[ProductOut]) -> tuple[int, str]:
    """持久化一次分析结果，返回 (analysis_id, created_at)。"""
    result_json = json.dumps([p.model_dump() for p in products])
    with connect() as con:
        cur = con.execute(
            "INSERT INTO analyses (site_id, product_count, result_json) VALUES (?, ?, ?)",
            (site_id, len(products), result_json),
        )
        created_at = con.execute(
            "SELECT created_at FROM analyses WHERE id = ?", (cur.lastrowid,)
        ).fetchone()["created_at"]
    return cur.lastrowid, created_at


# 并发使用同一幂等键时，落败方等待领先方事务的最长时间（秒）。
IDEMPOTENT_BUSY_TIMEOUT_SECONDS = 30.0


def save_analysis_once(
    site_id: int,
    idempotency_key: str | None,
    compute_products: Callable[[], list[ProductOut]],
) -> tuple[AnalysisResult, bool]:
    """执行并保存分析；带幂等键时按 (站点, 键) 原子登记，重复键直接重放。

    返回 (AnalysisResult, created)：created=True 表示本次实际执行并写入了
    新分析；False 表示命中既有登记，返回首次结果。

    幂等路径在同一个 SQLite 事务（BEGIN IMMEDIATE，立即取得写锁）内完成
    登记查重、分析计算、分析结果与登记行写入并提交：并发同键请求中只有一个
    能成功插入登记行（PRIMARY KEY(site_id, key)），落败方在写锁释放后
    重放已提交的首次结果，因此至多产生一份分析记录。计算或写入任一步
    失败都会整体回滚，后续同键请求可作为新的首次请求重新执行。

    idempotency_key 为 None 时保持既有行为：每次调用都产生新分析记录。
    """
    if idempotency_key is None:
        products = compute_products()
        analysis_id, created_at = insert_analysis(site_id, products)
        result = _build_result(
            site_id, analysis_id, created_at, len(products), products
        )
        return result, True

    con = sqlite3.connect(db_path(), timeout=IDEMPOTENT_BUSY_TIMEOUT_SECONDS)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute(f"PRAGMA busy_timeout = {int(IDEMPOTENT_BUSY_TIMEOUT_SECONDS * 1000)}")
    # isolation_level=None：手动控制事务，BEGIN IMMEDIATE 立即获取保留锁，
    # 使并发同键请求在事务入口串行化（而非各自先读后竞争插入）。
    con.isolation_level = None
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT a.*"
            " FROM idempotency_keys k JOIN analyses a ON a.id = k.analysis_id"
            " WHERE k.site_id = ? AND k.key = ?",
            (site_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            con.execute("COMMIT")
            return _row_to_result(existing), False

        # 首次请求：分析结果与登记行在同一事务内写入。BEGIN IMMEDIATE 已
        # 取得写锁，并发同键请求会阻塞至本事务提交后重放，故计算期间
        # 不可能有其他事务插入同键登记；任一（计算/写入）失败整体回滚。
        products = compute_products()
        result_json = json.dumps([p.model_dump() for p in products])
        cur = con.execute(
            "INSERT INTO analyses (site_id, product_count, result_json) VALUES (?, ?, ?)",
            (site_id, len(products), result_json),
        )
        analysis_id = cur.lastrowid
        con.execute(
            "INSERT INTO idempotency_keys (site_id, key, analysis_id) VALUES (?, ?, ?)",
            (site_id, idempotency_key, analysis_id),
        )
        row = con.execute(
            "SELECT id, site_id, created_at, product_count, result_json"
            " FROM analyses WHERE id = ?",
            (analysis_id,),
        ).fetchone()
        con.execute("COMMIT")
        return _row_to_result(row), True
    except sqlite3.IntegrityError:
        # 并发事务抢先提交了同键登记：本事务已在 BEGIN IMMEDIATE 处
        # 被写锁阻塞至领先方提交之后才继续，故唯一冲突只可能来自该路径。
        con.execute("ROLLBACK")
        row = con.execute(
            "SELECT a.*"
            " FROM idempotency_keys k JOIN analyses a ON a.id = k.analysis_id"
            " WHERE k.site_id = ? AND k.key = ?",
            (site_id, idempotency_key),
        ).fetchone()
        if row is None:
            raise  # 非并发重放冲突（理论不可达）：交给上层按 500 处理
        return _row_to_result(row), False
    except Exception:
        # 计算或写入失败：回滚登记行与分析，后续同键请求可重新执行。
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # 事务已提交时不再回滚（保留原始异常向上抛出）
        raise
    finally:
        con.close()


def save_analyses_batch(
    site_ids: list[int],
    compute_products: Callable[[SiteOut], list[ProductOut]],
) -> list[AnalysisResult] | None:
    """按输入顺序逐站执行并保存分析；全部记录在同一个事务内原子提交。

    返回与 site_ids 同序的 AnalysisResult 列表；任一站点不存在时返回 None，
    且不执行任何分析、不写入任何记录。

    站点存在性检查、逐站分析计算（复用单站分析逻辑 compute_products）与全部
    分析记录写入都在同一个 SQLite 事务（BEGIN IMMEDIATE，立即取得写锁）内完成：
    任一站点不存在或任一步骤（计算/写入）失败都整体回滚，不会出现部分站点
    已写入的中间状态。无幂等登记（idempotency_keys）行为。
    """
    con = sqlite3.connect(db_path(), timeout=IDEMPOTENT_BUSY_TIMEOUT_SECONDS)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute(f"PRAGMA busy_timeout = {int(IDEMPOTENT_BUSY_TIMEOUT_SECONDS * 1000)}")
    # 与 save_analysis_once 一致手动控制事务，确保检查、计算与全部写入原子提交。
    con.isolation_level = None
    try:
        con.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in site_ids)
        rows = con.execute(
            f"SELECT * FROM sites WHERE id IN ({placeholders})", site_ids
        ).fetchall()
        if len(rows) != len(set(site_ids)):
            # 任一站点不存在：整批拒绝，不执行分析、不写入任何记录
            con.execute("ROLLBACK")
            return None
        site_by_id = {row["id"]: _row_to_site(con, row) for row in rows}

        results: list[AnalysisResult] = []
        for site_id in site_ids:  # 严格按输入顺序逐站计算并写入
            site = site_by_id[site_id]
            products = compute_products(site)
            result_json = json.dumps([p.model_dump() for p in products])
            cur = con.execute(
                "INSERT INTO analyses (site_id, product_count, result_json) VALUES (?, ?, ?)",
                (site_id, len(products), result_json),
            )
            analysis_id = cur.lastrowid
            created_at = con.execute(
                "SELECT created_at FROM analyses WHERE id = ?", (analysis_id,)
            ).fetchone()["created_at"]
            results.append(
                _build_result(
                    site_id, analysis_id, created_at, len(products), products
                )
            )
        con.execute("COMMIT")
        return results
    except Exception:
        # 计算或写入失败：全部分析记录随事务整体回滚
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # 事务已提交时不再回滚（保留原始异常向上抛出）
        raise
    finally:
        con.close()


def _build_result(
    site_id: int,
    analysis_id: int,
    created_at: str,
    product_count: int,
    products: list[ProductOut],
) -> AnalysisResult:
    return AnalysisResult(
        analysis_id=analysis_id,
        site_id=site_id,
        created_at=created_at,
        product_count=product_count,
        products=products,
    )


def _row_to_result(row: sqlite3.Row) -> AnalysisResult:
    products = [ProductOut(**d) for d in json.loads(row["result_json"])]
    return AnalysisResult(
        analysis_id=row["id"],
        site_id=row["site_id"],
        created_at=row["created_at"],
        product_count=row["product_count"],
        products=products,
    )


def fetch_analysis(analysis_id: int) -> AnalysisResult | None:
    with connect() as con:
        row = con.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
        return _row_to_result(row) if row else None


def fetch_latest_analysis(site_id: int) -> AnalysisResult | None:
    with connect() as con:
        row = con.execute(
            "SELECT * FROM analyses WHERE site_id = ? ORDER BY id DESC LIMIT 1", (site_id,)
        ).fetchone()
        return _row_to_result(row) if row else None


def fetch_recent_analyses(site_id: int, limit: int) -> list[AnalysisResult]:
    """读取该站点按 created_at、id 倒序的最近 limit 条已保存分析。

    返回顺序为最新在前（created_at 并列时 id 较大者在前），与历史保留清理
    的“最近”选取规则一致；仅读取已持久化结果，不触发任何分析。
    """
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM analyses WHERE site_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (site_id, limit),
        ).fetchall()
    return [_row_to_result(row) for row in rows]


def list_analyses(site_id: int) -> list[AnalysisMeta]:
    with connect() as con:
        rows = con.execute(
            "SELECT id, site_id, created_at, product_count FROM analyses"
            " WHERE site_id = ? ORDER BY id",
            (site_id,),
        ).fetchall()
    return [
        AnalysisMeta(
            analysis_id=r["id"],
            site_id=r["site_id"],
            created_at=r["created_at"],
            product_count=r["product_count"],
        )
        for r in rows
    ]


def prune_analyses(site_id: int, keep: int) -> tuple[list[int], int] | None:
    """单事务保留最近 keep 条分析，删除更旧分析及其幂等键登记行。

    保留集合按 (created_at DESC, id DESC) 选取（created_at 并列时以 id
    倒序决胜，与“最近一次分析”语义一致）；不在保留集合内的分析及其在
    idempotency_keys 中的登记行在同一个事务内删除，任一步失败即整体回滚，
    不会出现分析已删而幂等键残留（或反之）的中间状态。

    返回 (按 id 升序的已删除分析 ID 列表, 清理后剩余条数)；站点不存在时
    返回 None（不执行任何删除）。分析总数不超过 keep 时删除列表为空。
    """
    with connect() as con:
        site = con.execute("SELECT 1 FROM sites WHERE id = ?", (site_id,)).fetchone()
        if site is None:
            return None
        rows = con.execute(
            "SELECT id FROM analyses"
            " WHERE site_id = ? AND id NOT IN ("
            "  SELECT id FROM analyses WHERE site_id = ?"
            "  ORDER BY created_at DESC, id DESC LIMIT ?"
            ") ORDER BY id",
            (site_id, site_id, keep),
        ).fetchall()
        deleted_ids = [r["id"] for r in rows]
        if deleted_ids:
            placeholders = ",".join("?" for _ in deleted_ids)
            # 先显式删除幂等登记行，再删分析；两条 DELETE 在同一事务内，
            # 任一失败随 with 上下文整体回滚。
            con.execute(
                f"DELETE FROM idempotency_keys"
                f" WHERE site_id = ? AND analysis_id IN ({placeholders})",
                (site_id, *deleted_ids),
            )
            con.execute(
                f"DELETE FROM analyses WHERE id IN ({placeholders})",
                deleted_ids,
            )
        remaining = con.execute(
            "SELECT COUNT(*) AS n FROM analyses WHERE site_id = ?", (site_id,)
        ).fetchone()["n"]
    return deleted_ids, remaining


# ---------------------------------------------------------------- 跨站点批量查询

def fetch_existing_site_ids(site_ids: list[int]) -> set[int]:
    """返回给定 id 中实际存在的站点 id 集合（单次查询）。"""
    if not site_ids:
        return set()
    placeholders = ",".join("?" for _ in site_ids)
    with connect() as con:
        rows = con.execute(
            f"SELECT id FROM sites WHERE id IN ({placeholders})", site_ids
        ).fetchall()
    return {row["id"] for row in rows}


def fetch_latest_analyses_for_sites(site_ids: list[int]) -> dict[int, AnalysisResult]:
    """批量读取多个站点各自最近一次分析（每个站点取分析 id 最大的一条）。

    返回 {site_id: AnalysisResult}；无分析记录的站点不出现在结果中。
    """
    if not site_ids:
        return {}
    placeholders = ",".join("?" for _ in site_ids)
    with connect() as con:
        rows = con.execute(
            "SELECT a.* FROM analyses a"
            " JOIN (SELECT site_id, MAX(id) AS max_id FROM analyses"
            f"      WHERE site_id IN ({placeholders}) GROUP BY site_id) latest"
            "  ON a.id = latest.max_id",
            site_ids,
        ).fetchall()
    return {row["site_id"]: _row_to_result(row) for row in rows}
