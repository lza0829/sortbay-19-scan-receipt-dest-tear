"""
过机装载领域服务（全局唯一口径，架构 B：告警仍装载、落待补码）。

口径（ADR-001）：
- 缺目的地码的过机 *允许* 落账，但只落「待补目的地」状态：
  写扫描行、不进待路由桶、不可裁决，缺码事实进 reject_reason 与审计，永不静默抹掉。
- 补码后同一行翻为「待路由」，此时才可被路由裁决。
- 读路径「能否进待路由 / 能否裁决」与写入侧共用 can_enter_routing 这一个谓词，
  禁止读侧另写一套判断。
- 整笔装载在单连接 BEGIN IMMEDIATE 事务内完成；任一步失败整体回滚到装载前快照。
"""
from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from app.services.common import audit, current_shift, now_str

# ---- 状态口径（全局唯一，禁止各处另造字面量分叉） ----
STATUS_PENDING = "待路由"            # 有目的地码，可进待路由桶、可裁决
STATUS_NEED_DEST = "待补目的地"      # 缺目的地码挂账，不可裁决、不进待路由桶
STATUS_ROUTED = "已路由"
STATUS_IN_BAG = "已入袋"
STATUS_SEALED = "已封袋"
STATUS_DIVERT = "错分回流"

# 可被路由裁决的状态
ROUTABLE_STATUSES = (STATUS_PENDING, STATUS_DIVERT)

# ---- 拒因口径（落 scans.reject_reason 与回执，可核不可抹） ----
REASON_MISSING_DEST = "缺目的地码"
REASON_DUP = "班次内条码重复"
REASON_NOT_NEED_DEST = "非待补目的地件，无需补码"
REASON_DEST_EMPTY = "补码目的地码不得为空"


def is_missing_dest(dest_code: str | None) -> bool:
    """缺目的地事实谓词：空串/纯空白即缺码。读写两侧共用。"""
    return not (dest_code or "").strip()


def can_enter_routing(scan_row: sqlite3.Row | dict) -> tuple[bool, str]:
    """
    读路径谓词：该行能否进待路由桶 / 能否被裁决。
    写入侧装载与补码用同一函数判定状态翻转，读侧总览与桶直接重放它。
    返回 (可否裁决, 拒因)。
    """
    dest = scan_row["dest_code"] if isinstance(scan_row, dict) else scan_row["dest_code"]
    if is_missing_dest(dest):
        return False, REASON_MISSING_DEST
    return True, ""


def _get_line(conn, line_id: int | None):
    if line_id:
        line = conn.execute("SELECT * FROM lines WHERE id=?", (line_id,)).fetchone()
        if not line:
            raise HTTPException(400, "分拣线不存在")
    else:
        line = conn.execute("SELECT * FROM lines WHERE active=1 ORDER BY id LIMIT 1").fetchone()
        if not line:
            raise HTTPException(400, "无可用分拣线")
    if not line["active"]:
        raise HTTPException(400, "分拣线已停用")
    return line


def load_scan(conn, *, barcode: str, dest_code: str, line_id: int | None, note: str) -> dict:
    """
    过机装载写口。调用方须已在 db() 事务（BEGIN IMMEDIATE）内。
    整笔成功才提交；重复/缺码等结果均在同一事务内落账或回滚。
    """
    shift = current_shift(conn)
    line = _get_line(conn, line_id)
    barcode = barcode.strip()

    # 班次唯一：先查后插处于写事务内，唯一索引再兜底，并发双装只会一成一拒
    dup = conn.execute(
        "SELECT id FROM scans WHERE barcode=? AND shift_id=?",
        (barcode, shift["id"]),
    ).fetchone()
    if dup:
        raise HTTPException(400, f"{REASON_DUP}：{barcode}")

    missing = is_missing_dest(dest_code)
    status = STATUS_NEED_DEST if missing else STATUS_PENDING
    reject_reason = REASON_MISSING_DEST if missing else ""

    try:
        cur = conn.execute(
            """
            INSERT INTO scans(barcode, dest_code, shift_id, line_id, status,
                              scanned_at, note, reject_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (barcode, dest_code.strip(), shift["id"], line["id"], status,
             now_str(), note, reject_reason),
        )
    except sqlite3.IntegrityError:
        # 并发下唯一索引兜底：整笔失败，不留半截
        raise HTTPException(400, f"{REASON_DUP}：{barcode}")

    scan_id = cur.lastrowid
    audit_note = barcode if not missing else f"{barcode} | {REASON_MISSING_DEST}，挂待补码"
    audit(conn, "扫描", scan_id, "过机装载", audit_note)

    row = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
    routable, reason = can_enter_routing(row)
    receipt = dict(row)
    receipt["装载结果"] = "待补码" if missing else "成功"
    receipt["可裁决"] = routable
    receipt["拒因"] = reason
    return receipt


def supplement_dest(conn, *, scan_id: int, dest_code: str) -> dict:
    """
    补码写口：仅「待补目的地」件可补；补码后由同一谓词重放翻为可裁决。
    """
    current_shift(conn)  # 仍须处于未封班班次
    if is_missing_dest(dest_code):
        raise HTTPException(400, REASON_DEST_EMPTY)
    scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
    if not scan:
        raise HTTPException(404, "扫描不存在")
    if scan["status"] != STATUS_NEED_DEST:
        raise HTTPException(400, REASON_NOT_NEED_DEST)

    new_dest = dest_code.strip()
    conn.execute(
        "UPDATE scans SET dest_code=?, status=?, reject_reason='' WHERE id=?",
        (new_dest, STATUS_PENDING, scan_id),
    )
    audit(conn, "扫描", scan_id, "补目的地码", f"{scan['barcode']} -> {new_dest}")

    row = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
    routable, reason = can_enter_routing(row)
    out = dict(row)
    out["可裁决"] = routable
    out["拒因"] = reason
    return out

