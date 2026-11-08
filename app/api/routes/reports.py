"""Report export endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.dependencies import current_user, get_session
from app.database import repositories as repo
from app.database.models import User
from app.reporting.export import to_json, to_markdown
from app.utils.hashing import hash_obj

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/{report_id}/export")
def export_report(report_id: str, format: Literal["json", "markdown"] = "json", _: User = Depends(current_user),
                  session: Session = Depends(get_session)) -> Response:
    report = repo.get_report_by_id(session, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Report not found."})
    data = report.report_data
    recomputed = hash_obj({k: v for k, v in data.items() if k != "integrity"})
    headers = {"X-ProofLoop-Report-Hash": report.report_hash, "X-ProofLoop-Integrity": "ok" if recomputed == report.report_hash else "mismatch"}
    if format == "markdown":
        headers["Content-Disposition"] = f'attachment; filename="{report_id}.md"'
        return Response(to_markdown(data), media_type="text/markdown; charset=utf-8", headers=headers)
    headers["Content-Disposition"] = f'attachment; filename="{report_id}.json"'
    return Response(to_json(data), media_type="application/json", headers=headers)
