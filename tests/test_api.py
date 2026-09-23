"""API 端到端测试：使用临时 SQLite 库，通过 TestClient 走完整请求链路。"""
from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("IMD_DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:  # 触发 lifespan -> init_db
        yield c


def make_payload(**overrides):
    payload = {
        "name": "site-a",
        "scan_start_hz": 1,
        "scan_end_hz": 10_000,
        "guard_interval_hz": 150,
        "carriers_hz": [100, 300, 400],
        "guard_bands": [],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- 站点创建与校验

def test_create_site_ok(client):
    resp = client.post("/sites", json=make_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == 1
    assert body["carriers_hz"] == [100, 300, 400]
    assert body["guard_interval_hz"] == 150
    # 查询已保存方案
    assert client.get(f"/sites/{body['id']}").json()["name"] == "site-a"
    assert len(client.get("/sites").json()) == 1


def test_duplicate_carriers_rejected_as_whole_batch(client):
    resp = client.post("/sites", json=make_payload(carriers_hz=[100, 200, 100]))
    assert resp.status_code == 422
    assert client.get("/sites").json() == []  # 整批拒绝，未写入任何数据


def test_invalid_guard_band_rejected_as_whole_batch(client):
    bad = make_payload(guard_bands=[{"start_hz": 500, "end_hz": 500}])
    assert client.post("/sites", json=bad).status_code == 422
    bad2 = make_payload(guard_bands=[{"start_hz": 900, "end_hz": 100}])
    assert client.post("/sites", json=bad2).status_code == 422
    assert client.get("/sites").json() == []


def test_invalid_scan_range_rejected(client):
    assert client.post("/sites", json=make_payload(scan_end_hz=1)).status_code == 422
    assert client.get("/sites").json() == []


def test_non_positive_values_rejected(client):
    assert client.post("/sites", json=make_payload(carriers_hz=[0, 100])).status_code == 422
    assert client.post("/sites", json=make_payload(guard_interval_hz=-1)).status_code == 422
    assert client.get("/sites").json() == []


def test_duplicate_site_name_conflict(client):
    assert client.post("/sites", json=make_payload()).status_code == 201
    resp = client.post("/sites", json=make_payload())
    assert resp.status_code == 409
    sites = client.get("/sites").json()
    assert len(sites) == 1  # 第二个方案未残留


def test_get_missing_site_404(client):
    assert client.get("/sites/999").status_code == 404


# ---------------------------------------------------------------- 互调分析

def test_analysis_aggregates_same_frequency_sources(client):
    # 载频 [100, 300, 400]：2*300-100=500 与 2*400-300=500 同频，须聚合
    client.post("/sites", json=make_payload())
    resp = client.post("/sites/1/analysis")
    assert resp.status_code == 201, resp.text
    body = resp.json()

    freqs = [p["freq_hz"] for p in body["products"]]
    assert freqs == sorted(freqs)  # 按频率稳定排序
    assert freqs == [200, 500, 700]  # 范围外产物（<=0）已被过滤

    p500 = next(p for p in body["products"] if p["freq_hz"] == 500)
    assert {tuple(s.values()) for s in p500["sources"]} == {(300, 100), (400, 300)}

    # 命中对象与最短距离
    p200 = next(p for p in body["products"] if p["freq_hz"] == 200)
    assert p200["distance_hz"] == 100
    assert p200["hit"] == {"type": "carrier", "carrier_hz": 100, "guard_band": None}
    assert p200["violates_guard_interval"] is True  # 100 < 150

    p700 = next(p for p in body["products"] if p["freq_hz"] == 700)
    assert p700["hit"]["carrier_hz"] == 400
    assert p700["distance_hz"] == 300
    assert p700["violates_guard_interval"] is False  # 300 >= 150


def test_analysis_hits_guard_band_with_zero_distance(client):
    payload = make_payload(
        carriers_hz=[1000, 1100],
        guard_bands=[{"start_hz": 880, "end_hz": 920}],
        guard_interval_hz=50,
    )
    client.post("/sites", json=payload)
    body = client.post("/sites/1/analysis").json()

    products = {p["freq_hz"]: p for p in body["products"]}
    assert set(products) == {900, 1200}

    # 900 落在保护频段 [880, 920] 内：距离 0，命中频段
    assert products[900]["distance_hz"] == 0
    assert products[900]["hit"]["type"] == "guard_band"
    assert products[900]["hit"]["guard_band"] == {"start_hz": 880, "end_hz": 920}
    assert products[900]["violates_guard_interval"] is True

    # 1200 距载频 1100 为 100，距频段最近边界 280 -> 命中载频
    assert products[1200]["hit"] == {"type": "carrier", "carrier_hz": 1100, "guard_band": None}
    assert products[1200]["distance_hz"] == 100


def test_analysis_filters_out_of_scan_range(client):
    payload = make_payload(carriers_hz=[200, 500], scan_start_hz=500, scan_end_hz=900)
    client.post("/sites", json=payload)
    body = client.post("/sites/1/analysis").json()
    # 2*200-500=-100 与 2*500-200=800：仅 800 在 [500, 900] 内
    assert [p["freq_hz"] for p in body["products"]] == [800]
    assert body["product_count"] == 1


def test_analysis_persisted_and_queryable(client):
    client.post("/sites", json=make_payload())
    created = client.post("/sites/1/analysis").json()

    latest = client.get("/sites/1/analysis").json()
    assert latest == created  # 查询已保存的分析结果

    by_id = client.get(f"/analyses/{created['analysis_id']}").json()
    assert by_id == created

    metas = client.get("/sites/1/analyses").json()
    assert [m["analysis_id"] for m in metas] == [created["analysis_id"]]
    assert metas[0]["product_count"] == created["product_count"]


def test_analysis_404s(client):
    assert client.post("/sites/999/analysis").status_code == 404
    assert client.get("/sites/999/analysis").status_code == 404
    assert client.get("/analyses/999").status_code == 404
    client.post("/sites", json=make_payload())
    assert client.get("/sites/1/analysis").status_code == 404  # 尚未分析


# ---------------------------------------------------------------- 幂等键（Idempotency-Key）

def test_analysis_without_idempotency_key_keeps_existing_behavior(client):
    # 未提供请求头：每次 POST 都执行并保存一份新分析（201）
    client.post("/sites", json=make_payload())
    first = client.post("/sites/1/analysis")
    second = client.post("/sites/1/analysis")
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["analysis_id"] > first.json()["analysis_id"]
    assert [m["analysis_id"] for m in client.get("/sites/1/analyses").json()] == [
        first.json()["analysis_id"],
        second.json()["analysis_id"],
    ]


def test_idempotency_key_replays_first_result_without_new_record(client):
    client.post("/sites", json=make_payload())
    headers = {"Idempotency-Key": "run-001"}
    first = client.post("/sites/1/analysis", headers=headers)
    assert first.status_code == 201, first.text

    replay = client.post("/sites/1/analysis", headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()  # 完全返回首次结果（含 analysis_id/created_at）

    # 再次重放仍为同一份结果，且只新增过一条分析记录
    replay2 = client.post("/sites/1/analysis", headers=headers)
    assert replay2.status_code == 200
    assert replay2.json() == first.json()
    assert client.get("/sites/1/analyses").json() == [
        {
            "analysis_id": first.json()["analysis_id"],
            "site_id": 1,
            "created_at": first.json()["created_at"],
            "product_count": first.json()["product_count"],
        }
    ]
    # 最近分析与按 id 查询都指向首次结果；现有查询接口保持兼容
    assert client.get("/sites/1/analysis").json() == first.json()
    assert client.get(f"/analyses/{first.json()['analysis_id']}").json() == first.json()
    # 重放也可经风险分组等下游接口读到同一份持久化结果
    assert client.get(
        f"/analyses/{first.json()['analysis_id']}/risk-groups"
    ).status_code == 200


def test_idempotency_keys_are_scoped_per_site(client):
    client.post("/sites", json=make_payload())
    client.post("/sites", json=make_payload(name="site-b"))
    # 不同站点使用相同键互不影响，各自首次执行
    r1 = client.post("/sites/1/analysis", headers={"Idempotency-Key": "shared-key"})
    r2 = client.post("/sites/2/analysis", headers={"Idempotency-Key": "shared-key"})
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["site_id"] == 1 and r2.json()["site_id"] == 2
    assert r1.json()["analysis_id"] != r2.json()["analysis_id"]
    # 各自重放仍命中本站点的首次结果
    assert client.post(
        "/sites/1/analysis", headers={"Idempotency-Key": "shared-key"}
    ).json()["analysis_id"] == r1.json()["analysis_id"]
    assert client.post(
        "/sites/2/analysis", headers={"Idempotency-Key": "shared-key"}
    ).json()["analysis_id"] == r2.json()["analysis_id"]


def test_different_idempotency_keys_produce_separate_analyses(client):
    client.post("/sites", json=make_payload())
    r1 = client.post("/sites/1/analysis", headers={"Idempotency-Key": "key-one"})
    r2 = client.post("/sites/1/analysis", headers={"Idempotency-Key": "key-two"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r2.json()["analysis_id"] > r1.json()["analysis_id"]
    assert len(client.get("/sites/1/analyses").json()) == 2
    # 不带键的请求不影响已登记键，反之亦然
    r3 = client.post("/sites/1/analysis")
    assert r3.status_code == 201
    replay = client.post("/sites/1/analysis", headers={"Idempotency-Key": "key-one"})
    assert replay.status_code == 200
    assert replay.json()["analysis_id"] == r1.json()["analysis_id"]


def test_blank_idempotency_key_rejected_as_422(client):
    client.post("/sites", json=make_payload())
    for blank in ("", " ", "   ", "\t", " \t "):
        resp = client.post("/sites/1/analysis", headers={"Idempotency-Key": blank})
        assert resp.status_code == 422, repr(blank)
        assert "Idempotency-Key" in resp.json()["detail"]
    # 空白键被拒绝时不登记、不产生任何分析记录
    assert client.get("/sites/1/analyses").json() == []
    # 非空白键含空白字符仍视为合法（不做 strip）
    ok = client.post("/sites/1/analysis", headers={"Idempotency-Key": " key "})
    assert ok.status_code == 201


def test_idempotency_key_on_missing_site_still_404(client):
    assert client.post(
        "/sites/999/analysis", headers={"Idempotency-Key": "k"}
    ).status_code == 404


def test_failed_first_request_rolls_back_registration(client, monkeypatch):
    # 首次请求在事务内计算失败时登记行与分析都回滚；随后同键请求应能作为
    # 新的首次请求重新执行成功，而不是永久卡死或重放到不存在的结果
    client.post("/sites", json=make_payload())
    from app import main as main_module
    from app import analysis as analysis_module

    def boom(*args, **kwargs):
        raise RuntimeError("boom during analysis")

    with monkeypatch.context() as patch:
        patch.setattr(main_module, "analyze", boom)
        with pytest.raises(Exception, match="boom"):
            client.post("/sites/1/analysis", headers={"Idempotency-Key": "retry-me"})
    # 离开 context 后路由模块引用已恢复（IMD_DB_PATH 等其他补丁不受影响）
    assert main_module.analyze is analysis_module.analyze

    retry = client.post("/sites/1/analysis", headers={"Idempotency-Key": "retry-me"})
    assert retry.status_code == 201, retry.text
    replay = client.post("/sites/1/analysis", headers={"Idempotency-Key": "retry-me"})
    assert replay.status_code == 200
    assert replay.json() == retry.json()
    assert len(client.get("/sites/1/analyses").json()) == 1


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_concurrent_requests_with_same_idempotency_key_produce_one_result(
    tmp_path, monkeypatch
):
    # 真实 HTTP 并发重放：in-process uvicorn + 多线程。分析计算被刻意放慢，
    # 保证多个请求在首个事务内重叠到达；BEGIN IMMEDIATE + UNIQUE(site_id,key)
    # 必须使它们只产生一份分析结果。
    db_path = tmp_path / "conc.db"
    monkeypatch.setenv("IMD_DB_PATH", str(db_path))

    port = _free_tcp_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)

        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as http:
            r = http.post(
                "/sites",
                json=make_payload(),
            )
            assert r.status_code == 201, r.text

            from app import analysis as analysis_module
            from app import main as main_module

            real_analyze = main_module.analyze

            def slow_analyze(*args, **kwargs):
                time.sleep(0.5)  # 放大窗口，确保并发请求在同一时刻重叠
                return real_analyze(*args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(main_module, "analyze", slow_analyze)
                n = 8
                barrier = threading.Barrier(n)
                responses: list[httpx.Response | None] = [None] * n

                def worker(i: int) -> None:
                    barrier.wait()
                    responses[i] = http.post(
                        "/sites/1/analysis",
                        headers={"Idempotency-Key": "concurrent-key"},
                    )

                threads = [
                    threading.Thread(target=worker, args=(i,)) for i in range(n)
                ]
                start = time.monotonic()
                for th in threads:
                    th.start()
                for th in threads:
                    th.join()
                elapsed = time.monotonic() - start

            statuses = sorted(resp.status_code for resp in responses if resp is not None)
            assert statuses == [200] * (n - 1) + [201], statuses
            bodies = [resp.json() for resp in responses if resp is not None]
            assert all(body == bodies[0] for body in bodies)
            # 全部并发请求合计只产生一份分析记录
            metas = http.get("/sites/1/analyses").json()
            assert len(metas) == 1
            assert metas[0]["analysis_id"] == bodies[0]["analysis_id"]
            # 串行化生效：总耗时远小于 n 次完整分析（0.5s）串行之和
            assert elapsed < n * 0.5

            # 并发结束后再用同键请求，仍是稳定的 200 重放
            after = http.post(
                "/sites/1/analysis", headers={"Idempotency-Key": "concurrent-key"}
            )
            assert after.status_code == 200
            assert after.json() == bodies[0]

            # 分析模块恢复后，不带键的请求仍产生新分析
            extra = http.post("/sites/1/analysis")
            assert extra.status_code == 201
            assert len(http.get("/sites/1/analyses").json()) == 2
            assert analysis_module.analyze is main_module.analyze
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)


# ---------------------------------------------------------------- 分析历史保留清理

def _create_n_analyses(client, site_id, n, idempotent=True):
    """为站点依次创建 n 条分析（各带唯一幂等键），返回分析 id 升序列表。"""
    ids = []
    for i in range(n):
        if idempotent:
            resp = client.post(
                f"/sites/{site_id}/analysis", headers={"Idempotency-Key": f"run-{i:03d}"}
            )
            assert resp.status_code == 201, resp.text
        else:
            resp = client.post(f"/sites/{site_id}/analysis")
            assert resp.status_code == 201, resp.text
        ids.append(resp.json()["analysis_id"])
    return ids


def test_retention_keeps_recent_and_reports_counts(client):
    # keep=3：5 条分析中最旧 2 条被删（按 created_at、id 倒序保留最近 3 条）
    client.post("/sites", json=make_payload())
    ids = _create_n_analyses(client, 1, 5)
    resp = client.delete("/sites/1/analyses/retention", params={"keep": 3})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "site_id": 1,
        "keep": 3,
        "deleted_analysis_ids": [ids[0], ids[1]],  # 按 id 升序
        "remaining_count": 3,
    }
    # 剩余的恰好是最近 3 条；最近分析接口与按 id 查询均一致
    assert [m["analysis_id"] for m in client.get("/sites/1/analyses").json()] == ids[2:]
    assert client.get("/sites/1/analysis").json()["analysis_id"] == ids[-1]
    assert client.get(f"/analyses/{ids[0]}").status_code == 404
    assert client.get(f"/analyses/{ids[-1]}").status_code == 200


def test_retention_keep_one_deletes_all_but_latest(client):
    client.post("/sites", json=make_payload())
    ids = _create_n_analyses(client, 1, 4)
    body = client.delete("/sites/1/analyses/retention", params={"keep": 1}).json()
    assert body["deleted_analysis_ids"] == ids[:-1]
    assert body["remaining_count"] == 1
    assert [m["analysis_id"] for m in client.get("/sites/1/analyses").json()] == [ids[-1]]


def test_retention_no_delete_when_at_or_below_keep(client):
    # 分析总数不超过 keep：删除列表为空，不报错
    client.post("/sites", json=make_payload())
    ids = _create_n_analyses(client, 1, 2)
    resp = client.delete("/sites/1/analyses/retention", params={"keep": 2})
    assert resp.status_code == 200
    assert resp.json()["deleted_analysis_ids"] == []
    assert resp.json()["remaining_count"] == 2
    # keep=100（上限）同样不删除
    resp2 = client.delete("/sites/1/analyses/retention", params={"keep": 100})
    assert resp2.json() == {
        "site_id": 1,
        "keep": 100,
        "deleted_analysis_ids": [],
        "remaining_count": 2,
    }
    assert [m["analysis_id"] for m in client.get("/sites/1/analyses").json()] == ids


def test_retention_on_site_without_analysis(client):
    # 站点存在但无任何分析：空删除，remaining_count=0
    client.post("/sites", json=make_payload())
    resp = client.delete("/sites/1/analyses/retention", params={"keep": 1})
    assert resp.status_code == 200
    assert resp.json() == {
        "site_id": 1,
        "keep": 1,
        "deleted_analysis_ids": [],
        "remaining_count": 0,
    }


def test_retention_deletes_idempotency_keys_of_old_analyses(client):
    # 被删分析的幂等登记行同步删除：同键再次请求会作为新的首次请求（201）
    client.post("/sites", json=make_payload())
    ids = _create_n_analyses(client, 1, 3)
    assert client.delete(
        "/sites/1/analyses/retention", params={"keep": 1}
    ).json()["deleted_analysis_ids"] == ids[:-1]
    # 已删分析对应的旧键重放 -> 重新执行并产生新记录（201）
    retry = client.post("/sites/1/analysis", headers={"Idempotency-Key": "run-000"})
    assert retry.status_code == 201, retry.text
    new_id = retry.json()["analysis_id"]
    assert new_id not in ids
    assert client.post(
        "/sites/1/analysis", headers={"Idempotency-Key": "run-000"}
    ).json()["analysis_id"] == new_id
    # 保留分析的幂等键不受影响：仍是 200 重放，不产生新记录
    kept_replay = client.post(
        "/sites/1/analysis", headers={"Idempotency-Key": "run-002"}
    )
    assert kept_replay.status_code == 200
    assert kept_replay.json()["analysis_id"] == ids[-1]


def test_retention_is_scoped_per_site(client):
    # 清理一个站点不影响其他站点的分析与幂等键
    client.post("/sites", json=make_payload())
    client.post("/sites", json=make_payload(name="site-b"))
    ids1 = _create_n_analyses(client, 1, 3)
    ids2 = _create_n_analyses(client, 2, 2)
    body = client.delete("/sites/1/analyses/retention", params={"keep": 1}).json()
    assert body["deleted_analysis_ids"] == ids1[:-1]
    assert body["remaining_count"] == 1
    assert [m["analysis_id"] for m in client.get("/sites/2/analyses").json()] == ids2
    # 站点 2 的幂等键仍可重放
    replay2 = client.post("/sites/2/analysis", headers={"Idempotency-Key": "run-000"})
    assert replay2.status_code == 200
    assert replay2.json()["analysis_id"] == ids2[0]


def test_retention_orders_by_created_at_then_id(client):
    # 保留顺序以 created_at 为主、id 为辅：id 较大但 created_at 更旧的记录
    # 应先被删除；created_at 并列时再按 id 倒序保留。
    client.post("/sites", json=make_payload())
    ids = _create_n_analyses(client, 1, 3)
    import sqlite3

    from app import database

    con = sqlite3.connect(database.db_path())
    try:
        # 把中间 id 的记录改得最旧：保留顺序变为 ids[2](最新) > ids[0] > ids[1]
        con.execute(
            "UPDATE analyses SET created_at = '2000-01-01T00:00:00.000Z' WHERE id = ?",
            (ids[1],),
        )
        # ids[0] 与 ids[2] 置为同一时刻：按 id 倒序应保留 ids[2]
        con.execute(
            "UPDATE analyses SET created_at = '2026-01-01T00:00:00.000Z' WHERE id IN (?, ?)",
            (ids[0], ids[2]),
        )
        con.commit()
    finally:
        con.close()

    body = client.delete("/sites/1/analyses/retention", params={"keep": 2}).json()
    assert body["deleted_analysis_ids"] == [ids[1]]  # created_at 最旧的先删
    assert body["remaining_count"] == 2
    # 尽管 ids[0] 的 id 更小，但 created_at 较新故被保留；并列时刻中 id 较大者为最新
    assert [m["analysis_id"] for m in client.get("/sites/1/analyses").json()] == [
        ids[0],
        ids[2],
    ]
    assert client.get("/sites/1/analysis").json()["analysis_id"] == ids[2]

    # 剩余两条 created_at 相同：keep=1 时按 id 倒序保留 ids[2]
    body = client.delete("/sites/1/analyses/retention", params={"keep": 1}).json()
    assert body["deleted_analysis_ids"] == [ids[0]]
    assert body["remaining_count"] == 1
    assert client.get("/sites/1/analysis").json()["analysis_id"] == ids[2]

    # 只剩 1 条，幂等再清理无删除
    again = client.delete("/sites/1/analyses/retention", params={"keep": 1}).json()
    assert again["deleted_analysis_ids"] == []
    assert again["remaining_count"] == 1


def test_retention_404_missing_site(client):
    assert client.delete(
        "/sites/999/analyses/retention", params={"keep": 1}
    ).status_code == 404
    # 站点不存在时参数仍先通过校验；非法 keep 优先返回 422 而不是 404
    assert client.delete(
        "/sites/999/analyses/retention", params={"keep": 0}
    ).status_code == 422


def test_retention_422_invalid_keep(client):
    client.post("/sites", json=make_payload())
    url = "/sites/1/analyses/retention"
    # 缺少 keep
    assert client.delete(url).status_code == 422
    # 越界（0、负数、101）/ 非整数 -> 422
    for bad in (0, -1, 101, 1000, "abc", 1.5):
        assert client.delete(url, params={"keep": bad}).status_code == 422, bad
    # 边界值 1 与 100 合法
    assert client.delete(url, params={"keep": 1}).status_code == 200
    assert client.delete(url, params={"keep": 100}).status_code == 200


def test_retention_rolls_back_whole_batch_on_failure(client, monkeypatch):
    # 删除过程中任一步失败：分析与幂等键登记行整批回滚，数据恢复原样。
    # 通过 SQL 触发器令 idempotency_keys 的 DELETE 在存在登记行时抛错。
    client.post("/sites", json=make_payload())
    ids = _create_n_analyses(client, 1, 3)

    import sqlite3

    from app import database

    con = sqlite3.connect(database.db_path())
    try:
        con.execute(
            "CREATE TRIGGER fail_idempotency_delete BEFORE DELETE ON idempotency_keys"
            " BEGIN"
            "   SELECT RAISE(ABORT, 'injected delete failure');"
            " END"
        )
        con.commit()
    finally:
        con.close()

    with pytest.raises(Exception, match="injected delete failure"):
        client.delete("/sites/1/analyses/retention", params={"keep": 1})

    # 分析记录一条不少
    assert [m["analysis_id"] for m in client.get("/sites/1/analyses").json()] == ids
    # 幂等登记行同样回滚：旧键仍 200 重放到原分析，而非重新执行
    replay = client.post("/sites/1/analysis", headers={"Idempotency-Key": "run-000"})
    assert replay.status_code == 200
    assert replay.json()["analysis_id"] == ids[0]

    # 触发器移除后清理可正常完成
    con = sqlite3.connect(database.db_path())
    try:
        con.execute("DROP TRIGGER fail_idempotency_delete")
        con.commit()
    finally:
        con.close()
    body = client.delete("/sites/1/analyses/retention", params={"keep": 1}).json()
    assert body["deleted_analysis_ids"] == ids[:-1]
    assert body["remaining_count"] == 1


# ---------------------------------------------------------------- 风险分组

def test_risk_groups_404(client):
    assert client.get("/analyses/999/risk-groups").status_code == 404

def test_risk_groups_empty_when_no_violation(client):
    # guard_interval_hz=0：distance < 0 永不成立，无违规产物 -> 空列表
    client.post("/sites", json=make_payload(guard_interval_hz=0))
    analysis_id = client.post("/sites/1/analysis").json()["analysis_id"]
    resp = client.get(f"/analyses/{analysis_id}/risk-groups")
    assert resp.status_code == 200
    assert resp.json() == []


def test_risk_groups_merge_multi_source_and_shared_carriers(client):
    # 载频 [100, 300, 400]：违规产物 200（来源 (300,400)）与 500（来源 (300,100)、
    # (400,300)，同频已合并）共享载频 300/400，归为一组；非违规的 700 不参与
    client.post("/sites", json=make_payload())
    analysis_id = client.post("/sites/1/analysis").json()["analysis_id"]
    resp = client.get(f"/analyses/{analysis_id}/risk-groups")
    assert resp.status_code == 200
    assert resp.json() == [
        {
            "group_id": 1,
            "carriers_hz": [100, 300, 400],
            "product_freqs_hz": [200, 500],
            "source_pair_count": 3,  # 200 有 1 个来源，500 有 2 个来源
            "distance_hz": 100,  # min(100, 100)
        }
    ]


def test_risk_groups_split_disjoint_components(client):
    # 两簇载频各产生违规产物；非违规产物 9900（来源含载频 100 与 5000）
    # 不参与分组，不得把两簇桥接成一组
    payload = make_payload(
        carriers_hz=[100, 200, 5000, 6000],
        scan_start_hz=1,
        scan_end_hz=100000,
        guard_interval_hz=1100,
    )
    client.post("/sites", json=payload)
    analysis_id = client.post("/sites/1/analysis").json()["analysis_id"]
    groups = client.get(f"/analyses/{analysis_id}/risk-groups").json()
    assert groups == [
        {
            "group_id": 1,
            "carriers_hz": [100, 200],
            "product_freqs_hz": [300],
            "source_pair_count": 1,
            "distance_hz": 100,
        },
        {
            "group_id": 2,
            "carriers_hz": [5000, 6000],
            "product_freqs_hz": [4000, 7000],
            "source_pair_count": 2,
            "distance_hz": 1000,
        },
    ]


def test_risk_groups_transitive_carrier_sharing(client):
    # 300 的载频 {100,200} 与 1200 的 {400,800} 不直接共享，
    # 仅经 600/700 链式共享载频传递归为一组
    payload = make_payload(
        carriers_hz=[100, 200, 400, 800],
        scan_start_hz=1,
        scan_end_hz=2000,
        guard_interval_hz=450,
    )
    client.post("/sites", json=payload)
    analysis_id = client.post("/sites/1/analysis").json()["analysis_id"]
    groups = client.get(f"/analyses/{analysis_id}/risk-groups").json()
    assert groups == [
        {
            "group_id": 1,
            "carriers_hz": [100, 200, 400, 800],
            "product_freqs_hz": [300, 600, 700, 1200],
            "source_pair_count": 4,
            "distance_hz": 100,
        }
    ]


def test_risk_groups_use_persisted_result(client):
    # 分组基于已持久化的分析结果：同一 analysis_id 重复查询结果一致
    client.post("/sites", json=make_payload())
    analysis_id = client.post("/sites/1/analysis").json()["analysis_id"]
    first = client.get(f"/analyses/{analysis_id}/risk-groups").json()
    second = client.get(f"/analyses/{analysis_id}/risk-groups").json()
    assert first == second
    assert [g["group_id"] for g in first] == list(range(1, len(first) + 1))


# ---------------------------------------------------------------- 新增载频禁用区间

def test_carrier_exclusion_ranges_both_formulas_and_merge(client):
    # 载频 [1000, 5000]，g=10：违规产物位置为 (990,1010) 与 (4990,5010) 的整数点。
    # 公式一 2*x-c：x∈[996,1004]（c=1000，ceil(1991/2)/floor(2009/2) 检验除 2 取整）、
    # [2996,3004]（两个载频各贡献一次）、[4996,5004]（c=5000）；
    # 公式二 2*c-x：x∈[991,1009]、[4991,5009]、[8991,9009]（负区间被候选范围裁掉）；
    # 载频点 [1000,1000]、[5000,5000] 被相邻区间吸收合并。
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 5000], scan_end_hz=100000, guard_interval_hz=10,
    ))
    resp = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 100000},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "site_id": 1,
        "candidate_start_hz": 1,
        "candidate_end_hz": 100000,
        "exclusion_ranges": [
            {"start_hz": 991, "end_hz": 1009},
            {"start_hz": 2996, "end_hz": 3004},
            {"start_hz": 4991, "end_hz": 5009},
            {"start_hz": 8991, "end_hz": 9009},
        ],
    }


