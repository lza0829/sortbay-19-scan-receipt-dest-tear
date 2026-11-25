"""
缺目的地过机装载闸 —— 全链路重放夹具（架构 B：告警仍装载、落待补码）。

钉住的面：过机装载写口、目的地码、扫描册、待路由桶、补码口、
裁决入口、总览待路由格、班次边界、审计流水，且读写共用同一谓词。
"""
from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi import HTTPException

from app.db import db
from app.services.loading import (
    REASON_MISSING_DEST,
    can_enter_routing,
    is_missing_dest,
    load_scan,
    supplement_dest,
)


# ---------- 夹具 1：缺码点装载——落扫描行但不进待路由桶，缺码事实可核 ----------
def test_缺目的地装载_落待补码且不进待路由桶(client):
    r = client.post("/扫描", json={"条码": "NO-DEST-001", "目的地码": ""})
    assert r.status_code == 200, r.text
    rec = r.json()
    # 回执不得假装成功可裁决
    assert rec["装载结果"] == "待补码"
    assert rec["可裁决"] is False
    assert rec["拒因"] == REASON_MISSING_DEST
    assert rec["status"] == "待补目的地"

    # 扫描册有行，缺码事实在册页可核
    scans = client.get("/扫描").json()
    row = next(s for s in scans if s["barcode"] == "NO-DEST-001")
    assert row["reject_reason"] == REASON_MISSING_DEST
    assert row["可裁决"] is False

    # 待补码视图可见
    need = client.get("/扫描/待补码").json()
    assert any(s["barcode"] == "NO-DEST-001" for s in need)

    # 待路由桶为空（至少不含该件）
    bucket = client.get("/待路由桶").json()
    assert all(s["barcode"] != "NO-DEST-001" for s in bucket["明细"])

    # 总览：待路由不涨、待补目的地 +1、无脏行
    ov = client.get("/总览").json()
    assert ov["分布"]["待补目的地"] >= 1
    assert ov["脏行数"] == 0

    # 审计有一条缺码装载，缺码事实在审计可核
    audit = client.get("/审计").json()
    a = next(x for x in audit if x["ref_id"] == rec["id"] and x["action"] == "过机装载")
    assert REASON_MISSING_DEST in a["note"]


# ---------- 夹具 2：补码前后裁决分叉 ----------
def test_补码前不可裁决_补码后可路由(client):
    rec = client.post("/扫描", json={"条码": "FORK-001", "目的地码": ""}).json()
    sid = rec["id"]

    # 补码前：裁决入口硬挡
    blocked = client.post(f"/扫描/{sid}/路由")
    assert blocked.status_code == 400
    assert REASON_MISSING_DEST in blocked.json()["detail"]

    # 非待补码件不能走补码口
    ok_rec = client.post("/扫描", json={"条码": "HAS-DEST-001", "目的地码": "110"}).json()
    bad_sup = client.post(f"/扫描/{ok_rec['id']}/补码", json={"目的地码": "210"})
    assert bad_sup.status_code == 400

    # 空补码：pydantic 入口 422；纯空白由服务层谓词 400 兜底
    empty_sup = client.post(f"/扫描/{sid}/补码", json={"目的地码": ""})
    assert empty_sup.status_code == 422
    blank_sup = client.post(f"/扫描/{sid}/补码", json={"目的地码": "   "})
    assert blank_sup.status_code == 400

    # 补码
    sup = client.post(f"/扫描/{sid}/补码", json={"目的地码": "110"})
    assert sup.status_code == 200, sup.text
    fixed = sup.json()
    assert fixed["status"] == "待路由"
    assert fixed["dest_code"] == "110"
    assert fixed["reject_reason"] == ""
    assert fixed["可裁决"] is True

    # 补码后进入待路由桶
    bucket = client.get("/待路由桶").json()
    assert any(s["id"] == sid for s in bucket["明细"])

    # 补码后裁决成功
    routed = client.post(f"/扫描/{sid}/路由")
    assert routed.status_code == 200, routed.text
    assert routed.json()["status"] == "已路由"

    # 审计同时留下装载缺码、补码、裁决三段
    audit = client.get("/审计").json()
    actions = [x["action"] for x in audit if x["ref_id"] == sid]
    assert "过机装载" in actions and "补目的地码" in actions and "路由裁决" in actions


# ---------- 夹具 3：同一条码并发双装，班次唯一只成一笔 ----------
def test_同条码双装_只成一笔(client):
    first = client.post("/扫描", json={"条码": "DUP-001", "目的地码": "110"})
    assert first.status_code == 200
    second = client.post("/扫描", json={"条码": "DUP-001", "目的地码": "210"})
    assert second.status_code == 400
    assert "重复" in second.json()["detail"]

    # 库里该条码本班次只有一行，目的地不被第二次覆盖
    rows = [s for s in client.get("/扫描").json() if s["barcode"] == "DUP-001"]
    assert len(rows) == 1
    assert rows[0]["dest_code"] == "110"


def test_同条码并发双装_数据库唯一索引兜底(client):
    results = []
    errors = []

    def fire():
        try:
            r = client.post("/扫描", json={"条码": "RACE-001", "目的地码": "110"})
            results.append(r.status_code)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=fire)
    t2 = threading.Thread(target=fire)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert not errors
    assert sorted(results) == [200, 400]
    with db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE barcode='RACE-001'"
        ).fetchone()["c"]
    assert n == 1


