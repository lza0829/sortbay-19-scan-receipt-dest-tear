from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list

router = APIRouter(tags=["审计"])


@router.get("/审计")
def list_audit():
    with db() as conn:
        return rows_to_list(conn.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT 200"
        ).fetchall())

