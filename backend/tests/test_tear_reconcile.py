"""
19 事故「过机成功灯与目的地证据撕裂」收口夹具。

全局唯一口径（架构一，ADR-002）：成功灯必须能由
目的地谓词 ∪ 扫描册正式行 ∪ 待路由水位 ∪ 过机审计 同命运重放成立；
任一侧不成立即整笔回滚/暴露，禁止回执先亮、缺口另挂的架构二。

钉死的夹具：
- 单号亮目的地空（回执成功而目的地探针为空的假过机）
- 册页缺 / 水位涨 / 审计成功而总览不加 的四侧分叉
- 同条码连点（一亮一僵）与半截回滚后不得回执绿而无目的地可裁决行
- 过机与补码交错时回执与落账同命运
- 分拣线路由落入前闸、口门、回流、封袋、班次、总览、审计的同刷重放对账
"""
from __future__ import annotations

import threading

import pytest

from app.db import db
from app.services.loading import REASON_MISSING_DEST, load_scan
from app.services.replay import replay_shift


# ---------- 夹具 1：单号亮、目的地空 —— 缺码件绝不发成功灯 ----------
def test_单号亮目的地空_缺码件只挂待补码不亮成功灯(client):
    r = client.post("/扫描", json={"条码": "TEAR-DEST-EMPTY", "目的地码": "   "})
    assert r.status_code == 200, r.text
    rec = r.json()
    # 回执层：成功灯不得在缺目的地时点亮
    assert rec["装载结果"] == "待补码"
    assert rec["可裁决"] is False
    assert rec["拒因"] == REASON_MISSING_DEST
    assert rec["dest_code"].strip() == ""
    assert rec["status"] == "待补目的地"

    # 目的地探针（册页行）：目的地确实为空，且状态自洽为待补，而非伪装待路由
    row = next(s for s in client.get("/扫描").json() if s["id"] == rec["id"])
    assert row["dest_code"] == ""
    assert row["status"] == "待补目的地"
    assert row["reject_reason"] == REASON_MISSING_DEST

    # 待路由水位不涨、裁决入口硬挡、总览待补+1 且无脏行
    assert all(s["id"] != rec["id"] for s in client.get("/待路由桶").json()["明细"])
    assert client.post(f"/扫描/{rec['id']}/路由").status_code == 400
    ov = client.get("/总览").json()
    assert ov["分布"]["待路由"] == 1  # 仅种子那一笔，缺码件不涨水位
    assert ov["分布"]["待补目的地"] == 1
    assert ov["脏行数"] == 0


# ---------- 夹具 2：册页无正式行时，水位与审计都不得单边宣称成功 ----------
def test_册页缺行_水位不涨且无悬空成功审计(client):
    before_bucket = client.get("/待路由桶").json()["件数"]
    before_audit = len(client.get("/审计").json())

    # 同事务内装载后制造失败：整笔回滚，册页不留行
    with pytest.raises(RuntimeError):
        with db() as conn:
            load_scan(conn, barcode="TEAR-NO-ROW", dest_code="110", line_id=None, note="")
            raise RuntimeError("boom-before-commit")

    # 册页无该正式行
    assert all(s["barcode"] != "TEAR-NO-ROW" for s in client.get("/扫描").json())
    # 待路由水位不涨
    assert client.get("/待路由桶").json()["件数"] == before_bucket
    # 审计无该笔成功行（不允许只剩半截过机成功审计）
    audits = client.get("/审计").json()
    assert len(audits) == before_audit
    assert not any(a.get("note", "").startswith("TEAR-NO-ROW") for a in audits)

    # 重放对账：无悬空审计、四侧一致
    recon = client.get("/班次/对账").json()
    rp = recon["重放对账"]
    assert rp["一致"] is True
    assert rp["重放"]["悬空审计数"] == 0
    assert rp["撕裂件"] == []