# ---------- 夹具 4：半截失败脏写——整笔回到装载前快照 ----------
def test_装载事务中失败_整体回滚无脏行(client):
    with db() as conn:
        before = conn.execute("SELECT COUNT(*) c FROM scans").fetchone()["c"]
        # 先成功落一笔缺码（该事务正常提交）
        rec = load_scan(conn, barcode="HALF-001", dest_code="", line_id=None, note="")
        assert rec["status"] == "待补目的地"

    # 半截回滚：在同一事务里先装载再制造失败，整笔回到装载前快照
    with pytest.raises(RuntimeError):
        with db() as conn:
            load_scan(conn, barcode="HALF-ROLL-001", dest_code="110", line_id=None, note="")
            load_scan(conn, barcode="HALF-ROLL-002", dest_code="", line_id=None, note="")
            raise RuntimeError("boom-halfway")

    with db() as conn:
        after = conn.execute("SELECT COUNT(*) c FROM scans").fetchone()["c"]
        # 只多出第一笔成功事务的 HALF-001；回滚事务的两件都不在
        assert after == before + 1
        assert conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE barcode IN ('HALF-ROLL-001','HALF-ROLL-002')"
        ).fetchone()["c"] == 0
        # 回滚事务不留无目的地脏行
        dirty = conn.execute(
            "SELECT * FROM scans WHERE status IN ('待路由','错分回流') AND TRIM(dest_code)=''"
        ).fetchall()
        assert dirty == []
        # 回滚事务的审计也被撤销
        audit_n = conn.execute(
            "SELECT COUNT(*) c FROM audit_events WHERE note LIKE '%HALF-ROLL%'"
        ).fetchone()["c"]
        assert audit_n == 0


# ---------- 夹具 5：总览与扫描册拧花——读路径重放写侧同一谓词 ----------
def test_读写谓词一致_缺码件绝不可裁决(client):
    rec = client.post("/扫描", json={"条码": "PRED-001", "目的地码": ""}).json()
    with db() as conn:
        row = conn.execute("SELECT * FROM scans WHERE id=?", (rec["id"],)).fetchone()
        ok, reason = can_enter_routing(row)
        assert ok is False and reason == REASON_MISSING_DEST
        assert is_missing_dest(row["dest_code"]) is True

    # 册页、桶、总览、裁决入口四处口径必须一致地认为不可裁决
    listed = next(s for s in client.get("/扫描").json() if s["id"] == rec["id"])
    bucketed = [s for s in client.get("/待路由桶").json()["明细"] if s["id"] == rec["id"]]
    ov = client.get("/总览").json()
    route_resp = client.post(f"/扫描/{rec['id']}/路由")
    assert listed["可裁决"] is False
    assert bucketed == []
    assert ov["脏行数"] == 0
    assert route_resp.status_code == 400


def test_手工制造脏行_总览自检暴露且裁决仍挡住(client):
    rec = client.post("/扫描", json={"条码": "DIRTY-001", "目的地码": ""}).json()
    # 人为拧花：把待补码件改成待路由却仍空目的地（模拟历史脏数据）
    with db() as conn:
        conn.execute(
            "UPDATE scans SET status='待路由', dest_code='' WHERE id=?", (rec["id"],)
        )
    ov = client.get("/总览").json()
    assert ov["脏行数"] >= 1
    assert any(d["id"] == rec["id"] for d in ov["脏行"])
    # 待路由桶不收脏行，裁决入口仍以谓词硬挡
    bucket = client.get("/待路由桶").json()
    assert all(s["id"] != rec["id"] for s in bucket["明细"])
    assert client.post(f"/扫描/{rec['id']}/路由").status_code == 400


# ---------- 夹具 6：班次边界——封班拒写，待补码件挡住封班 ----------
def test_封班后禁止装载_待补码件挡封班(client):
    client.post("/扫描", json={"条码": "SHIFT-001", "目的地码": ""})
    shifts = client.get("/班次").json()
    sid = shifts[0]["id"]
    # 有待补码件，封班被拒
    sealed = client.post(f"/班次/{sid}/封班")
    assert sealed.status_code == 400
    # 补码并路由清空后可封班（路由到 110→A01，再入袋封口流程外，这里仅验证待补码口径）
    rec = next(s for s in client.get("/扫描/待补码").json() if s["barcode"] == "SHIFT-001")
    client.post(f"/扫描/{rec['id']}/补码", json={"目的地码": "110"})
    # 仍有未路由件，封班依旧拒，但拒因不再是待补码
    again = client.post(f"/班次/{sid}/封班")
    assert again.status_code == 400
    recon = client.get("/班次/对账").json()
    assert recon["待补码件数"] == 0


# ---------- 服务层：补码口仅对待补码状态开放 ----------
def test_补码服务_状态守卫(client):
    with db() as conn:
        rec = load_scan(conn, barcode="SVC-001", dest_code="110", line_id=None, note="")
        with pytest.raises(HTTPException) as ei:
            supplement_dest(conn, scan_id=rec["id"], dest_code="210")
        assert ei.value.status_code == 400