def test_carrier_exclusion_ranges_open_boundary(client):
    # 开边界：产物到载频的距离恰好等于 g 时不违规。单载频 1000、g=10 时
    # 禁用区间为 [991,1009]；若误用闭边界会得到 [990,1010]
    client.post("/sites", json=make_payload(
        carriers_hz=[1000], scan_end_hz=100000, guard_interval_hz=10,
    ))
    body = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 100000},
    ).json()
    assert body["exclusion_ranges"] == [{"start_hz": 991, "end_hz": 1009}]


def test_carrier_exclusion_ranges_with_guard_band(client):
    # 保护频段 [480,520]、g=150 产生违规产物位置 [331,669]，载频 1000 产生
    # [851,1149]；反解后载频点 [1000,1000] 被 [851,1149] 吸收
    client.post("/sites", json=make_payload(
        carriers_hz=[1000],
        guard_bands=[{"start_hz": 480, "end_hz": 520}],
        guard_interval_hz=150,
    ))
    body = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 10000},
    ).json()
    assert body["exclusion_ranges"] == [
        {"start_hz": 666, "end_hz": 834},
        {"start_hz": 851, "end_hz": 1149},
        {"start_hz": 1331, "end_hz": 1669},
    ]


def test_carrier_exclusion_ranges_clipped_to_candidate(client):
    # 与上一组相同的数据，候选范围 [1005, 4990]：首区间被裁剪，
    # [4991,5009] 与 [8991,9009] 整体落在候选范围外被丢弃
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 5000], scan_end_hz=100000, guard_interval_hz=10,
    ))
    body = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1005, "candidate_end_hz": 4990},
    ).json()
    assert body["exclusion_ranges"] == [
        {"start_hz": 1005, "end_hz": 1009},
        {"start_hz": 2996, "end_hz": 3004},
    ]