# ---------- 夹具 3：审计记成功而总览不加 —— 成功审计必须与册页/水位同命运 ----------
def test_审计成功与总览水位_同命运不单边(client):
    rec = client.post("/扫描", json={"条码": "TEAR-AUDIT-SYNC", "目的地码": "210"}).json()
    sid = rec["id"]

    # 正常件：审计成功、册页有行、待路由水位 +1、总览待路由 +1 同时成立
    audits = client.get("/审计").json()
    load_audit = next(a for a in audits if a["ref_id"] == sid and a["action"] == "过机装载")
    assert load_audit
    assert any(s["id"] == sid for s in client.get("/待路由桶").json()["明细"])
    ov = client.get("/总览").json()
    assert ov["分布"]["待路由"] == 2  # 种子 + 本笔
    assert ov["脏行数"] == 0

    # 重放对账四侧同命运
    rp = client.get("/班次/对账").json()["重放对账"]
    assert rp["一致"] is True
    assert rp["重放"]["待路由水位_谓词重放"] == rp["重放"]["待路由水位_桶读口径"] == 2
    assert rp["重放"]["过机装载审计件数"] == 2

    # 人为制造「审计成功但册页行被抹」的撕裂：重放必须暴露悬空审计，不得静默
    with db() as conn:
        conn.execute("DELETE FROM scans WHERE id=?", (sid,))
    rp2 = client.get("/班次/对账").json()["重放对账"]
    assert rp2["一致"] is False
    assert any(t["scan_id"] == sid and "悬空" in t["撕裂"] for t in rp2["撕裂件"])


# ---------- 夹具 4：同条码连点一亮一僵 —— 唯一索引兜底，只成一笔且目的地不被抹 ----------
def test_同条码连点_一亮一僵只成一笔(client):
    a = client.post("/扫描", json={"条码": "TEAR-RAPID", "目的地码": "110"})
    b = client.post("/扫描", json={"条码": "TEAR-RAPID", "目的地码": ""})
    assert a.status_code == 200 and b.status_code == 400
    assert "重复" in b.json()["detail"]

    rows = [s for s in client.get("/扫描").json() if s["barcode"] == "TEAR-RAPID"]
    assert len(rows) == 1
    # 第二点（缺码）不得把第一点的目的地清空、不得把状态拧成待补
    assert rows[0]["dest_code"] == "110"
    assert rows[0]["status"] == "待路由"
    assert rows[0]["可裁决"] is True


def test_同条码并发连点_数据库兜底一成一拒(client):
    results, errors = [], []

    def fire():
        try:
            r = client.post("/扫描", json={"条码": "TEAR-CONCUR", "目的地码": "110"})
            results.append(r.status_code)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(results) == [200, 400]
    with db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE barcode='TEAR-CONCUR'"
        ).fetchone()["c"]
    assert n == 1


# ---------- 夹具 5：半截回滚后，不得回执绿而无目的地可裁决行 ----------
def test_半截回滚后_无绿回执无无目的地可裁决行(client):
    with pytest.raises(RuntimeError):
        with db() as conn:
            load_scan(conn, barcode="TEAR-HALF-OK", dest_code="110", line_id=None, note="")
            load_scan(conn, barcode="TEAR-HALF-MISS", dest_code="", line_id=None, note="")
            raise RuntimeError("halfway")

    with db() as conn:
        # 回滚的两件都不在册
        ghost = conn.execute(
            "SELECT * FROM scans WHERE barcode IN ('TEAR-HALF-OK','TEAR-HALF-MISS')"
        ).fetchall()
        assert ghost == []
        # 不存在「无目的地却可裁决」的行
        dirty = conn.execute(
            "SELECT * FROM scans WHERE status IN ('待路由','错分回流') AND TRIM(dest_code)=''"
        ).fetchall()
        assert dirty == []
        # 回滚事务的审计同步撤销
        n = conn.execute(
            "SELECT COUNT(*) c FROM audit_events WHERE note LIKE '%TEAR-HALF%'"
        ).fetchone()["c"]
        assert n == 0

    # 总览自检与重放对账都不报撕裂
    assert client.get("/总览").json()["脏行数"] == 0
    rp = client.get("/班次/对账").json()["重放对账"]
    assert rp["一致"] is True and rp["撕裂件"] == []


