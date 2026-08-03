from datetime import datetime, timedelta, date as date_type
from typing import Optional
from sqlalchemy import text
from app.db.engine import get_db

RESULT_COLORS = {
    "HUMAN":     "#22c55e",
    "VOICEMAIL": "#f59e0b",
    "UNKNOWN":   "#6366f1",
    "ERROR":     "#ef4444",
}
CLIENT_COLORS = ["#6366f1","#22c55e","#f59e0b","#ec4899","#14b8a6","#f97316","#8b5cf6","#06b6d4","#ef4444","#a3e635"]


def _minute_labels(hours: int) -> list[str]:
    now   = datetime.now().replace(second=0, microsecond=0)
    start = now - timedelta(hours=hours)
    labels, cur = [], start
    while cur <= now:
        labels.append(cur.strftime("%H:%M"))
        cur += timedelta(minutes=1)
    return labels


def _hour_labels() -> list[str]:
    return [f"{h:02d}:00" for h in range(24)]


def _build_series_by_key(rows, label_key, group_key, count_key, labels, color_map=None) -> list[dict]:
    groups: dict = {}
    for r in rows:
        name  = str(r[group_key] or "Sin nombre")
        label = r[label_key]
        count = int(r[count_key] or 0)
        groups.setdefault(name, {})[label] = count

    series = []
    for i, (name, data) in enumerate(groups.items()):
        color = (color_map or {}).get(name) or CLIENT_COLORS[i % len(CLIENT_COLORS)]
        series.append({
            "name":  name,
            "color": color,
            "data":  [data.get(lb, 0) for lb in labels],
        })
    return series


async def query_timeseries_live(hours: int, client_id: Optional[int] = None) -> dict:
    """Detecciones por minuto (últimas N horas) desde voxidet_logs."""
    cond = "AND l.client_id = :cid" if client_id else ""
    sql = f"""
        SELECT
            ANY_VALUE(DATE_FORMAT(l.created_at, '%H:%i')) AS lbl,
            c.name                              AS client_name,
            l.result,
            COUNT(*)                            AS cnt
        FROM voxidet_logs l
        JOIN clients c ON c.id = l.client_id
        WHERE l.created_at >= NOW() - INTERVAL :hours HOUR {cond}
        GROUP BY DATE_FORMAT(l.created_at, '%Y-%m-%d %H:%i'), l.client_id, l.result
        ORDER BY MIN(l.created_at) ASC
    """
    params: dict = {"hours": hours}
    if client_id:
        params["cid"] = client_id

    async with get_db() as db:
        r = await db.execute(text(sql), params)
        rows = r.mappings().all()

    labels = _minute_labels(hours)
    return {
        "labels":     labels,
        "by_client":  _build_series_by_key(rows, "lbl", "client_name", "cnt", labels),
        "by_result":  _build_series_by_key(rows, "lbl", "result",      "cnt", labels, RESULT_COLORS),
    }


async def query_timeseries_day(day: date_type, client_id: Optional[int] = None) -> dict:
    """Detecciones por hora para un día completo desde voxidet_logs."""
    cond = "AND l.client_id = :cid" if client_id else ""
    sql = f"""
        SELECT
            ANY_VALUE(DATE_FORMAT(l.created_at, '%H:00')) AS lbl,
            c.name                              AS client_name,
            l.result,
            COUNT(*)                            AS cnt
        FROM voxidet_logs l
        JOIN clients c ON c.id = l.client_id
        WHERE l.created_at >= :day AND l.created_at < :day + INTERVAL 1 DAY {cond}
        GROUP BY HOUR(l.created_at), l.client_id, l.result
        ORDER BY HOUR(l.created_at) ASC
    """
    params: dict = {"day": day.isoformat()}
    if client_id:
        params["cid"] = client_id

    async with get_db() as db:
        r = await db.execute(text(sql), params)
        rows = r.mappings().all()

    labels = _hour_labels()
    return {
        "labels":    labels,
        "by_client": _build_series_by_key(rows, "lbl", "client_name", "cnt", labels),
        "by_result": _build_series_by_key(rows, "lbl", "result",      "cnt", labels, RESULT_COLORS),
    }