def test_carrier_exclusion_ranges_zero_guard_interval(client):
    # g=0：任何产物都不违规，仅现有载频点禁用；相邻载频点 [100,100] 与
    # [101,101] 合并为 [100,101]
    client.post("/sites", json=make_payload(
        carriers_hz=[100, 101, 105], guard_interval_hz=0,
    ))
    body = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 1000},
    ).json()
    assert body["exclusion_ranges"] == [
        {"start_hz": 100, "end_hz": 101},
        {"start_hz": 105, "end_hz": 105},
    ]


def test_carrier_exclusion_ranges_empty_when_nothing_forbidden(client):
    # 候选范围避开全部禁用位置时返回空列表
    client.post("/sites", json=make_payload(
        carriers_hz=[1000], scan_end_hz=100000, guard_interval_hz=10,
    ))
    body = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 2000, "candidate_end_hz": 3000},
    ).json()
    assert body["exclusion_ranges"] == []


def test_carrier_exclusion_ranges_404(client):
    resp = client.get(
        "/sites/999/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 100},
    )
    assert resp.status_code == 404


def test_carrier_exclusion_ranges_param_validation(client):
    client.post("/sites", json=make_payload())
    base = "/sites/1/carrier-exclusion-ranges"
    # 缺参 / 非正整数 / 非整数 -> 422
    assert client.get(base).status_code == 422
    assert client.get(base, params={"candidate_start_hz": 1}).status_code == 422
    for bad in (
        {"candidate_start_hz": 0, "candidate_end_hz": 100},
        {"candidate_start_hz": 1, "candidate_end_hz": -5},
        {"candidate_start_hz": "abc", "candidate_end_hz": 100},
        {"candidate_start_hz": 1.5, "candidate_end_hz": 100},
        {"candidate_start_hz": 200, "candidate_end_hz": 100},  # 起点大于终点
    ):
        resp = client.get(base, params=bad)
        assert resp.status_code == 422, bad
    # 起点等于终点合法（闭区间退化为单点候选范围）
    ok = client.get(base, params={"candidate_start_hz": 300, "candidate_end_hz": 300})
    assert ok.status_code == 200
    assert ok.json()["exclusion_ranges"] == [{"start_hz": 300, "end_hz": 300}]


def test_carrier_exclusion_ranges_does_not_save_analysis(client):
    # 该端点不执行也不保存分析：调用后仍无分析记录
    client.post("/sites", json=make_payload())
    resp = client.get(
        "/sites/1/carrier-exclusion-ranges",
        params={"candidate_start_hz": 1, "candidate_end_hz": 10000},
    )
    assert resp.status_code == 200
    assert client.get("/sites/1/analysis").status_code == 404
    assert client.get("/sites/1/analyses").json() == []


# ---------------------------------------------------------------- 单载频迁移规划

def _reloc_body(old_hz, start_hz, end_hz):
    return {"old_hz": old_hz, "start_hz": start_hz, "end_hz": end_hz}