# ---------- 夹具 6：过机与补码交错 —— 回执与落账同命运，不拧花 ----------
def test_过机补码交错_回执与落账同命运(client):
    # 先一笔缺码挂账
    miss = client.post("/扫描", json={"条码": "TEAR-INTER-M", "目的地码": ""}).json()
    # 交错插入一笔正常件
    ok = client.post("/扫描", json={"条码": "TEAR-INTER-OK", "目的地码": "110"}).json()
    # 再回来给缺码件补码
    sup = client.post(f"/扫描/{miss['id']}/补码", json={"目的地码": "210"})
    assert sup.status_code == 200
    fixed = sup.json()

    # 补码回执与册页落账完全一致：同 id、目的地 210、状态待路由、可裁决
    assert fixed["id"] == miss["id"]
    assert fixed["dest_code"] == "210"
    assert fixed["status"] == "待路由"
    assert fixed["可裁决"] is True
    assert fixed["reject_reason"] == ""
    listed = next(s for s in client.get("/扫描").json() if s["id"] == miss["id"])
    assert listed["dest_code"] == "210" and listed["status"] == "待路由"
    assert listed["可裁决"] is True

    # 交错的正常件不受影响
    ok_row = next(s for s in client.get("/扫描").json() if s["id"] == ok["id"])
    assert ok_row["dest_code"] == "110" and ok_row["可裁决"] is True

    # 两件都进待路由桶、水位=种子+2；审计三段（两装载 + 一补码）齐
    bucket_ids = {s["id"] for s in client.get("/待路由桶").json()["明细"]}
    assert {miss["id"], ok["id"]} <= bucket_ids
    assert client.get("/总览").json()["分布"]["待路由"] == 3
    audits = client.get("/审计").json()
    miss_actions = {a["action"] for a in audits if a["ref_id"] == miss["id"]}
    assert {"过机装载", "补目的地码"} <= miss_actions

    rp = client.get("/班次/对账").json()["重放对账"]
    assert rp["一致"] is True


# ---------- 夹具 7：分拣线路由落入前闸 + 口门/回流/封袋/班次全链同刷 ----------
def test_路由落入前闸_闭口硬挡且全链重放一致(client):
    rec = client.post("/扫描", json={"条码": "TEAR-GATE", "目的地码": "110"}).json()
    sid = rec["id"]

    # 找到 110 对应的口门并闭口
    rule = next(r for r in client.get("/路由表").json() if r["dest_code"] == "110")
    chute_id = rule["chute_id"]
    closed = client.post(f"/口门/{chute_id}/开闭?状态=口门闭")
    assert closed.status_code == 200
    # 口门闭：落入前闸硬挡，状态不得翻成已路由
    blocked = client.post(f"/扫描/{sid}/路由")
    assert blocked.status_code == 400 and "口门已闭" in blocked.json()["detail"]
    row = next(s for s in client.get("/扫描").json() if s["id"] == sid)
    assert row["status"] == "待路由"

    # 重开口门后裁决落入
    client.post(f"/口门/{chute_id}/开闭?状态=口门开")
    routed = client.post(f"/扫描/{sid}/路由")
    assert routed.status_code == 200 and routed.json()["status"] == "已路由"
    assert routed.json()["chute_id"] == chute_id

    # 错分回流 → 可再裁决（回流写口与重放同刷）
    div = client.post("/回流批次", json={"扫描编号": sid})
    assert div.status_code == 200 and div.json()["扫描"]["status"] == "错分回流"
    assert client.post(f"/扫描/{sid}/路由").status_code == 200

    # 封袋集包：开袋→入袋→封口
    bag = client.post("/封袋/开袋", json={"口门编号": chute_id}).json()
    assert client.post(
        f"/封袋/{bag['id']}/入袋", json={"扫描编号": sid}
    ).status_code == 200
    seal = client.post(f"/封袋/{bag['id']}/封口")
    assert seal.status_code == 200 and seal.json()["status"] == "已封袋"

    # 总览刷新与审计查询同刷：状态分布、审计流水都能查到该件全链动作
    ov = client.get("/总览").json()
    assert ov["脏行数"] == 0
    assert ov["分布"]["已封袋"] >= 1
    all_audit = client.get("/审计").json()
    # 扫描件自身的过机、裁决审计挂扫描号
    scan_actions = {a["action"] for a in all_audit if a["ref_id"] == sid}
    assert {"过机装载", "路由裁决"} <= scan_actions
    # 入袋/封口审计挂袋号、回流批次审计记「建回流批次」：全链动作在审计流水中均可核
    audit_actions = {a["action"] for a in all_audit}
    assert {"建回流批次", "入袋", "封口"} <= audit_actions
    # route_events 留痕一次错分回流 + 两次裁决落入（含回流后再路由），不允许半截
    with db() as conn:
        event_actions = [
            r["action"] for r in conn.execute(
                "SELECT action FROM route_events WHERE scan_id=? ORDER BY id", (sid,)
            ).fetchall()
        ]
    assert event_actions.count("路由落入") == 2
    assert "错分回流" in event_actions

    # 重放对账：全链后四侧仍一致，无悬空审计、无脏行
    rp = client.get("/班次/对账").json()["重放对账"]
    assert rp["一致"] is True and rp["撕裂件"] == []
    assert rp["重放"]["悬空审计数"] == 0