async def query_quality(day: str, client_id: Optional[int] = None) -> dict:
    """Calidad de detección por hora: totales, %HUMAN/VOICEMAIL/UNKNOWN, latencia promedio."""
    cond = "AND l.client_id = :cid" if client_id else ""
    sql = f"""
        SELECT
            HOUR(l.created_at)                          AS h,
            c.name                                      AS client_name,
            COUNT(*)                                    AS total,
            SUM(l.result = 'HUMAN')                     AS human,
            SUM(l.result = 'VOICEMAIL')                 AS voicemail,
            SUM(l.result = 'UNKNOWN')                   AS unknown_c,
            ROUND(AVG(l.latency_ms), 0)                 AS avg_latency,
            ROUND(AVG(l.audio_secs), 2)                 AS avg_audio,
            SUM(l.latency_ms > 3000)                    AS slow_calls
        FROM voxidet_logs l
        JOIN clients c ON c.id = l.client_id
        WHERE l.created_at >= :day AND l.created_at < :day + INTERVAL 1 DAY {cond}
        GROUP BY HOUR(l.created_at), l.client_id
        ORDER BY h DESC
    """
    params: dict = {"day": day}
    if client_id:
        params["cid"] = client_id

    def _pct(num, den):
        return round(num / den * 100, 1) if den else 0.0

    async with get_db() as db:
        r = await db.execute(text(sql), params)
        rows_raw = r.mappings().all()

    rows = []
    totals: dict = {}
    for row in rows_raw:
        tot = int(row["total"] or 0)
        hum = int(row["human"] or 0)
        vm  = int(row["voicemail"] or 0)
        unk = int(row["unknown_c"] or 0)
        lat = int(row["avg_latency"] or 0)
        aud = float(row["avg_audio"] or 0)
        slw = int(row["slow_calls"] or 0)
        cname = row["client_name"] or ""

        rows.append({
            "hour":         f"{int(row['h']):02d}:00",
            "client_name":  cname,
            "total":        tot,
            "human":        hum,
            "voicemail":    vm,
            "unknown":      unk,
            "human_pct":    _pct(hum, tot),
            "voicemail_pct":_pct(vm,  tot),
            "unknown_pct":  _pct(unk, tot),
            "avg_latency":  lat,
            "avg_audio":    aud,
            "slow_calls":   slw,
            "slow_pct":     _pct(slw, tot),
        })

        k = cname
        if k not in totals:
            totals[k] = {"client_name": cname, "total": 0, "human": 0,
                         "voicemail": 0, "unknown": 0, "avg_latency": [], "slow_calls": 0}
        totals[k]["total"]       += tot
        totals[k]["human"]       += hum
        totals[k]["voicemail"]   += vm
        totals[k]["unknown"]     += unk
        totals[k]["slow_calls"]  += slw
        if lat:
            totals[k]["avg_latency"].append(lat)

    total_list = []
    for t in totals.values():
        tot = t["total"]
        lats = t["avg_latency"]
        total_list.append({
            **t,
            "avg_latency":   round(sum(lats) / len(lats)) if lats else 0,
            "human_pct":     _pct(t["human"],     tot),
            "voicemail_pct": _pct(t["voicemail"],  tot),
            "unknown_pct":   _pct(t["unknown"],    tot),
            "slow_pct":      _pct(t["slow_calls"], tot),
        })
    total_list.sort(key=lambda x: -x["total"])
    return {"rows": rows, "totals": total_list}


async def query_report_day(day: str, client_id: Optional[int] = None) -> list[dict]:
    """Resumen del día por cliente: total, HUMAN/VM/UNK, avg latencia, audio."""
    cond = "AND l.client_id = :cid" if client_id else ""
    sql = f"""
        SELECT
            c.name                          AS client_name,
            COUNT(*)                        AS total,
            SUM(l.result = 'HUMAN')         AS human,
            SUM(l.result = 'VOICEMAIL')     AS voicemail,
            SUM(l.result = 'UNKNOWN')       AS unknown_c,
            ROUND(AVG(l.latency_ms), 0)     AS avg_latency,
            ROUND(AVG(l.audio_secs), 2)     AS avg_audio,
            SUM(l.latency_ms > 3000)        AS slow_calls
        FROM voxidet_logs l
        JOIN clients c ON c.id = l.client_id
        WHERE l.created_at >= :day AND l.created_at < :day + INTERVAL 1 DAY {cond}
        GROUP BY l.client_id
        ORDER BY total DESC
    """
    params: dict = {"day": day}
    if client_id:
        params["cid"] = client_id

    def _pct(num, den):
        return round(num / den * 100, 1) if den else 0.0

    async with get_db() as db:
        r = await db.execute(text(sql), params)
        rows = r.mappings().all()

    out = []
    for row in rows:
        tot = int(row["total"] or 0)
        hum = int(row["human"] or 0)
        vm  = int(row["voicemail"] or 0)
        unk = int(row["unknown_c"] or 0)
        slw = int(row["slow_calls"] or 0)
        out.append({
            "client_name":   row["client_name"],
            "total":         tot,
            "human":         hum,
            "voicemail":     vm,
            "unknown":       unk,
            "human_pct":     _pct(hum, tot),
            "voicemail_pct": _pct(vm,  tot),
            "unknown_pct":   _pct(unk, tot),
            "avg_latency":   int(row["avg_latency"] or 0),
            "avg_audio":     float(row["avg_audio"]  or 0),
            "slow_calls":    slw,
            "slow_pct":      _pct(slw, tot),
        })
    return out


