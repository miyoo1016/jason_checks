"""Today intraday sparkline helpers for dashboard mini charts."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo


SEOUL = ZoneInfo("Asia/Seoul")


def normalize_symbol(code: object) -> str:
    value = str(code or "").strip().upper()
    if value.startswith("A") and value[1:].isdigit():
        value = value[1:]
    if value.isdigit() and len(value) < 6:
        value = value.zfill(6)
    return value


def session_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    base = now.astimezone(SEOUL) if now and now.tzinfo else (now or datetime.now(SEOUL)).replace(tzinfo=SEOUL)
    start = datetime.combine(base.date(), time(9, 0), tzinfo=SEOUL)
    end = datetime.combine(base.date(), time(15, 30), tzinfo=SEOUL)
    if base < end:
        end = base
    if end < start:
        end = start
    return start, end


def parse_kis_intraday_row(row: dict) -> dict | None:
    date_s = str(row.get("stck_bsop_date") or row.get("date") or "").strip()
    time_s = str(row.get("stck_cntg_hour") or row.get("time") or row.get("t") or "").strip()
    price_raw = row.get("stck_prpr") or row.get("price") or row.get("close") or row.get("p")
    if not date_s or not time_s:
        return None
    try:
        price = float(str(price_raw).replace(",", ""))
        if price <= 0:
            return None
        time_s = time_s.zfill(6)[:6]
        dt = datetime.strptime(date_s + time_s, "%Y%m%d%H%M%S").replace(tzinfo=SEOUL)
        return {"time": dt, "price": price}
    except Exception:
        return None


def parse_naver_intraday_row(row: dict) -> dict | None:
    dt_s = str(row.get("localDateTime") or "").strip()
    price_raw = row.get("currentPrice") or row.get("closePrice") or row.get("price")
    if not dt_s or len(dt_s) < 14:
        return None
    try:
        price = float(str(price_raw).replace(",", ""))
        if price <= 0:
            return None
        dt = datetime.strptime(dt_s[:14], "%Y%m%d%H%M%S").replace(tzinfo=SEOUL)
        return {"time": dt, "price": price}
    except Exception:
        return None


async def fetch_naver_intraday_prices(code: str) -> list[dict]:
    """Fetch today's 1-minute intraday rows from Naver mobile chart API."""
    import httpx

    symbol = normalize_symbol(code)
    if not symbol.isdigit():
        return []
    url = f"https://api.stock.naver.com/chart/domestic/item/{symbol}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": f"https://m.stock.naver.com/domestic/stock/{symbol}",
    }
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=True, headers=headers) as client:
        resp = await client.get(url, params={"periodType": "day"})
    if resp.status_code != 200:
        return []
    payload = resp.json()
    rows = []
    for item in payload.get("priceInfos") or []:
        parsed = parse_naver_intraday_row(item)
        if not parsed:
            continue
        rows.append(parsed)
    return rows