# ---------- 夹具 8：总览/桶/册页/裁决对脏行口径一致（历史脏数据也必须暴露并挡住） ----------
def test_脏行_总览暴露桶剔除裁决挡重放报警(client):
    rec = client.post("/扫描", json={"条码": "TEAR-DIRTY", "目的地码": ""}).json()
    with db() as conn:
        # 模拟历史脏数据：状态待路由却空目的地
        conn.execute(
            "UPDATE scans SET status='待路由', dest_code='' WHERE id=?", (rec["id"],)
        )

    ov = client.get("/总览").json()
    assert ov["脏行数"] >= 1 and any(d["id"] == rec["id"] for d in ov["脏行"])
    # 待路由水位不把脏行算进去（谓词重放）
    assert all(s["id"] != rec["id"] for s in client.get("/待路由桶").json()["明细"])
    # 裁决入口硬挡
    assert client.post(f"/扫描/{rec['id']}/路由").status_code == 400
    # 重放对账把该撕裂件点名
    rp = client.get("/班次/对账").json()["重放对账"]
    assert rp["一致"] is False
    assert any(t["scan_id"] == rec["id"] and "谓词" in t["撕裂"] for t in rp["撕裂件"])

    # 补码口按 ADR-001 仅对「待补目的地」开放；脏行已被拧成待路由，先由补码口硬挡
    blocked_sup = client.post(f"/扫描/{rec['id']}/补码", json={"目的地码": "510"})
    assert blocked_sup.status_code == 400
    # 恢复为待补目的地状态后，补码修复收口：四侧重新一致
    with db() as conn:
        conn.execute(
            "UPDATE scans SET status='待补目的地' WHERE id=?", (rec["id"],)
        )
    client.post(f"/扫描/{rec['id']}/补码", json={"目的地码": "510"})
    rp2 = client.get("/班次/对账").json()["重放对账"]
    assert rp2["一致"] is True
    assert client.get("/总览").json()["脏行数"] == 0


# ---------- 夹具 9：服务层直接重放 —— 成功灯四侧可重入校验 ----------
def test_重放服务_成功件四侧均可重放(client):
    rec = client.post("/扫描", json={"条码": "TEAR-REPLAY", "目的地码": "110"}).json()
    with db() as conn:
        shift_id = rec["shift_id"]
        rp = replay_shift(conn, shift_id)
    assert rp["一致"] is True
    r = rp["重放"]
    # 种子 + 本笔两件可裁决，桶读口径与谓词重放水位相等
    assert r["待路由水位_谓词重放"] == r["待路由水位_桶读口径"] == 2
    assert r["悬空审计数"] == 0
    assert r["过机装载审计件数"] == 2