async def query_report_month(month: str, client_id: Optional[int] = None) -> list[dict]:
    """Resumen del mes agrupado por día."""
    cond = "AND l.client_id = :cid" if client_id else ""
    sql = f"""
        SELECT
            DATE(l.created_at)              AS day,
            COUNT(*)                        AS total,
            SUM(l.result = 'HUMAN')         AS human,
            SUM(l.result = 'VOICEMAIL')     AS voicemail,
            SUM(l.result = 'UNKNOWN')       AS unknown_c,
            ROUND(AVG(l.latency_ms), 0)     AS avg_latency
        FROM voxidet_logs l
        WHERE l.created_at >= STR_TO_DATE(CONCAT(:month, '-01'), '%Y-%m-%d')
          AND l.created_at <  STR_TO_DATE(CONCAT(:month, '-01'), '%Y-%m-%d') + INTERVAL 1 MONTH
          {cond}
        GROUP BY DATE(l.created_at)
        ORDER BY day DESC
    """
    params: dict = {"month": month}
    if client_id:
        params["cid"] = client_id

    def _pct(num, den):
        return round(num / den * 100, 1) if den else 0.0

    async with get_db() as db:
        r = await db.execute(text(sql), params)
        rows = r.mappings().all()

    out = []
    for row in rows:
        tot = int(row["total"] or 0)
        hum = int(row["human"] or 0)
        vm  = int(row["voicemail"] or 0)
        unk = int(row["unknown_c"] or 0)
        d   = row["day"]
        out.append({
            "day":           d.isoformat() if hasattr(d, "isoformat") else str(d),
            "total":         tot,
            "human":         hum,
            "voicemail":     vm,
            "unknown":       unk,
            "human_pct":     _pct(hum, tot),
            "voicemail_pct": _pct(vm,  tot),
            "unknown_pct":   _pct(unk, tot),
            "avg_latency":   int(row["avg_latency"] or 0),
        })
    return out


async def query_report_month_by_client(month: str, client_id: Optional[int] = None) -> list[dict]:
    """Resumen del mes agrupado por día Y cliente (una fila por combinación) —
    a diferencia de query_report_month, que agrega todos los clientes juntos
    por día. Solo tiene sentido mostrarla cuando no hay un cliente puntual
    filtrado (si ya filtraste a un cliente, query_report_month da lo mismo)."""
    cond = "AND l.client_id = :cid" if client_id else ""
    sql = f"""
        SELECT
            DATE(l.created_at)              AS day,
            c.name                          AS client_name,
            COUNT(*)                        AS total,
            SUM(l.result = 'HUMAN')         AS human,
            SUM(l.result = 'VOICEMAIL')     AS voicemail,
            SUM(l.result = 'UNKNOWN')       AS unknown_c
        FROM voxidet_logs l
        JOIN clients c ON c.id = l.client_id
        WHERE l.created_at >= STR_TO_DATE(CONCAT(:month, '-01'), '%Y-%m-%d')
          AND l.created_at <  STR_TO_DATE(CONCAT(:month, '-01'), '%Y-%m-%d') + INTERVAL 1 MONTH
          {cond}
        GROUP BY DATE(l.created_at), l.client_id
        ORDER BY day DESC, total DESC
    """
    params: dict = {"month": month}
    if client_id:
        params["cid"] = client_id

    def _pct(num, den):
        return round(num / den * 100, 1) if den else 0.0

    async with get_db() as db:
        r = await db.execute(text(sql), params)
        rows = r.mappings().all()

    out = []
    for row in rows:
        tot = int(row["total"] or 0)
        hum = int(row["human"] or 0)
        vm  = int(row["voicemail"] or 0)
        unk = int(row["unknown_c"] or 0)
        d   = row["day"]
        out.append({
            "day":           d.isoformat() if hasattr(d, "isoformat") else str(d),
            "client_name":   row["client_name"],
            "total":         tot,
            "human":         hum,
            "voicemail":     vm,
            "unknown":       unk,
            "human_pct":     _pct(hum, tot),
            "voicemail_pct": _pct(vm,  tot),
            "unknown_pct":   _pct(unk, tot),
        })
    return out


async def query_top_voicemail_transcripts(month: str, client_id: Optional[int] = None, limit: int = 10) -> list[dict]:
    """Top N transcripciones más repetidas entre las llamadas marcadas VOICEMAIL
    en el mes — excluye los placeholders [silencio]/[sin audio] (capa 1 sin
    transcripción real, ver amd.py) porque dominarían el ranking sin aportar
    nada; solo interesan transcripciones reales que se repiten."""
    cond = "AND l.client_id = :cid" if client_id else ""
    sql = f"""
        SELECT
            TRIM(l.transcript) AS transcript,
            COUNT(*)           AS cnt
        FROM voxidet_logs l
        WHERE l.result = 'VOICEMAIL'
          AND l.transcript IS NOT NULL
          AND TRIM(l.transcript) NOT IN ('', '[silencio]', '[sin audio]')
          AND l.created_at >= STR_TO_DATE(CONCAT(:month, '-01'), '%Y-%m-%d')
          AND l.created_at <  STR_TO_DATE(CONCAT(:month, '-01'), '%Y-%m-%d') + INTERVAL 1 MONTH
          {cond}
        GROUP BY TRIM(l.transcript)
        ORDER BY cnt DESC
        LIMIT :limit
    """
    params: dict = {"month": month, "limit": limit}
    if client_id:
        params["cid"] = client_id

    async with get_db() as db:
        r = await db.execute(text(sql), params)
        rows = r.mappings().all()

    return [{"transcript": row["transcript"], "count": int(row["cnt"])} for row in rows]