def _bucket_ms(dt: datetime, interval_minutes: int) -> int:
    local = dt.astimezone(SEOUL)
    minute = (local.minute // interval_minutes) * interval_minutes
    bucket = local.replace(minute=minute, second=0, microsecond=0)
    return int(bucket.timestamp() * 1000)


def downsample_evenly(points: list[dict], max_points: int = 80) -> list[dict]:
    if max_points <= 0 or len(points) <= max_points:
        return points
    if max_points == 1:
        return [points[-1]]
    last_idx = len(points) - 1
    selected = []
    seen = set()
    for i in range(max_points):
        idx = round(i * last_idx / (max_points - 1))
        if idx not in seen:
            selected.append(points[idx])
            seen.add(idx)
    return selected


def build_today_sparkline(
    raw_points: list[dict],
    *,
    prev_close: float | None = None,
    interval_minutes: int = 5,
    max_points: int = 80,
    source: str = "kis_intraday",
    now: datetime | None = None,
) -> dict:
    start, end = session_bounds(now)
    clean = []
    for row in raw_points or []:
        parsed = parse_kis_intraday_row(row) if isinstance(row, dict) and "stck_cntg_hour" in row else row
        if not parsed:
            continue
        dt = parsed.get("time")
        price = parsed.get("price")
        if not isinstance(dt, datetime):
            continue
        try:
            price_f = float(price)
        except Exception:
            continue
        if start <= dt.astimezone(SEOUL) <= end and price_f > 0:
            clean.append({"time": dt.astimezone(SEOUL), "price": price_f})

    clean.sort(key=lambda p: p["time"])
    if len(clean) < 3:
        return {
            "status": "DATA_NA",
            "points": [],
            "baseline": 0,
            "source": source,
            "sparkline_source": source,
            "sparkline_range": "today",
            "sparkline_tf": f"{interval_minutes}m",
            "sparkline_is_fallback": False,
            "sparkline_point_count": 0,
        }

    buckets: dict[int, dict] = {}
    for point in clean:
        buckets[_bucket_ms(point["time"], interval_minutes)] = point

    aggregated = [
        {"t": bucket, "p": point["price"], "time": point["time"].isoformat(), "price": point["price"]}
        for bucket, point in sorted(buckets.items())
    ]
    aggregated = downsample_evenly(aggregated, max_points=max_points)
    baseline = float(aggregated[0]["p"])
    prev = float(prev_close) if prev_close and prev_close > 0 else None
    enriched = []
    for point in aggregated:
        price = float(point["p"])
        enriched.append({
            **point,
            "pct_from_open": ((price - baseline) / baseline * 100.0) if baseline else None,
            "pct_from_prev_close": ((price - prev) / prev * 100.0) if prev else None,
            "source": source,
            "is_fallback": False,
        })
    last = float(enriched[-1]["p"])
    return {
        "status": "OK" if len(enriched) >= 3 else "collecting",
        "points": enriched,
        "baseline": baseline,
        "source": source,
        "interval": f"{interval_minutes}m",
        "trend_label": "Today 5m" if interval_minutes == 5 else f"Today {interval_minutes}m",
        "point_count": len(enriched),
        "change_from_baseline_pct": ((last - baseline) / baseline * 100.0) if baseline else 0.0,
        "sparkline_points": enriched,
        "sparkline_tf": f"{interval_minutes}m",
        "sparkline_range": "today",
        "sparkline_start": enriched[0]["time"],
        "sparkline_end": enriched[-1]["time"],
        "sparkline_source": source,
        "sparkline_is_fallback": False,
        "sparkline_point_count": len(enriched),
        "sparkline_change_pct_from_open": ((last - baseline) / baseline * 100.0) if baseline else 0.0,
    }


def build_fallback_sparkline(points: dict[int, float] | list[dict], *, price: float = 0, change_pct: float = 0) -> dict | None:
    if isinstance(points, dict):
        ordered = [{"t": int(t), "p": float(p)} for t, p in sorted(points.items()) if float(p or 0) > 0]
    else:
        ordered = [{"t": int(p.get("t")), "p": float(p.get("p"))} for p in (points or []) if float(p.get("p") or 0) > 0]
    if len(ordered) >= 3:
        first = ordered[0]["p"]
        last = ordered[-1]["p"]
        return {
            "status": "OK",
            "points": ordered[-80:],
            "baseline": first,
            "source": "recent_buffer",
            "interval": "recent",
            "trend_label": "recent",
            "point_count": len(ordered[-80:]),
            "change_from_baseline_pct": ((last - first) / first * 100.0) if first else 0.0,
            "sparkline_range": "recent",
            "sparkline_tf": "recent",
            "sparkline_source": "recent_buffer",
            "sparkline_is_fallback": True,
            "sparkline_point_count": len(ordered[-80:]),
        }
    try:
        p = float(price)
        c = float(change_pct)
    except Exception:
        return None
    if p <= 0:
        return None
    baseline = p / (1 + c / 100.0) if c > -99.9 else p
    now_ms = int(datetime.now(SEOUL).timestamp() * 1000)
    return {
        "status": "collecting",
        "points": [{"t": now_ms - 60000, "p": baseline}, {"t": now_ms, "p": p}],
        "baseline": baseline,
        "source": "fallback_quote",
        "interval": "recent",
        "trend_label": "fallback",
        "point_count": 2,
        "change_from_baseline_pct": ((p - baseline) / baseline * 100.0) if baseline else 0.0,
        "sparkline_range": "recent",
        "sparkline_tf": "fallback",
        "sparkline_source": "fallback_quote",
        "sparkline_is_fallback": True,
        "sparkline_point_count": 2,
    }
