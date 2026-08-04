import csv
import io
from datetime import date as date_type, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from app.api.admin.session import require_session, get_session
from app.api.admin._templates import templates as _templates
from app.config import settings
from app.db.timeseries import (
    query_report_day,
    query_report_month,
    query_report_month_by_client,
    query_top_voicemail_transcripts,
    query_quality,
)
from app.db.clients import get_all_clients_with_stats
from app.db.sites import list_sites

router = APIRouter()


@router.get("/reports", response_class=HTMLResponse)
async def reports_page(
    request:   Request,
    mode:      str          = Query("day"),   # day | month
    date:      Optional[str] = Query(None),
    month:     Optional[str] = Query(None),
    client_id: Optional[str] = Query(None),   # "" cuando el selector es "Todos los clientes" — no un int siempre
    site_id:   Optional[str] = Query(None),   # filtro opcional por sede, se suma al filtro por cliente
    _=Depends(require_session),
):
    client_id = int(client_id) if client_id else None
    site_id   = int(site_id) if site_id else None
    today      = date_type.today()
    today_str  = today.isoformat()
    month_str  = month or today.strftime("%Y-%m")
    day_str    = date  or today_str
    clients    = await get_all_clients_with_stats()
    sites      = await list_sites()

    hourly_rows    = []
    by_client_rows = []
    top_voicemail  = []
    if mode == "month":
        rows = await query_report_month(month_str, client_id, site_id)
        total_row = _sum_rows_month(rows)
        # Solo tiene sentido el detalle por día+cliente cuando se ven todos los
        # clientes juntos — si ya filtraste a uno, query_report_month ya da
        # exactamente lo mismo por día.
        if not client_id:
            by_client_rows = await query_report_month_by_client(month_str, client_id, site_id)
        top_voicemail = await query_top_voicemail_transcripts(month_str, client_id, limit=10)
        prev_month = _shift_month(month_str, -1)
        prev_total_row = _sum_rows_month(await query_report_month(prev_month, client_id, site_id))
    else:
        rows = await query_report_day(day_str, client_id, site_id)
        total_row = _sum_rows_day(rows)
        hourly_rows = (await query_quality(day_str, client_id, site_id))["rows"]
        prev_day = (date_type.fromisoformat(day_str) - timedelta(days=1)).isoformat()
        prev_total_row = _sum_rows_day(await query_report_day(prev_day, client_id, site_id))

    delta_pct = _delta_pct(total_row.get("total"), prev_total_row.get("total"))

    return _templates.TemplateResponse(request, "reports.html", {
        "request":         request,
        "admin_prefix":    settings.ADMIN_PREFIX,
        "active_page":     "reports",
        "clients":         clients,
        "sites":           sites,
        "mode":            mode,
        "selected_day":    day_str,
        "selected_month":  month_str,
        "selected_client": client_id,
        "selected_site":   site_id,
        "today":           today_str,
        "rows":            rows,
        "total_row":       total_row,
        "prev_total_row":  prev_total_row,
        "delta_pct":       delta_pct,
        "hourly_rows":     hourly_rows,
        "by_client_rows":  by_client_rows,
        "top_voicemail":   top_voicemail,
    })


def _shift_month(month_str: str, delta_months: int) -> str:
    y, m = (int(x) for x in month_str.split("-"))
    total = y * 12 + (m - 1) + delta_months
    return f"{total // 12}-{total % 12 + 1:02d}"


def _delta_pct(current: Optional[int], previous: Optional[int]) -> Optional[float]:
    """None cuando el período anterior no tiene datos — no tiene sentido
    mostrar un delta contra cero (sería siempre +100% o indefinido)."""
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


@router.get("/reports/export.csv")
async def reports_export_csv(
    mode:      str          = Query("day"),
    date:      Optional[str] = Query(None),
    month:     Optional[str] = Query(None),
    client_id: Optional[str] = Query(None),
    site_id:   Optional[str] = Query(None),
    _=Depends(require_session),
):
    client_id_int = int(client_id) if client_id else None
    site_id_int   = int(site_id) if site_id else None
    today = date_type.today()

    if mode == "month":
        month_str = month or today.strftime("%Y-%m")
        rows = await query_report_month(month_str, client_id_int, site_id_int)
        fieldnames = ["day", "total", "human", "voicemail", "unknown",
                      "human_pct", "voicemail_pct", "unknown_pct", "avg_latency"]
        filename = f"voxidet_reporte_{month_str}.csv"
    else:
        day_str = date or today.isoformat()
        rows = await query_report_day(day_str, client_id_int, site_id_int)
        fieldnames = ["client_name", "total", "human", "voicemail", "unknown",
                      "human_pct", "voicemail_pct", "unknown_pct",
                      "avg_latency", "avg_audio", "slow_calls", "slow_pct"]
        filename = f"voxidet_reporte_{day_str}.csv"

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _sum_rows_day(rows: list[dict]) -> dict:
    if not rows:
        return {}
    total = sum(r["total"] for r in rows)
    human = sum(r["human"] for r in rows)
    vm    = sum(r["voicemail"] for r in rows)
    unk   = sum(r["unknown"] for r in rows)
    slow  = sum(r["slow_calls"] for r in rows)
    lats  = [r["avg_latency"] for r in rows if r["avg_latency"]]

    def _pct(n, d): return round(n / d * 100, 1) if d else 0.0
    return {
        "total":         total,
        "human":         human,
        "voicemail":     vm,
        "unknown":       unk,
        "human_pct":     _pct(human, total),
        "voicemail_pct": _pct(vm,    total),
        "unknown_pct":   _pct(unk,   total),
        "avg_latency":   round(sum(lats) / len(lats)) if lats else 0,
        "slow_calls":    slow,
        "slow_pct":      _pct(slow, total),
    }


def _sum_rows_month(rows: list[dict]) -> dict:
    if not rows:
        return {}
    total = sum(r["total"] for r in rows)
    human = sum(r["human"] for r in rows)
    vm    = sum(r["voicemail"] for r in rows)
    unk   = sum(r["unknown"] for r in rows)
    lats  = [r["avg_latency"] for r in rows if r["avg_latency"]]

    def _pct(n, d): return round(n / d * 100, 1) if d else 0.0
    return {
        "total":         total,
        "human":         human,
        "voicemail":     vm,
        "unknown":       unk,
        "human_pct":     _pct(human, total),
        "voicemail_pct": _pct(vm,    total),
        "unknown_pct":   _pct(unk,   total),
        "avg_latency":   round(sum(lats) / len(lats)) if lats else 0,
    }


@router.get("/reports/data")
async def reports_data(
    request:   Request,
    mode:      str           = Query("day"),
    date:      Optional[str] = Query(None),
    month:     Optional[str] = Query(None),
    client_id: Optional[str] = Query(None),   # "" cuando el selector es "Todos los clientes" — no un int siempre
    site_id:   Optional[str] = Query(None),
):
    from fastapi.responses import JSONResponse
    if not get_session(request):
        return JSONResponse(status_code=403, content={"detail": "No autorizado"})
    client_id = int(client_id) if client_id else None
    site_id   = int(site_id) if site_id else None
    today = date_type.today()
    if mode == "month":
        m = month or today.strftime("%Y-%m")
        return await query_report_month(m, client_id, site_id)
    d = date or today.isoformat()
    return await query_report_day(d, client_id, site_id)