def test_carrier_relocation_nearest_feasible_below_old(client):
    # 载频 [1000, 1100]、g=150：替换 1000 时禁用区间为 [951,1249]
    # （产物 2x-1100 与 2200-x 到 x 本身/载频 1100 的距离约束合并），
    # 候选 [1,3000] 内距 1000 最近的可行位置为 950（距离恰为 150 不违规）
    client.post("/sites", json=make_payload(carriers_hz=[1000, 1100]))
    resp = client.post("/sites/1/carrier-relocation", json=_reloc_body(1000, 1, 3000))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "site_id": 1,
        "old_hz": 1000,
        "start_hz": 1,
        "end_hz": 3000,
        "feasible": True,
        "new_hz": 950,
        "distance_hz": 50,
    }
    # 951 不可行（2*951-1100=802 距新载频 951 为 149 < 150），950 两侧最近


def test_carrier_relocation_unchanged_source_product_hits_new_carrier(client):
    # 载频 [700, 1000, 1300]、g=150，替换 700。未变来源产物 2*1000-1300=700
    # 自身不违规（距 1000 为 300），但会命中落在 (550, 850) 内的新载频，
    # 禁用 [551,849]；与其余约束合并后可行缺口为 [1,550]、[850,850]、
    # [1750,3000]。550 与 850 距 old_hz=700 同为 150，等距取较小值 550
    client.post("/sites", json=make_payload(carriers_hz=[700, 1000, 1300]))
    resp = client.post("/sites/1/carrier-relocation", json=_reloc_body(700, 1, 3000))
    assert resp.status_code == 200, resp.text
    assert resp.json()["new_hz"] == 550
    assert resp.json()["distance_hz"] == 150

    # 边界开区间：x=551 时产物 700 距新载频为 149 < 150 -> 违规，单点候选无可行位置
    resp = client.post("/sites/1/carrier-relocation", json=_reloc_body(700, 551, 551))
    assert resp.json()["feasible"] is False
    assert resp.json()["new_hz"] is None
    # x=550 时产物 700 距新载频恰为 150，不违规
    resp = client.post("/sites/1/carrier-relocation", json=_reloc_body(700, 550, 550))
    assert resp.json()["feasible"] is True
    assert resp.json()["new_hz"] == 550


def test_carrier_relocation_infeasible_when_remaining_carriers_violate(client):
    # 载频 [300, 400, 1000]、g=150，替换 1000：未变来源产物 2*300-400=200
    # 距载频 300 为 100 < 150，与 x 无关地违规 -> 任何 x 都不可行
    client.post("/sites", json=make_payload(carriers_hz=[300, 400, 1000]))
    resp = client.post("/sites/1/carrier-relocation", json=_reloc_body(1000, 1, 5000))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "site_id": 1,
        "old_hz": 1000,
        "start_hz": 1,
        "end_hz": 5000,
        "feasible": False,
        "new_hz": None,
        "distance_hz": None,
    }


def test_carrier_relocation_duplicate_of_remaining_carrier_forbidden(client):
    # 候选区间退化为其余载频本身（5000）：唯一点与其余载频重复 -> 无可行位置
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 5000], scan_end_hz=100000, guard_interval_hz=10,
    ))
    resp = client.post(
        "/sites/1/carrier-relocation", json=_reloc_body(1000, 5000, 5000)
    )
    assert resp.status_code == 200
    assert resp.json()["feasible"] is False
    assert resp.json()["new_hz"] is None


def test_carrier_relocation_respects_scan_range_window(client):
    # 扫描范围 [1,3000]：x≈5000 时产物 2x-5000≈5000 与 10000-x≈5000 都落在
    # 扫描范围外，不产生约束；候选 [4990,4995] 全部可行，取距 1000 最近的 4990
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 5000], scan_end_hz=3000, guard_interval_hz=10,
    ))
    resp = client.post(
        "/sites/1/carrier-relocation", json=_reloc_body(1000, 4990, 4995)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["feasible"] is True
    assert resp.json()["new_hz"] == 4990
    assert resp.json()["distance_hz"] == 3990


def test_carrier_relocation_guard_band_constraint(client):
    # 载频 [1000, 2000]、保护频段 [480,520]、g=150，替换 2000：
    # 禁用区间 [666,834]（产物 2x-1000 命中频段）、[851,1149]、
    # [1331,1669]（产物 2000-x 命中频段）；候选 [1150,1670] 内可行缺口为
    # [1150,1330] 与单点 1670，距 2000 最近的可行位置为 1670
    # （产物 2*1000-1670=330 距频段恰为 150，不违规）
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 2000],
        guard_bands=[{"start_hz": 480, "end_hz": 520}],
    ))
    resp = client.post("/sites/1/carrier-relocation", json=_reloc_body(2000, 1150, 1670))
    assert resp.status_code == 200, resp.text
    assert resp.json()["new_hz"] == 1670
    assert resp.json()["distance_hz"] == 330


def test_carrier_relocation_zero_guard_interval(client):
    # g=0：任何产物都不违规，仅其余载频点禁用；候选 [101,104] 内 101 禁用，
    # 距 old_hz=105 最近的可行位置为 104
    client.post("/sites", json=make_payload(
        carriers_hz=[100, 101, 105], guard_interval_hz=0,
    ))
    resp = client.post("/sites/1/carrier-relocation", json=_reloc_body(105, 101, 104))
    assert resp.status_code == 200, resp.text
    assert resp.json()["new_hz"] == 104
    assert resp.json()["distance_hz"] == 1


def test_carrier_relocation_single_carrier_site(client):
    # 单载频站点替换后无任何产物：候选区间全部可行，取距 old_hz 最近的端点
    client.post("/sites", json=make_payload(carriers_hz=[500], guard_interval_hz=100))
    resp = client.post("/sites/1/carrier-relocation", json=_reloc_body(500, 600, 900))
    assert resp.status_code == 200, resp.text
    assert resp.json()["new_hz"] == 600
    assert resp.json()["distance_hz"] == 100


def test_carrier_relocation_old_position_stays_feasible(client):
    # 当前配置本无违规且 old_hz 在候选区间内：最近可行位置即 old_hz 本身
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 5000], scan_end_hz=100000, guard_interval_hz=10,
    ))
    resp = client.post(
        "/sites/1/carrier-relocation", json=_reloc_body(1000, 1, 100000)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["new_hz"] == 1000
    assert resp.json()["distance_hz"] == 0


def test_carrier_relocation_404_missing_site(client):
    resp = client.post("/sites/999/carrier-relocation", json=_reloc_body(100, 1, 200))
    assert resp.status_code == 404


def test_carrier_relocation_422_old_hz_not_a_carrier(client):
    client.post("/sites", json=make_payload())  # 载频 [100, 300, 400]
    resp = client.post("/sites/1/carrier-relocation", json=_reloc_body(999, 1, 1000))
    assert resp.status_code == 422
    assert "999" in resp.json()["detail"]
    # 站点不存在与 old_hz 非法同时出现时，站点不存在优先返回 404
    assert (
        client.post("/sites/999/carrier-relocation", json=_reloc_body(999, 1, 1000))
        .status_code
        == 404
    )


def test_carrier_relocation_422_validation(client):
    client.post("/sites", json=make_payload())
    url = "/sites/1/carrier-relocation"
    # 区间无效：起点大于终点
    assert client.post(url, json=_reloc_body(100, 200, 100)).status_code == 422
    # 非正整数 / 非整数 / 缺失字段 / 缺整个请求体
    assert client.post(url, json=_reloc_body(0, 1, 100)).status_code == 422
    assert client.post(url, json=_reloc_body(100, 0, 100)).status_code == 422
    assert client.post(url, json=_reloc_body(100, 1, -5)).status_code == 422
    assert client.post(url, json=_reloc_body(100, 1.5, 100)).status_code == 422
    assert client.post(url, json=_reloc_body(100, "x", 100)).status_code == 422
    assert client.post(url, json={"old_hz": 100, "start_hz": 1}).status_code == 422
    assert client.post(url, json={"start_hz": 1, "end_hz": 100}).status_code == 422
    assert client.post(url).status_code == 422
    # 起点等于终点合法（候选区间退化为单点）
    ok = client.post(url, json=_reloc_body(400, 700, 700))
    assert ok.status_code == 200
    assert ok.json()["new_hz"] == 700


def test_carrier_relocation_does_not_modify_site_or_save_analysis(client):
    # 该端点不修改站点配置、不保存分析：调用前后站点与分析记录完全不变
    client.post("/sites", json=make_payload())
    before = client.get("/sites/1").json()
    resp = client.post("/sites/1/carrier-relocation", json=_reloc_body(400, 1, 10000))
    assert resp.status_code == 200
    assert client.get("/sites/1").json() == before
    assert client.get("/sites/1/analysis").status_code == 404
    assert client.get("/sites/1/analyses").json() == []


def _create_and_analyze(client, **payload_overrides):
    client.post("/sites", json=make_payload(**payload_overrides))
    return client.post("/sites/1/analysis").json()["analysis_id"]


def test_mitigation_plan_404(client):
    assert client.get("/analyses/999/mitigation-plan", params={"max_disabled": 3}).status_code == 404


def test_mitigation_plan_max_disabled_validation(client):
    analysis_id = _create_and_analyze(client)
    # 缺少必填参数 / 越界 / 非整数 -> 422
    assert client.get(f"/analyses/{analysis_id}/mitigation-plan").status_code == 422
    for bad in (0, 9, -1):
        resp = client.get(
            f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": bad}
        )
        assert resp.status_code == 422, bad
    assert (
        client.get(
            f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": "x"}
        ).status_code
        == 422
    )


def test_mitigation_plan_empty_when_no_violation(client):
    # guard_interval_hz=0：无违规产物，无需停用任何载频
    analysis_id = _create_and_analyze(client, guard_interval_hz=0)
    resp = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 1}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "analysis_id": analysis_id,
        "max_disabled": 1,
        "feasible": True,
        "disabled_carriers_hz": [],
        "eliminated_product_freqs_hz": [],
        "remaining_product_freqs_hz": [],
    }


def test_mitigation_plan_single_carrier_solution(client):
    # 载频 [1000, 1100]：违规产物 900 只有来源 (1000,1100)，停用其一即可，
    # 多个最小解时按字典序停用较小载频 1000
    analysis_id = _create_and_analyze(
        client,
        carriers_hz=[1000, 1100],
        guard_bands=[{"start_hz": 880, "end_hz": 920}],
        guard_interval_hz=50,
    )
    resp = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 1}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "analysis_id": analysis_id,
        "max_disabled": 1,
        "feasible": True,
        "disabled_carriers_hz": [1000],
        "eliminated_product_freqs_hz": [900],
        "remaining_product_freqs_hz": [],
    }


def test_mitigation_plan_lexicographic_tie_break(client):
    # 违规边 {100,200}/{200,400}/{400,100}/{400,800}：最小点覆盖大小为 2，
    # 多个最小解（{100,400}、{200,400}）中按升序列表字典序取 [100, 400]；
    # 上限 1 时无解，尽力方案停用 400 可消除 3 个产物、仅剩 300
    analysis_id = _create_and_analyze(
        client,
        carriers_hz=[100, 200, 400, 800],
        scan_end_hz=2000,
        guard_interval_hz=450,
    )
    infeasible = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 1}
    ).json()
    assert infeasible["feasible"] is False
    assert infeasible["disabled_carriers_hz"] == [400]
    assert infeasible["eliminated_product_freqs_hz"] == [600, 700, 1200]
    assert infeasible["remaining_product_freqs_hz"] == [300]

    feasible = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 2}
    ).json()
    assert feasible == {
        "analysis_id": analysis_id,
        "max_disabled": 2,
        "feasible": True,
        "disabled_carriers_hz": [100, 400],
        "eliminated_product_freqs_hz": [300, 600, 700, 1200],
        "remaining_product_freqs_hz": [],
    }


