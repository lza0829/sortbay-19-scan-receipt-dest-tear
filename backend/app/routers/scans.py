from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import db, rows_to_list, row_to_dict
from app.services.common import audit, current_shift, now_str, require_unsealed
from app.services.loading import (
    REASON_MISSING_DEST,
    ROUTABLE_STATUSES,
    STATUS_NEED_DEST,
    can_enter_routing,
    load_scan,
    supplement_dest,
)

router = APIRouter(tags=["过机扫描"])


class ScanIn(BaseModel):
    条码: str = Field(min_length=1)
    目的地码: str = ""
    分拣线编号: int | None = None
    备注: str = ""


class SupplementIn(BaseModel):
    目的地码: str = Field(min_length=1)


@router.get("/扫描")
def list_scans(状态: str | None = None):
    with db() as conn:
        if 状态:
            rows = conn.execute(
                "SELECT * FROM scans WHERE status=? ORDER BY id DESC LIMIT 200", (状态,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 200").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["可裁决"], d["拒因"] = can_enter_routing(r)  # 读路径重放写侧同一谓词
            out.append(d)
        return out


@router.get("/扫描/待补码")
def list_need_dest():
    """待补码视图：缺目的地挂账件，缺码事实在此可核，不与待路由桶混算。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM scans WHERE status=? ORDER BY id DESC LIMIT 200",
            (STATUS_NEED_DEST,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["可裁决"], d["拒因"] = can_enter_routing(r)
            out.append(d)
        return out


@router.get("/待路由桶")
def routing_bucket():
    """
    待路由桶：仅纳入通过 can_enter_routing 的件。
    待补目的地件一律不进桶，从根上杜绝「无目的地却可裁决」。
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM scans WHERE status IN (?, ?) ORDER BY id DESC LIMIT 200",
            ROUTABLE_STATUSES,
        ).fetchall()
        items = []
        for r in rows:
            ok, reason = can_enter_routing(r)
            if not ok:
                # 谓词与状态不一致即视为脏行，绝不放进可裁决桶
                continue
            d = dict(r)
            d["可裁决"] = True
            d["拒因"] = ""
            items.append(d)
        return {"件数": len(items), "明细": items}


@router.post("/扫描")
def create_scan(body: ScanIn):
    # 单连接立即事务：装载行 + 审计同生共死，失败整体回到装载前快照
    with db() as conn:
        require_unsealed(conn)
        return load_scan(
            conn,
            barcode=body.条码,
            dest_code=body.目的地码,
            line_id=body.分拣线编号,
            note=body.备注,
        )


@router.post("/扫描/{scan_id}/补码")
def supplement_scan(scan_id: int, body: SupplementIn):
    """臂内补码口：把待补目的地件补齐后翻为可裁决，缺码拒因随补码清除并留审计。"""
    with db() as conn:
        require_unsealed(conn)
        return supplement_dest(conn, scan_id=scan_id, dest_code=body.目的地码)


@router.post("/扫描/{scan_id}/路由")
def route_scan(scan_id: int):
    with db() as conn:
        require_unsealed(conn)
        scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        if not scan:
            raise HTTPException(404, "扫描不存在")
        # 裁决第一道闸：重放写侧同一谓词，缺目的地件在此被明确硬挡，不可能落入口门
        routable, reason = can_enter_routing(scan)
        if not routable:
            raise HTTPException(400, reason or REASON_MISSING_DEST)
        if scan["status"] not in ROUTABLE_STATUSES:
            raise HTTPException(400, f"当前状态不可路由: {scan['status']}")
        rule = conn.execute(
            "SELECT * FROM route_rules WHERE dest_code=?", (scan["dest_code"],)
        ).fetchone()
        if not rule:
            raise HTTPException(400, "无匹配路由")
        chute = conn.execute("SELECT * FROM chutes WHERE id=?", (rule["chute_id"],)).fetchone()
        if not chute:
            raise HTTPException(400, "口门不存在")
        if chute["status"] != "口门开":
            raise HTTPException(400, "口门已闭，禁止落入")
        conn.execute(
            "UPDATE scans SET status='已路由', chute_id=?, reject_reason='' WHERE id=?",
            (chute["id"], scan_id),
        )
        conn.execute(
            "INSERT INTO route_events(scan_id, chute_id, action, created_at, note) VALUES (?, ?, ?, ?, ?)",
            (scan_id, chute["id"], "路由落入", now_str(), chute["code"]),
        )
        audit(conn, "扫描", scan_id, "路由裁决", chute["code"])
        return row_to_dict(conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone())

