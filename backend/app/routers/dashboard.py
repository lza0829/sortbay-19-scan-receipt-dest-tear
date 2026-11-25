from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list, row_to_dict
from app.services.loading import (
    STATUS_NEED_DEST,
    can_enter_routing,
)

router = APIRouter(tags=["总览"])


@router.get("/总览")
def overview():
    with db() as conn:
        shift = conn.execute(
            "SELECT * FROM shifts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        all_scans = conn.execute("SELECT * FROM scans").fetchall()

        # 全部计数以写侧同一谓词在 Python侧重放，禁止读侧另写 SQL 口径
        counts = {
            "待路由": 0,
            "待补目的地": 0,
            "已路由": 0,
            "错分回流": 0,
            "已入袋": 0,
            "已封袋": 0,
        }
        dirty = []
        for r in all_scans:
            d = dict(r)
            st = d["status"]
            if st in counts:
                counts[st] += 1
            # 脏行自检：处于可裁决状态却过不了写侧谓词（无目的地却可裁决）
            if st in ("待路由", "错分回流") and not can_enter_routing(r)[0]:
                d["拒因"] = can_enter_routing(r)[1]
                dirty.append(d)

        # 待路由只数真正通过谓词的件；待补目的地从待路由口径里剥除
        counts["待路由"] = sum(
            1 for r in all_scans
            if r["status"] == "待路由" and can_enter_routing(r)[0]
        )
        chutes = rows_to_list(conn.execute(
            """
            SELECT c.*,
              (SELECT COUNT(*) FROM scans s WHERE s.chute_id=c.id AND s.status IN ('已路由','已入袋')) AS 在口件数
            FROM chutes c ORDER BY c.code
            """
        ).fetchall())
        return {
            "班次": row_to_dict(shift),
            "分布": counts,
            "口门": chutes,
            "脏行数": len(dirty),
            "脏行": dirty,
        }