def test_mitigation_plan_all_sources_must_be_covered(client):
    # 产物 700 有两个互不共享载频的来源 (400,100) 与 (500,300)：只停一个载频
    # 不能消除它（必须两个来源组合各停用至少一个载频）
    analysis_id = _create_and_analyze(
        client,
        carriers_hz=[100, 300, 400, 500],
        scan_start_hz=695,
        scan_end_hz=710,
        guard_bands=[{"start_hz": 698, "end_hz": 702}],
        guard_interval_hz=10,
    )
    one = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 1}
    ).json()
    assert one["feasible"] is False
    # 只停一个载频最多覆盖一个来源组合，700 仍未消除；字典序最小的尽力选择是 100
    assert one["disabled_carriers_hz"] == [100]
    assert one["eliminated_product_freqs_hz"] == []
    assert one["remaining_product_freqs_hz"] == [700]

    two = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 2}
    ).json()
    assert two["feasible"] is True
    # 两条来源边 {100,400}、{300,500} 各停一个端点，字典序最小为 [100, 300]
    assert two["disabled_carriers_hz"] == [100, 300]
    assert two["eliminated_product_freqs_hz"] == [700]
    assert two["remaining_product_freqs_hz"] == []


def test_mitigation_plan_three_disjoint_edges(client):
    # 三对互不相连的载频各有一个定点违规产物（小保护间隔 + 窄保护频段，
    # 不规则间隔避免跨对产物恰好落在其他载频上而产生连边）：需恰好停用 3 个
    analysis_id = _create_and_analyze(
        client,
        carriers_hz=[1000, 1150, 4700, 4900, 12300, 12600],
        scan_start_hz=1,
        scan_end_hz=40000,
        guard_bands=[
            {"start_hz": 845, "end_hz": 855},
            {"start_hz": 4495, "end_hz": 4505},
            {"start_hz": 11995, "end_hz": 12005},
        ],
        guard_interval_hz=10,
    )
    two = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 2}
    ).json()
    assert two["feasible"] is False
    # 上限 2 只能消除两对中的产物；消除数并列时按字典序取较小两对的小端点
    assert two["disabled_carriers_hz"] == [1000, 4700]
    assert two["eliminated_product_freqs_hz"] == [850, 4500]
    assert two["remaining_product_freqs_hz"] == [12000]

    three = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 3}
    ).json()
    assert three["feasible"] is True
    assert three["disabled_carriers_hz"] == [1000, 4700, 12300]
    assert three["eliminated_product_freqs_hz"] == [850, 4500, 12000]
    assert three["remaining_product_freqs_hz"] == []

    # 上限 8 同样返回基数最小（3）的解，不多停用载频
    eight = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 8}
    ).json()
    assert eight["feasible"] is True
    assert eight["disabled_carriers_hz"] == [1000, 4700, 12300]


def test_mitigation_plan_uses_persisted_result(client):
    # 仅使用已保存结果：同一 analysis_id 重复查询（含不同上限）结果确定可复现
    analysis_id = _create_and_analyze(client)
    first = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 2}
    ).json()
    second = client.get(
        f"/analyses/{analysis_id}/mitigation-plan", params={"max_disabled": 2}
    ).json()
    assert first == second


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


# ---------------------------------------------------------------- 跨站点冲突检测

def _make_site(client, site_id, name=None, **overrides):
    """按默认参数创建站点（id 从 1 起连续，故直接返回传入 site_id）。"""
    payload = make_payload(name=name or f"site-{site_id}", **overrides)
    assert client.post("/sites", json=payload).status_code == 201, payload
    return site_id


def _analyze(client, site_id):
    return client.post(f"/sites/{site_id}/analysis").json()["analysis_id"]


def test_cross_site_conflicts_basic_pairing_and_clusters(client):
    # 站点 1 产物 {200, 500, 700}（距离 100/100/300）；
    # 站点 2 产物 {500, 800, 900, 1400, 1800, 2000}（距离 500/200/100/100/300/500）。
    # tolerance=10 时跨站仅 500~500 配对 -> 单簇
    _make_site(client, 1, carriers_hz=[100, 300, 400], scan_end_hz=10_000)
    _make_site(client, 2, carriers_hz=[1000, 1200, 1500], scan_end_hz=10_000)
    a1 = _analyze(client, 1)
    a2 = _analyze(client, 2)

    resp = client.post(
        "/cross-site-conflicts", json={"site_ids": [2, 1], "tolerance_hz": 10}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "site_ids": [2, 1],  # 回显保持请求顺序
        "tolerance_hz": 10,
        "analyses": [
            {"site_id": 2, "analysis_id": a2},
            {"site_id": 1, "analysis_id": a1},
        ],
        "cluster_count": 1,
        "clusters": [
            {
                "cluster_id": 1,
                "site_ids": [1, 2],
                "products": [
                    {"site_id": 1, "freq_hz": 500, "distance_hz": 100},
                    {"site_id": 2, "freq_hz": 500, "distance_hz": 500},
                ],
                "min_distance_hz": 100,
                "pair_count": 1,
                "pairs": [
                    {
                        "site_a_id": 1,
                        "product_a_freq_hz": 500,
                        "site_b_id": 2,
                        "product_b_freq_hz": 500,
                        "frequency_delta_hz": 0,
                    }
                ],
            }
        ],
    }


