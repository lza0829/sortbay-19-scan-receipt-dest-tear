"""
重放对账（架构一：目的地谓词 ∪ 册页正式行 ∪ 待路由水位 ∪ 过机审计同命运重放）。

全局唯一收口口径（见 ADR-002）：过机成功灯必须能由以下四侧重放成立，
任一侧不成立即判该笔撕裂并整笔回滚/暴露，禁止只改回执文案：
1. 目的地谓词 can_enter_routing：册页正式行的目的地事实；
2. 扫描册正式行 scans：回执引用的行真实存在、状态与目的地自洽；
3. 待路由水位：待路由桶/总览「待路由」只数通过谓词的件，读侧重放同一谓词；
4. 过机审计：每件的过机装载/补码等写动作在 audit_events 同命运可查，
   回滚事务的审计必须随册页行一起消失，不允许只剩半截成功审计。

本模块只做只读重放，不另立一套业务判断；所有可否裁决的结论都重放
app.services.loading.can_enter_routing，禁止在此复制谓词逻辑。
"""
from __future__ import annotations

from app.services.loading import (
    REASON_MISSING_DEST,
    ROUTABLE_STATUSES,
    STATUS_NEED_DEST,
    can_enter_routing,
)


def replay_shift(conn, shift_id: int | None = None) -> dict:
    """
    对一个班次做四侧重放对账。返回每侧重放值、是否一致与逐条撕裂明细。
    全程只读：撕裂只暴露、不改账，修账走补码口或整笔回滚写口。
    """
    if shift_id is None:
        shift = conn.execute(
            "SELECT * FROM shifts ORDER BY id DESC LIMIT 1"
        ).fetchone()
    else:
        shift = conn.execute("SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
    if not shift:
        return {"一致": False, "撕裂件": [], "问题": "班次不存在"}

    sid = shift["id"]
    scans = conn.execute(
        "SELECT * FROM scans WHERE shift_id=? ORDER BY id", (sid,)
    ).fetchall()

    # 侧 1+2：逐行重放目的地谓词，检查册页行状态与目的地是否自洽
    torn: list[dict] = []
    replay_routable = 0          # 重放口径下的待路由水位
    replay_need_dest = 0         # 重放口径下的待补目的地水位
    status_buckets: dict[str, int] = {}
    for r in scans:
        d = dict(r)
        st = d["status"]
        status_buckets[st] = status_buckets.get(st, 0) + 1
        ok, reason = can_enter_routing(r)

        if st in ROUTABLE_STATUSES:
            if ok:
                replay_routable += 1
            else:
                # 可裁决状态却过不了目的地谓词：无目的地却可裁决的脏行（成功灯撕裂）
                torn.append({
                    "scan_id": d["id"], "barcode": d["barcode"], "status": st,
                    "dest_code": d["dest_code"], "拒因": reason or REASON_MISSING_DEST,
                    "撕裂": "可裁决状态过不了目的地谓词",
                })
        if st == STATUS_NEED_DEST:
            replay_need_dest += 1
            if ok:
                # 挂待补码却其实有目的地：册页状态与目的地事实拧花
                torn.append({
                    "scan_id": d["id"], "barcode": d["barcode"], "status": st,
                    "dest_code": d["dest_code"], "拒因": "",
                    "撕裂": "待补目的地状态却有有效目的地码",
                })

    # 侧 3：待路由桶读口径重放——与逐行重放水位必须相等
    bucket_rows = conn.execute(
        "SELECT * FROM scans WHERE shift_id=? AND status IN (?, ?) ORDER BY id",
        (sid, *ROUTABLE_STATUSES),
    ).fetchall()
    bucket_watermark = sum(1 for r in bucket_rows if can_enter_routing(r)[0])

    # 侧 4：过机审计同命运——每件册页行都应有一条过机装载审计；
    # 反过来，过机装载审计引用的件必须在册页中存在（半截审计：行回滚了审计还在）。
    load_audits = conn.execute(
        "SELECT DISTINCT ref_id FROM audit_events "
        "WHERE kind='扫描' AND action='过机装载' AND ref_id IS NOT NULL"
    ).fetchall()
    audited_ids = {a["ref_id"] for a in load_audits}
    scan_ids = {r["id"] for r in scans}

    for r in scans:
        if r["id"] not in audited_ids:
            torn.append({
                "scan_id": r["id"], "barcode": r["barcode"], "status": r["status"],
                "dest_code": r["dest_code"], "拒因": "",
                "撕裂": "册页有正式行但缺过机装载审计（账与审计不同命运）",
            })
    dangling = sorted(audited_ids - scan_ids)
    for ref in dangling:
        torn.append({
            "scan_id": ref, "barcode": "", "status": "",
            "dest_code": "", "拒因": "",
            "撕裂": "过机装载审计悬空：册页行已回滚/不存在而审计成功行仍在",
        })

    watermark_consistent = bucket_watermark == replay_routable
    consistent = not torn and watermark_consistent

    return {
        "班次": dict(shift),
        "一致": consistent,
        "重放": {
            "册页正式行数": len(scans),
            "待路由水位_谓词重放": replay_routable,
            "待路由水位_桶读口径": bucket_watermark,
            "待补目的地水位": replay_need_dest,
            "状态分布": status_buckets,
            "过机装载审计件数": len(audited_ids & scan_ids),
            "悬空审计数": len(dangling),
            "水位一致": watermark_consistent,
        },
        "撕裂件": torn,
    }