def test_cross_site_conflicts_transitive_chaining(client):
    # 站点 1: 990/1020；站点 2: 1000/1030；站点 3: 1010/1040，tolerance=10。
    # 990-1000-1010-1020 沿跨站边链式相连（站点 1 的 990/1020 同站不直连，
    # 但二者经站点 2、站点 3 的跨站配对桥接），6 个节点合为一簇；
    # 站点 4(4990/5020)、站点 5(5000/5030) 各自独立成两簇。
    _make_site(client, 1, carriers_hz=[1000, 1010], scan_start_hz=900, scan_end_hz=1100)
    _make_site(client, 2, carriers_hz=[1010, 1020], scan_start_hz=900, scan_end_hz=1100)
    _make_site(client, 3, carriers_hz=[1020, 1030], scan_start_hz=900, scan_end_hz=1100)
    _make_site(client, 4, carriers_hz=[5000, 5010], scan_start_hz=900, scan_end_hz=6000)
    _make_site(client, 5, carriers_hz=[5010, 5020], scan_start_hz=900, scan_end_hz=6000)
    for site_id in range(1, 6):
        _analyze(client, site_id)

    resp = client.post(
        "/cross-site-conflicts", json={"site_ids": [5, 1, 4, 3, 2], "tolerance_hz": 10}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # analyses 与回显 site_ids 同序
    assert [a["site_id"] for a in body["analyses"]] == [5, 1, 4, 3, 2]
    assert [a["analysis_id"] for a in body["analyses"]] == [5, 1, 4, 3, 2]
    assert body["cluster_count"] == 3

    first = body["clusters"][0]
    assert first["cluster_id"] == 1
    assert first["site_ids"] == [1, 2, 3]
    assert [(p["site_id"], p["freq_hz"]) for p in first["products"]] == [
        (1, 990), (1, 1020), (2, 1000), (2, 1030), (3, 1010), (3, 1040)
    ]
    assert first["min_distance_hz"] == 10
    assert first["pair_count"] == 5
    assert first["pairs"] == [
        {"site_a_id": 1, "product_a_freq_hz": 990, "site_b_id": 2,
         "product_b_freq_hz": 1000, "frequency_delta_hz": 10},
        {"site_a_id": 1, "product_a_freq_hz": 1020, "site_b_id": 2,
         "product_b_freq_hz": 1030, "frequency_delta_hz": 10},
        {"site_a_id": 1, "product_a_freq_hz": 1020, "site_b_id": 3,
         "product_b_freq_hz": 1010, "frequency_delta_hz": 10},
        {"site_a_id": 2, "product_a_freq_hz": 1000, "site_b_id": 3,
         "product_b_freq_hz": 1010, "frequency_delta_hz": 10},
        {"site_a_id": 2, "product_a_freq_hz": 1030, "site_b_id": 3,
         "product_b_freq_hz": 1040, "frequency_delta_hz": 10},
    ]

    # 后两簇仅涉及站点 4、5；同站的 4990/5020（差 30）不直连，不合并
    assert [cl["site_ids"] for cl in body["clusters"][1:]] == [[4, 5], [4, 5]]
    assert [
        sorted(p["freq_hz"] for p in cl["products"]) for cl in body["clusters"][1:]
    ] == [[4990, 5000], [5020, 5030]]
    assert [cl["cluster_id"] for cl in body["clusters"]] == [1, 2, 3]


def test_cross_site_conflicts_within_site_products_do_not_bridge(client):
    # 站点 2 的同站产物 906/910（差 4 <= tolerance）不直接连边：
    # 900~906 与 910~914 本可借同站近频桥接成一簇，实际必须保持两簇。
    _make_site(client, 1, carriers_hz=[896, 892], scan_start_hz=899, scan_end_hz=901)
    _make_site(client, 2, carriers_hz=[900, 894, 890], scan_start_hz=905, scan_end_hz=911)
    _make_site(client, 3, carriers_hz=[912, 910], scan_start_hz=913, scan_end_hz=915)
    for site_id in (1, 2, 3):
        _analyze(client, site_id)

    body = client.post(
        "/cross-site-conflicts", json={"site_ids": [1, 2, 3], "tolerance_hz": 6}
    ).json()
    assert body["cluster_count"] == 2
    assert body["clusters"][0] == {
        "cluster_id": 1,
        "site_ids": [1, 2],
        "products": [
            {"site_id": 1, "freq_hz": 900, "distance_hz": 4},
            {"site_id": 2, "freq_hz": 906, "distance_hz": 6},
        ],
        "min_distance_hz": 4,
        "pair_count": 1,
        "pairs": [
            {"site_a_id": 1, "product_a_freq_hz": 900, "site_b_id": 2,
             "product_b_freq_hz": 906, "frequency_delta_hz": 6}
        ],
    }
    assert body["clusters"][1]["site_ids"] == [2, 3]
    assert body["clusters"][1]["min_distance_hz"] == 2  # min(10, 2)


def test_cross_site_conflicts_empty_when_no_nearby_products(client):
    # 站点 1 产物 {200,500,700}；站点 2 产物 {2900,3200}：无频率差 <= 1000 的配对
    _make_site(client, 1, carriers_hz=[100, 300, 400], scan_end_hz=10_000)
    _make_site(client, 2, carriers_hz=[3000, 3100], scan_end_hz=10_000)
    for site_id in (1, 2):
        _analyze(client, site_id)

    body = client.post(
        "/cross-site-conflicts", json={"site_ids": [1, 2], "tolerance_hz": 1000}
    ).json()
    assert body["cluster_count"] == 0
    assert body["clusters"] == []
    assert [a["site_id"] for a in body["analyses"]] == [1, 2]


def test_cross_site_conflicts_zero_tolerance_means_exact_match_only(client):
    # tolerance_hz=0：仅同频 500~500 配对；700 与 500（差 200）不配对
    _make_site(client, 1, carriers_hz=[100, 300, 400], scan_end_hz=10_000)
    _make_site(client, 2, carriers_hz=[1000, 1500], scan_end_hz=10_000)
    for site_id in (1, 2):
        _analyze(client, site_id)

    body = client.post(
        "/cross-site-conflicts", json={"site_ids": [1, 2], "tolerance_hz": 0}
    ).json()
    assert body["cluster_count"] == 1
    assert body["clusters"][0]["pairs"] == [
        {"site_a_id": 1, "product_a_freq_hz": 500, "site_b_id": 2,
         "product_b_freq_hz": 500, "frequency_delta_hz": 0}
    ]


def test_cross_site_conflicts_uses_latest_analysis(client):
    # 读取各站点最近一次分析：重复分析后 analysis_id 应取最新一条
    _make_site(client, 1, carriers_hz=[100, 300, 400], scan_end_hz=10_000)
    _make_site(client, 2, carriers_hz=[1000, 1200, 1500], scan_end_hz=10_000)
    first_1 = _analyze(client, 1)
    _analyze(client, 2)
    latest_1 = _analyze(client, 1)  # 站点 1 的第二次分析
    latest_2 = _analyze(client, 2)
    assert latest_1 > first_1

    body = client.post(
        "/cross-site-conflicts", json={"site_ids": [1, 2], "tolerance_hz": 10}
    ).json()
    assert body["analyses"] == [
        {"site_id": 1, "analysis_id": latest_1},
        {"site_id": 2, "analysis_id": latest_2},
    ]
    # 同一请求重复调用结果确定可复现
    again = client.post(
        "/cross-site-conflicts", json={"site_ids": [1, 2], "tolerance_hz": 10}
    ).json()
    assert again["clusters"] == body["clusters"]


def test_cross_site_conflicts_404_missing_site(client):
    _make_site(client, 1)
    _analyze(client, 1)
    # 任一站点不存在 -> 404（即使其他站点合法）
    resp = client.post("/cross-site-conflicts", json={"site_ids": [1, 999], "tolerance_hz": 10})
    assert resp.status_code == 404
    assert "999" in resp.json()["detail"]


def test_cross_site_conflicts_404_site_without_analysis(client):
    _make_site(client, 1)
    _make_site(client, 2)
    _analyze(client, 1)  # 仅站点 1 有分析
    resp = client.post("/cross-site-conflicts", json={"site_ids": [1, 2], "tolerance_hz": 10})
    assert resp.status_code == 404
    assert "2" in resp.json()["detail"]


def test_cross_site_conflicts_422_validation(client):
    _make_site(client, 1)
    _analyze(client, 1)

    def post(body):
        return client.post("/cross-site-conflicts", json=body)

    # 站点数少于 2 / 多于 8
    assert post({"site_ids": [1], "tolerance_hz": 10}).status_code == 422
    assert post({"site_ids": [1, 2, 3, 4, 5, 6, 7, 8, 9], "tolerance_hz": 10}).status_code == 422
    # 站点 ID 重复 / 非正
    assert post({"site_ids": [1, 1], "tolerance_hz": 10}).status_code == 422
    assert post({"site_ids": [1, 0], "tolerance_hz": 10}).status_code == 422
    assert post({"site_ids": [1, -2], "tolerance_hz": 10}).status_code == 422
    # tolerance_hz 缺失 / 为负 / 非整数
    assert post({"site_ids": [1, 2]}).status_code == 422
    assert post({"site_ids": [1, 2], "tolerance_hz": -1}).status_code == 422
    assert post({"site_ids": [1, 2], "tolerance_hz": 1.5}).status_code == 422
    assert post({"site_ids": [1, 2], "tolerance_hz": "x"}).status_code == 422
    # 缺整个请求体
    assert client.post("/cross-site-conflicts").status_code == 422
    # 参数非法时不查询站点：重复 ID 即使站点不存在也先由校验拒绝
    assert post({"site_ids": [998, 998], "tolerance_hz": 10}).status_code == 422


def test_cross_site_conflicts_does_not_write_analysis(client):
    # 检测只读取已保存分析，不产生新的分析记录
    _make_site(client, 1, carriers_hz=[100, 300, 400], scan_end_hz=10_000)
    _make_site(client, 2, carriers_hz=[1000, 1500], scan_end_hz=10_000)
    a1 = _analyze(client, 1)
    a2 = _analyze(client, 2)
    client.post("/cross-site-conflicts", json={"site_ids": [1, 2], "tolerance_hz": 10})
    assert [m["analysis_id"] for m in client.get("/sites/1/analyses").json()] == [a1]
    assert [m["analysis_id"] for m in client.get("/sites/2/analyses").json()] == [a2]


def test_cross_site_conflicts_eight_sites_limit(client):
    # 8 个站点为允许上限：站点间产物互不相近 -> 200 空簇响应
    for i in range(1, 9):
        _make_site(
            client, i,
            carriers_hz=[10_000 * i, 10_000 * i + 100],
            scan_start_hz=1,
            scan_end_hz=100_000,
        )
        _analyze(client, i)
    resp = client.post(
        "/cross-site-conflicts",
        json={"site_ids": list(range(1, 9)), "tolerance_hz": 10},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cluster_count"] == 0
    assert len(resp.json()["analyses"]) == 8


# ---------------------------------------------------------------- 多站点原子批量分析

def test_batch_analysis_creates_results_in_input_order(client):
    # 三个站点产物不同：按输入顺序 [2, 1, 3] 返回，列表顺序与内容均一致；
    # 每站新增一条分析记录，按写入顺序分配递增的 analysis_id。
    _make_site(client, 1, carriers_hz=[100, 300, 400], scan_end_hz=10_000)
    _make_site(client, 2, carriers_hz=[1000, 1200, 1500], scan_end_hz=10_000)
    _make_site(client, 3, carriers_hz=[200, 500], scan_start_hz=500, scan_end_hz=900)

    resp = client.post("/sites/analyses/batch", json={"site_ids": [2, 1, 3]})
    assert resp.status_code == 201, resp.text
    results = resp.json()
    assert [r["site_id"] for r in results] == [2, 1, 3]
    assert [r["freq_hz"] for r in results[0]["products"]] == [
        500,
        800,
        900,
        1400,
        1800,
        2000,
    ]
    assert [p["freq_hz"] for p in results[1]["products"]] == [200, 500, 700]
    assert [p["freq_hz"] for p in results[2]["products"]] == [800]
    # 按输入顺序逐站写入：analysis_id 递增
    assert [r["analysis_id"] for r in results] == sorted(r["analysis_id"] for r in results)

    # 每条结果都已持久化，且就是对应站点的最近一次分析
    for result in results:
        assert client.get(
            f"/sites/{result['site_id']}/analysis"
        ).json() == result
        assert client.get(
            f"/analyses/{result['analysis_id']}"
        ).json() == result
        assert len(client.get(f"/sites/{result['site_id']}/analyses").json()) == 1


def test_batch_analysis_single_site(client):
    # 单元素批次：结果与单站接口（不带 Idempotency-Key）的分析内容一致
    client.post("/sites", json=make_payload())
    batch = client.post("/sites/analyses/batch", json={"site_ids": [1]})
    assert batch.status_code == 201, batch.text
    batch_body = batch.json()[0]

    single = client.post("/sites/1/analysis")
    assert single.status_code == 201
    assert single.json()["products"] == batch_body["products"]
    assert single.json()["analysis_id"] > batch_body["analysis_id"]
    # 两次调用各产生一条记录
    assert [m["analysis_id"] for m in client.get("/sites/1/analyses").json()] == [
        batch_body["analysis_id"],
        single.json()["analysis_id"],
    ]


def test_batch_analysis_accepts_ten_sites(client):
    # 10 个站点为允许上限
    for i in range(1, 11):
        _make_site(client, i)
    resp = client.post(
        "/sites/analyses/batch", json={"site_ids": list(range(10, 0, -1))}
    )
    assert resp.status_code == 201, resp.text
    assert [r["site_id"] for r in resp.json()] == list(range(10, 0, -1))
    for i in range(1, 11):
        assert len(client.get(f"/sites/{i}/analyses").json()) == 1


def test_batch_analysis_422_validation(client):
    def post(body):
        return client.post("/sites/analyses/batch", json=body)

    # 空列表 / 超过 10 个
    assert post({"site_ids": []}).status_code == 422
    assert post({"site_ids": list(range(1, 12))}).status_code == 422
    # 重复 / 非正整数
    assert post({"site_ids": [1, 1]}).status_code == 422
    assert post({"site_ids": [1, 0]}).status_code == 422
    assert post({"site_ids": [1, -2]}).status_code == 422
    # 缺整个请求体 / 缺字段
    assert client.post("/sites/analyses/batch").status_code == 422
    assert post({}).status_code == 422
    assert post({"site_ids": ["x"]}).status_code == 422
    assert post({"site_ids": [1.5]}).status_code == 422
    # 校验先于站点查询：重复 ID 即使站点不存在也先 422
    assert post({"site_ids": [998, 998]}).status_code == 422


def test_batch_analysis_404_missing_site_saves_nothing(client):
    _make_site(client, 1)
    _make_site(client, 2)
    # 任一站点不存在 -> 404（即使其他站点存在）
    resp = client.post("/sites/analyses/batch", json={"site_ids": [2, 999, 1]})
    assert resp.status_code == 404
    assert "999" in resp.json()["detail"]
    # 整批不保存：所有站点（包括存在的）都没有分析记录
    for site_id in (1, 2):
        assert client.get(f"/sites/{site_id}/analysis").status_code == 404
        assert client.get(f"/sites/{site_id}/analyses").json() == []


def test_batch_analysis_rolls_back_whole_batch_on_failure(client, monkeypatch):
    # 计算过程中任一步失败：已计算/已写入的前序站点结果全部回滚
    _make_site(client, 1, carriers_hz=[100, 300, 400])
    _make_site(client, 2, carriers_hz=[1000, 1200, 1500])
    from app import main as main_module

    real_analyze = main_module.analyze

    def fail_on_site_2(**kwargs):
        # 路由层以关键字参数调用 analyze（与单站接口一致）；按载频区分站点
        if kwargs["carriers_hz"] == [1000, 1200, 1500]:
            raise RuntimeError("boom during batch analysis")
        return real_analyze(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(main_module, "analyze", fail_on_site_2)
        with pytest.raises(Exception, match="boom"):
            client.post("/sites/analyses/batch", json={"site_ids": [1, 2]})

    # 站点 1 虽先完成计算，但事务回滚后没有任何分析残留
    assert client.get("/sites/1/analyses").json() == []
    assert client.get("/sites/2/analyses").json() == []

    # 恢复后整批重试：两站各成功保存一条
    retry = client.post("/sites/analyses/batch", json={"site_ids": [2, 1]})
    assert retry.status_code == 201, retry.text
    assert [r["site_id"] for r in retry.json()] == [2, 1]
    assert len(client.get("/sites/1/analyses").json()) == 1
    assert len(client.get("/sites/2/analyses").json()) == 1


def test_batch_analysis_does_not_affect_single_site_endpoint(client):
    # 批量接口不支持幂等登记：每次调用都产生新记录；单站接口行为不变
    _make_site(client, 1)
    _make_site(client, 2)
    first = client.post("/sites/analyses/batch", json={"site_ids": [1, 2]})
    second = client.post("/sites/analyses/batch", json={"site_ids": [1, 2]})
    assert first.status_code == 201 and second.status_code == 201
    for a, b in zip(first.json(), second.json()):
        assert b["analysis_id"] > a["analysis_id"]
        assert b["products"] == a["products"]  # 输入相同，分析结果确定可复现
    assert len(client.get("/sites/1/analyses").json()) == 2
    assert len(client.get("/sites/2/analyses").json()) == 2

    # 单站接口（带幂等键）行为保持原样
    single = client.post("/sites/1/analysis", headers={"Idempotency-Key": "k1"})
    assert single.status_code == 201
    replay = client.post("/sites/1/analysis", headers={"Idempotency-Key": "k1"})
    assert replay.status_code == 200
    assert replay.json() == single.json()


# ---------------------------------------------------------------- 载频漂移风险评估

def _drift_body(tolerances):
    """tolerances: (carrier_hz, tolerance_hz) 列表 -> 请求体（保留重复条目）。"""
    return {
        "tolerances": [
            {"carrier_hz": carrier, "tolerance_hz": tolerance}
            for carrier, tolerance in tolerances
        ]
    }


def test_drift_risk_merges_shared_drift_variables(client):
    # 载频 [1000, 1300]，容差 {1000: 100, 1300: 50}，g=200。
    # 来源 (1000,1300)：标称 700，可达区间 [450, 950]；与载频 1000（c==fa）
    # 比较时 δ_a 系数合并为 2-1=1，半径 100+50=150，最小距离
    # max(0, 300-150)=150（若误把产物区间与载频区间当独立区间，半径 350
    # 会错得 0）；与载频 1300（c==fb）半径 2*100+2*50=300，距离 300。
    # 来源 (1300,1000)：标称 1600，可达 [1400, 1800]，最小距离 150。
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 1300], guard_interval_hz=200, scan_end_hz=100000,
    ))
    resp = client.post(
        "/sites/1/drift-risk", json=_drift_body([(1000, 100), (1300, 50)])
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "site_id": 1,
        "guard_interval_hz": 200,
        "risk_count": 2,
        "risks": [
            {
                "source": {"fa_hz": 1000, "fb_hz": 1300},
                "nominal_freq_hz": 700,
                "reachable_start_hz": 450,
                "reachable_end_hz": 950,
                "min_distance_hz": 150,
                "hits": [
                    {"type": "carrier", "carrier_hz": 1000, "guard_band": None}
                ],
            },
            {
                "source": {"fa_hz": 1300, "fb_hz": 1000},
                "nominal_freq_hz": 1600,
                "reachable_start_hz": 1400,
                "reachable_end_hz": 1800,
                "min_distance_hz": 150,
                "hits": [
                    {"type": "carrier", "carrier_hz": 1300, "guard_band": None}
                ],
            },
        ],
    }


def test_drift_risk_reports_all_tied_hits_and_ignores_scan_range(client):
    # 零容差退化为静态距离。载频 [400, 600, 1000]，g=250，扫描范围仅
    # [1, 300]：全频域评估不按扫描范围过滤，标称 800 的来源仍参与。
    # (600,400)：标称 800，到载频 600 与 1000 的距离同为 200 -> 并列命中；
    # (400,600) 与 (600,1000) 标称同为 200，按 (标称频率, fa, fb) 排序。
    client.post("/sites", json=make_payload(
        carriers_hz=[400, 600, 1000], scan_end_hz=300, guard_interval_hz=250,
    ))
    resp = client.post(
        "/sites/1/drift-risk", json=_drift_body([(400, 0), (600, 0), (1000, 0)])
    )
    assert resp.status_code == 200, resp.text
    risks = resp.json()["risks"]
    assert [
        (r["nominal_freq_hz"], r["source"]["fa_hz"], r["source"]["fb_hz"])
        for r in risks
    ] == [(200, 400, 600), (200, 600, 1000), (800, 600, 400)]
    # 零容差时可达区间退化为标称频率单点
    assert risks[0]["reachable_start_hz"] == risks[0]["reachable_end_hz"] == 200
    assert risks[0]["min_distance_hz"] == 200
    assert risks[0]["hits"] == [
        {"type": "carrier", "carrier_hz": 400, "guard_band": None}
    ]
    # 并列命中：载频 600 与 1000 距离同为 200，按载频升序列出
    assert risks[2]["min_distance_hz"] == 200
    assert risks[2]["hits"] == [
        {"type": "carrier", "carrier_hz": 600, "guard_band": None},
        {"type": "carrier", "carrier_hz": 1000, "guard_band": None},
    ]


def test_drift_risk_tied_hits_include_guard_band(client):
    # 零容差，载频 [1000, 1400]，保护频段 [100, 200]，g=500。
    # 来源 (1000,1400)：标称 600，到载频 1000 与保护频段 [100,200] 的距离
    # 同为 400 -> 并列命中（载频在前，保护频段在后）
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 1400],
        guard_bands=[{"start_hz": 100, "end_hz": 200}],
        guard_interval_hz=500,
        scan_end_hz=100000,
    ))
    resp = client.post(
        "/sites/1/drift-risk", json=_drift_body([(1000, 0), (1400, 0)])
    )
    assert resp.status_code == 200, resp.text
    risks = resp.json()["risks"]
    assert [r["nominal_freq_hz"] for r in risks] == [600, 1800]
    assert risks[0]["min_distance_hz"] == 400
    assert risks[0]["hits"] == [
        {"type": "carrier", "carrier_hz": 1000, "guard_band": None},
        {
            "type": "guard_band",
            "carrier_hz": None,
            "guard_band": {"start_hz": 100, "end_hz": 200},
        },
    ]
    assert risks[1]["hits"] == [
        {"type": "carrier", "carrier_hz": 1400, "guard_band": None}
    ]


def test_drift_risk_guard_band_interval_distance(client):
    # 载频 [1000, 1300]，容差 {1000: 60, 1300: 0}，保护频段 [750, 900]，
    # g=250。来源 (1000,1300)：标称 700，可达 [580, 820]，与频段重叠 ->
    # 区间距离 0。来源 (1300,1000)：标称 1600，可达 [1540, 1660]，到频段
    # 距离 640、到载频 1300（c==fa，半径 60）距离 240 -> 最小 240。
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 1300],
        guard_bands=[{"start_hz": 750, "end_hz": 900}],
        guard_interval_hz=250,
        scan_end_hz=100000,
    ))
    resp = client.post(
        "/sites/1/drift-risk", json=_drift_body([(1000, 60), (1300, 0)])
    )
    assert resp.status_code == 200, resp.text
    risks = resp.json()["risks"]
    assert [r["nominal_freq_hz"] for r in risks] == [700, 1600]
    assert risks[0]["min_distance_hz"] == 0
    assert risks[0]["hits"] == [
        {
            "type": "guard_band",
            "carrier_hz": None,
            "guard_band": {"start_hz": 750, "end_hz": 900},
        }
    ]
    assert risks[1]["min_distance_hz"] == 240
    assert risks[1]["hits"] == [
        {"type": "carrier", "carrier_hz": 1300, "guard_band": None}
    ]


def test_drift_risk_strict_guard_interval_threshold(client):
    # 严格小于：最小可能距离恰好等于 guard_interval_hz 的来源不返回。
    # 零容差，载频 [1000, 1400]：两个来源的最小距离都是 400。
    client.post("/sites", json=make_payload(
        carriers_hz=[1000, 1400], guard_interval_hz=400, scan_end_hz=100000,
    ))
    body = _drift_body([(1000, 0), (1400, 0)])
    resp = client.post("/sites/1/drift-risk", json=body)
    assert resp.status_code == 200
    assert resp.json()["risks"] == []
    assert resp.json()["risk_count"] == 0
    # 保护间隔放宽到 401 时两个来源都进入结果
    client.post("/sites", json=make_payload(
        name="site-b",
        carriers_hz=[1000, 1400],
        guard_interval_hz=401,
        scan_end_hz=100000,
    ))
    assert client.post("/sites/2/drift-risk", json=body).json()["risk_count"] == 2


def test_drift_risk_full_response_example(client):
    # README 示例：载频 [100, 300, 400]，保护频段 [480, 520]，g=150，
    # 容差 {100: 10, 300: 20, 400: 30}
    client.post("/sites", json=make_payload(
        guard_bands=[{"start_hz": 480, "end_hz": 520}],
    ))
    resp = client.post(
        "/sites/1/drift-risk",
        json=_drift_body([(100, 10), (300, 20), (400, 30)]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "site_id": 1,
        "guard_interval_hz": 150,
        "risk_count": 4,
        "risks": [
            {
                "source": {"fa_hz": 300, "fb_hz": 400},
                "nominal_freq_hz": 200,
                "reachable_start_hz": 130,
                "reachable_end_hz": 270,
                "min_distance_hz": 20,
                "hits": [
                    {"type": "carrier", "carrier_hz": 100, "guard_band": None}
                ],
            },
            {
                "source": {"fa_hz": 300, "fb_hz": 100},
                "nominal_freq_hz": 500,
                "reachable_start_hz": 450,
                "reachable_end_hz": 550,
                "min_distance_hz": 0,
                "hits": [
                    {
                        "type": "guard_band",
                        "carrier_hz": None,
                        "guard_band": {"start_hz": 480, "end_hz": 520},
                    }
                ],
            },
            {
                "source": {"fa_hz": 400, "fb_hz": 300},
                "nominal_freq_hz": 500,
                "reachable_start_hz": 420,
                "reachable_end_hz": 580,
                "min_distance_hz": 0,
                "hits": [
                    {
                        "type": "guard_band",
                        "carrier_hz": None,
                        "guard_band": {"start_hz": 480, "end_hz": 520},
                    }
                ],
            },
            {
                "source": {"fa_hz": 400, "fb_hz": 100},
                "nominal_freq_hz": 700,
                "reachable_start_hz": 630,
                "reachable_end_hz": 770,
                "min_distance_hz": 110,
                "hits": [
                    {
                        "type": "guard_band",
                        "carrier_hz": None,
                        "guard_band": {"start_hz": 480, "end_hz": 520},
                    }
                ],
            },
        ],
    }


def test_drift_risk_404(client):
    resp = client.post("/sites/999/drift-risk", json=_drift_body([(100, 0)]))
    assert resp.status_code == 404


def test_drift_risk_mapping_validation(client):
    client.post("/sites", json=make_payload())  # 载频 [100, 300, 400]
    url = "/sites/1/drift-risk"
    # 边界值 0 与 1000000 合法
    ok = _drift_body([(100, 0), (300, 10), (400, 1_000_000)])
    assert client.post(url, json=ok).status_code == 200
    # 缺失载频 400 / 多余载频 500 / 重复载频 100
    assert client.post(url, json=_drift_body([(100, 0), (300, 0)])).status_code == 422
    assert (
        client.post(
            url, json=_drift_body([(100, 0), (300, 0), (400, 0), (500, 0)])
        ).status_code
        == 422
    )
    assert (
        client.post(
            url, json=_drift_body([(100, 0), (100, 1), (300, 0), (400, 0)])
        ).status_code
        == 422
    )
    # 容差越界 / 非整数 / 缺失
    assert (
        client.post(url, json=_drift_body([(100, -1), (300, 0), (400, 0)])).status_code
        == 422
    )
    assert (
        client.post(
            url, json=_drift_body([(100, 1_000_001), (300, 0), (400, 0)])
        ).status_code
        == 422
    )
    assert (
        client.post(url, json=_drift_body([(100, 1.5), (300, 0), (400, 0)])).status_code
        == 422
    )
    assert (
        client.post(
            url, json=_drift_body([(100, "x"), (300, 0), (400, 0)])
        ).status_code
        == 422
    )
    assert (
        client.post(
            url,
            json={
                "tolerances": [
                    {"carrier_hz": 100},
                    {"carrier_hz": 300, "tolerance_hz": 0},
                    {"carrier_hz": 400, "tolerance_hz": 0},
                ]
            },
        ).status_code
        == 422
    )
    # 空映射 / 缺请求体
    assert client.post(url, json={"tolerances": []}).status_code == 422
    assert client.post(url).status_code == 422


def test_drift_risk_does_not_save_analysis(client):
    # 该端点不执行也不保存分析：调用后仍无分析记录
    client.post("/sites", json=make_payload())
    resp = client.post(
        "/sites/1/drift-risk", json=_drift_body([(100, 10), (300, 20), (400, 30)])
    )
    assert resp.status_code == 200
    assert client.get("/sites/1/analysis").status_code == 404
    assert client.get("/sites/1/analyses").json() == []


def test_drift_risk_single_carrier_site_has_no_sources(client):
    # 单载频站点没有有序来源组合：合法映射下返回空风险列表
    client.post("/sites", json=make_payload(carriers_hz=[1000]))
    resp = client.post("/sites/1/drift-risk", json=_drift_body([(1000, 100)]))
    assert resp.status_code == 200
    assert resp.json()["risk_count"] == 0
    assert resp.json()["risks"] == []


# ---------------------------------------------------------------- 站点历史风险

def _set_analysis_products(client, analysis_id, products):
    """直接改写已保存分析的产物（测试构造历史违规形态，不经过分析计算）。"""
    import json
    import sqlite3

    from app import database

    con = sqlite3.connect(database.db_path())
    try:
        con.execute(
            "UPDATE analyses SET result_json = ?, product_count = ? WHERE id = ?",
            (json.dumps(products), len(products), analysis_id),
        )
        con.commit()
    finally:
        con.close()


def _mark_violations(client, analysis_id, violating_freqs):
    """把该分析中指定频率标记为违规，其余标记为非违规。"""
    products = client.get(f"/analyses/{analysis_id}").json()["products"]
    for product in products:
        product["violates_guard_interval"] = product["freq_hz"] in violating_freqs
    _set_analysis_products(client, analysis_id, products)


def _set_analysis_created_at(client, analysis_id, created_at):
    import sqlite3

    from app import database

    con = sqlite3.connect(database.db_path())
    try:
        con.execute(
            "UPDATE analyses SET created_at = ? WHERE id = ?", (created_at, analysis_id)
        )
        con.commit()
    finally:
        con.close()


def test_risk_history_counts_last_seen_and_consecutive_streak(client):
    # 默认站点产物为 200(违规)/500(违规)/700(非违规)。4 条分析（旧 -> 新）：
    #   a0:{200}  a1:{500}  a2:{200,500}  a3:{200}
    # 200：违规 3 次，最近为 a3，连续违规从 a3 起经 a2 到 a1 中断 -> 2
    # 500：违规 2 次，最近为 a2，最新 a3 即非违规 -> 连续 0
    client.post("/sites", json=make_payload())
    ids = _create_n_analyses(client, 1, 4)
    a0, a1, a2, a3 = ids
    _mark_violations(client, a0, {200})
    _mark_violations(client, a1, {500})
    _mark_violations(client, a2, {200, 500})
    _mark_violations(client, a3, {200})
    created_at = {
        m["analysis_id"]: m["created_at"]
        for m in client.get("/sites/1/analyses").json()
    }

    resp = client.get("/sites/1/risk-history", params={"limit": 4})
    assert resp.status_code == 200, resp.text
    assert resp.json() == [
        {
            "freq_hz": 200,
            "violation_count": 3,
            "last_violated_at": created_at[a3],
            "consecutive_count": 2,
        },
        {
            "freq_hz": 500,
            "violation_count": 2,
            "last_violated_at": created_at[a2],
            "consecutive_count": 0,
        },
    ]


def test_risk_history_limit_takes_recent_window(client):
    # 同上 4 条历史，limit 只取最近 N 条：统计范围与连续计数都不越过窗口
    client.post("/sites", json=make_payload())
    ids = _create_n_analyses(client, 1, 4)
    a0, a1, a2, a3 = ids
    _mark_violations(client, a0, {200})
    _mark_violations(client, a1, {500})
    _mark_violations(client, a2, {200, 500})
    _mark_violations(client, a3, {200})
    created_at = {
        m["analysis_id"]: m["created_at"]
        for m in client.get("/sites/1/analyses").json()
    }

    # 窗口 [a3, a2]：200 两次全中且连续 2；500 仅 a2 一次，最新 a3 非违规 -> 0
    body = client.get("/sites/1/risk-history", params={"limit": 2}).json()
    assert body == [
        {
            "freq_hz": 200,
            "violation_count": 2,
            "last_violated_at": created_at[a3],
            "consecutive_count": 2,
        },
        {
            "freq_hz": 500,
            "violation_count": 1,
            "last_violated_at": created_at[a2],
            "consecutive_count": 0,
        },
    ]

    # 窗口仅 [a3]：500 在窗口内未违规，不出现在结果中
    body = client.get("/sites/1/risk-history", params={"limit": 1}).json()
    assert body == [
        {
            "freq_hz": 200,
            "violation_count": 1,
            "last_violated_at": created_at[a3],
            "consecutive_count": 1,
        }
    ]

    # limit 大于分析总数：等价于取全部历史
    body = client.get("/sites/1/risk-history", params={"limit": 100}).json()
    assert [e["freq_hz"] for e in body] == [200, 500]
    assert body[0]["violation_count"] == 3


def test_risk_history_orders_by_created_at_then_id(client):
    # 最近 N 条按 created_at 主、id 辅倒序选取：令 a0 与 a2 同一时刻（id 大者
    # 为新），a1 更旧。limit=2 的窗口为 [a2, a0] 而非 [a1, ...]。
    client.post("/sites", json=make_payload())
    a0, a1, a2 = _create_n_analyses(client, 1, 3)
    _mark_violations(client, a0, {200})
    _mark_violations(client, a1, {500})
    _mark_violations(client, a2, {200, 500})
    ts_tie = "2026-03-01T00:00:00.000Z"
    _set_analysis_created_at(client, a0, ts_tie)
    _set_analysis_created_at(client, a2, ts_tie)
    _set_analysis_created_at(client, a1, "2026-01-01T00:00:00.000Z")

    body = client.get("/sites/1/risk-history", params={"limit": 2}).json()
    assert body == [
        {
            "freq_hz": 200,
            "violation_count": 2,
            "last_violated_at": ts_tie,
            "consecutive_count": 2,
        },
        {
            "freq_hz": 500,
            "violation_count": 1,  # 仅 a2；a1 落在窗口外
            "last_violated_at": ts_tie,
            # 最新 a2 违规，次新 a0 非违规即终止 -> 1（若并列时刻误用 id 升序，
            # 窗口顺序变成 [a0, a2]，连续数会错误地为 0）
            "consecutive_count": 1,
        },
    ]


def test_risk_history_empty_for_no_analysis_or_no_violation(client):
    # 站点存在但无任何分析：空列表（200，不报错）
    client.post("/sites", json=make_payload())
    resp = client.get("/sites/1/risk-history", params={"limit": 10})
    assert resp.status_code == 200
    assert resp.json() == []

    # guard_interval_hz=0：所有产物都非违规，窗口内无违规频率 -> 空列表
    client.post("/sites", json=make_payload(name="site-b", guard_interval_hz=0))
    _create_n_analyses(client, 2, 3)
    resp = client.get("/sites/2/risk-history", params={"limit": 3})
    assert resp.status_code == 200
    assert resp.json() == []


def test_risk_history_read_only_does_not_trigger_analysis(client):
    # 只读已保存结果：调用前后分析记录完全不变，不产生新分析
    client.post("/sites", json=make_payload())
    ids = _create_n_analyses(client, 1, 2)
    before = client.get("/sites/1/analyses").json()
    for _ in range(3):
        resp = client.get("/sites/1/risk-history", params={"limit": 1})
        assert resp.status_code == 200
    assert client.get("/sites/1/analyses").json() == before
    assert client.get("/sites/1/analysis").json()["analysis_id"] == ids[-1]


def test_risk_history_404_missing_site(client):
    assert client.get("/sites/999/risk-history", params={"limit": 1}).status_code == 404


def test_risk_history_422_invalid_limit(client):
    client.post("/sites", json=make_payload())
    url = "/sites/1/risk-history"
    # 缺少 limit
    assert client.get(url).status_code == 422
    # 越界（0、负数、101）/ 非整数 -> 422
    for bad in (0, -1, 101, 1000, "abc", 1.5):
        assert client.get(url, params={"limit": bad}).status_code == 422, bad
    # 边界值 1 与 100 合法
    assert client.get(url, params={"limit": 1}).status_code == 200
    assert client.get(url, params={"limit": 100}).status_code == 200
    # 参数校验先于站点查询：站点不存在且 limit 非法时返回 422 而非 404
    assert client.get("/sites/999/risk-history", params={"limit": 0}).status_code == 422


def test_build_risk_history_treats_absent_product_as_streak_break():
    # 直接对汇总函数建模：某频率在一条分析中连产物都不出现，同样终止连续计数。
    from app.analysis import build_risk_history
    from app.schemas import AnalysisResult, ProductOut

    def product(freq, violates):
        return ProductOut(
            freq_hz=freq,
            distance_hz=0,
            hit={"type": "carrier", "carrier_hz": freq, "guard_band": None},
            violates_guard_interval=violates,
            sources=[{"fa_hz": freq, "fb_hz": freq + 1}],
        )

    def analysis(analysis_id, pairs):
        return AnalysisResult(
            analysis_id=analysis_id,
            site_id=1,
            created_at=f"2026-01-0{analysis_id}T00:00:00.000Z",
            product_count=len(pairs),
            products=[product(f, v) for f, v in pairs],
        )

    # 从新到旧：A={100 违规, 200 违规}，B={200 违规}（100 缺席），
    # C={100 违规, 200 非违规}
    results = [
        analysis(3, [(100, True), (200, True)]),
        analysis(2, [(200, True)]),
        analysis(1, [(100, True), (200, False)]),
    ]
    assert [e.model_dump() for e in build_risk_history(results)] == [
        {
            "freq_hz": 100,
            "violation_count": 2,
            "last_violated_at": "2026-01-03T00:00:00.000Z",
            "consecutive_count": 1,  # B 中 100 缺席，连续计数终止
        },
        {
            "freq_hz": 200,
            "violation_count": 2,
            "last_violated_at": "2026-01-03T00:00:00.000Z",
            "consecutive_count": 2,  # A、B 连续违规，C 非违规终止
        },
    ]
    assert build_risk_history([]) == []

