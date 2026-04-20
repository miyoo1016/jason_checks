"""KIS REST API client — market ranking queries."""

import json
from datetime import datetime, timedelta
from pathlib import Path
import httpx
import structlog

from jason_checks.config import get_urls, get_active_credentials, get_settings

logger = structlog.get_logger()

ACCESS_TOKEN_CACHE = Path("data/.access_token_cache.json")

# In-memory cache for top movers (2-second TTL to respect KIS rate limits)
_top_movers_cache = {"data": [], "fetched_at": None}
_CACHE_TTL_SECONDS = 2


async def get_access_token() -> str:
    """Get OAuth access token for REST API (24h cache, separate from WS approval key)."""
    # Check file cache
    if ACCESS_TOKEN_CACHE.exists():
        try:
            cache = json.loads(ACCESS_TOKEN_CACHE.read_text())
            expire_at = datetime.fromisoformat(cache.get("expire_at", ""))
            if expire_at > datetime.now():
                return cache["access_token"]
        except Exception:
            pass

    settings = get_settings()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)

    endpoint = f"{rest_url}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "appsecret": app_secret,
    }

    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.post(endpoint, json=body, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()

    token = data.get("access_token")
    if not token:
        raise ValueError(f"No access_token in response: {data}")

    # Cache to file
    ACCESS_TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    cache_data = {
        "access_token": token,
        "token_type": data.get("token_type", "Bearer"),
        "issued_at": datetime.now().isoformat(),
        "expire_at": (datetime.now() + timedelta(hours=23)).isoformat(),
    }
    ACCESS_TOKEN_CACHE.write_text(json.dumps(cache_data, indent=2))
    logger.info("access_token_issued")
    return token


async def fetch_top_movers(
    market: str = "J",     # J=전체, P=코스피, Q=코스닥
    sort: str = "0",       # 0=상승률, 1=하락률
    limit: int = 30,
) -> list[dict]:
    """Fetch top N stocks by change rate from KIS REST API.

    Uses: 국내주식 등락률 순위 (FHPST01700000)

    Returns:
        List of dicts with keys: code, name, price, change_pct, volume, trading_value, strength
    """
    settings = get_settings()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    token = await get_access_token()

    url = f"{rest_url}/uapi/domestic-stock/v1/ranking/fluctuation"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "FHPST01700000",
        "custtype": "P",
    }
    params = {
        "fid_cond_mrkt_div_code": market,
        "fid_cond_scr_div_code": "20170",
        "fid_input_iscd": "0000",
        "fid_rank_sort_cls_code": sort,
        "fid_input_cnt_1": "0",
        "fid_prc_cls_code": "1",
        "fid_input_price_1": "",
        "fid_input_price_2": "",
        "fid_vol_cnt": "",
        "fid_trgt_cls_code": "0",
        "fid_trgt_exls_cls_code": "0",
        "fid_div_cls_code": "0",
        "fid_rsfl_rate1": "",
        "fid_rsfl_rate2": "",
    }

    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(url, headers=headers, params=params, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()

        output = data.get("output", [])
        results = []
        for item in output[:limit]:
            try:
                results.append({
                    "code": item.get("stck_shrn_iscd", ""),
                    "name": item.get("hts_kor_isnm", ""),
                    "price": int(item.get("stck_prpr", "0")),
                    "change_pct": float(item.get("prdy_ctrt", "0")),
                    "volume": int(item.get("acml_vol", "0")),
                    "trading_value": int(item.get("acml_tr_pbmn", "0")),
                    "cumulative_trading_value": int(item.get("acml_tr_pbmn", "0")),
                    "strength": float(item.get("tday_rltv", "0") or "0"),
                })
            except (ValueError, TypeError):
                continue

        logger.info("top_movers_fetched", count=len(results), market=market)
        return results

    except Exception as e:
        logger.error("top_movers_failed", error=str(e))
        return []


async def fetch_top_movers_cached(**kwargs) -> list[dict]:
    """Cached version of fetch_top_movers (2-second TTL to respect KIS rate limits)."""
    now = datetime.now()
    if (
        _top_movers_cache["fetched_at"]
        and (now - _top_movers_cache["fetched_at"]).total_seconds() < _CACHE_TTL_SECONDS
        and _top_movers_cache["data"]
    ):
        return _top_movers_cache["data"]

    data = await fetch_top_movers(**kwargs)
    _top_movers_cache["data"] = data
    _top_movers_cache["fetched_at"] = now
    return data


async def fetch_nxt_price(code: str) -> dict:
    """Fetch Nextrade (ATS) price for a single stock.
    
    Uses: 주식현재가 시세 (FHKST01010100) with market code NX.
    """
    settings = get_settings()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    token = await get_access_token()

    url = f"{rest_url}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "FHKST01010100",
        "custtype": "P",
    }
    params = {
        "fid_cond_mrkt_div_code": "NX",  # NX = Nextrade
        "fid_input_iscd": code,
    }

    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(url, headers=headers, params=params, timeout=5.0)
            if resp.status_code != 200:
                logger.warning("nxt_price_http_error", code=code, status=resp.status_code)
                return None
            data = resp.json()
            if data.get("rt_cd") != "0":
                logger.warning("nxt_price_api_error", code=code, msg=data.get("msg1", ""))
                return None
            out = data.get("output", {})
            return {
                "code": code,
                "price": int(out.get("stck_prpr", 0) or 0),
                "change_pct": float(out.get("prdy_ctrt", 0) or 0),
                "volume": int(out.get("acml_vol", 0) or 0),
                "trading_value": int(out.get("acml_tr_pbmn", 0) or 0),
                "is_nxt": True,
            }
    except Exception as e:
        logger.warning("nxt_price_exception", code=code, error=str(e))
    return None


def filter_non_theme_stocks(
    all_stocks: dict,
    active_leader_codes: set,
    code_theme_map: dict,
) -> list[dict]:
    """Fallback: filter subscribed stocks to exclude active theme leaders."""
    candidates = []
    for code, stock in all_stocks.items():
        if code in active_leader_codes:
            continue
        meta = code_theme_map.get(code, {})
        candidates.append({
            "code": code,
            "name": meta.get("name", code),
            "theme_code": meta.get("theme_code", ""),
            "theme_display": meta.get("theme_display", ""),
            "price": stock.price,
            "change_pct": stock.change_pct,
            "strength": stock.execution_strength,
            "cumulative_volume": stock.cum_volume_krw,
            "cumulative_trading_value": stock.cumulative_trading_value,
            "timestamp": stock.last_tick_ts.strftime("%H%M%S"),
        })
    return candidates

async def fetch_current_price(code: str) -> dict:
    """Fetch current price with NXT-aware logic.
    
    During NXT hours (08:00-08:50, 15:30-20:00), query NXT market first.
    """
    from datetime import datetime as _dt
    from jason_checks.config import get_urls, get_active_credentials, get_settings
    settings = get_settings()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    token = await get_access_token()

    now = _dt.now()
    is_nxt_hour = False
    if now.weekday() < 5:
        ct = now.hour * 100 + now.minute
        is_nxt_hour = (800 <= ct < 850) or (1530 <= ct < 2000)

    # Try NX market first during NXT hours, then J
    market_codes = ["NX", "J"] if is_nxt_hour else ["J"]

    url = f"{rest_url}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "FHKST01010100",
        "custtype": "P",
    }

    for mrkt in market_codes:
        params = {"fid_cond_mrkt_div_code": mrkt, "fid_input_iscd": code}
        try:
            async with httpx.AsyncClient(verify=False) as client:
                resp = await client.get(url, headers=headers, params=params, timeout=5.0)
                if resp.status_code != 200:
                    logger.warning("price_http_error", code=code, market=mrkt, status=resp.status_code)
                    continue
                data = resp.json()
                if data.get("rt_cd") != "0":
                    logger.warning("price_api_error", code=code, market=mrkt, msg=data.get("msg1", "")[:60])
                    continue
                out = data.get("output", {})
                price = int(out.get("stck_prpr", 0) or 0)
                if price > 0:
                    return {
                        "price": price,
                        "change_pct": float(out.get("prdy_ctrt", 0) or 0),
                        "volume": int(out.get("acml_vol", 0) or 0),
                        "trading_value": int(out.get("acml_tr_pbmn", 0) or 0),
                        "market": mrkt,
                    }
        except Exception as e:
            logger.warning("price_exception", code=code, market=mrkt, error=str(e))
    return None


# ---- 지수 / 수급 ----

# 지수 코드 정의
INDEX_CODES = {
    "0001": "KOSPI",
    "1001": "KOSDAQ",
}


async def fetch_market_indices() -> dict:
    """Fetch KOSPI + KOSDAQ index snapshot via inquire-index-price (FHPUP02100000).

    Returns:
        dict: { "0001": {price, change_pct, change_value, name}, "1001": {...} }
        Empty dict on failure.
    """
    settings = get_settings()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    token = await get_access_token()

    url = f"{rest_url}/uapi/domestic-stock/v1/quotations/inquire-index-price"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "FHPUP02100000",
        "custtype": "P",
    }

    results: dict = {}
    for code, name in INDEX_CODES.items():
        params = {
            "fid_cond_mrkt_div_code": "U",
            "fid_input_iscd": code,
        }
        try:
            async with httpx.AsyncClient(verify=False) as client:
                resp = await client.get(url, headers=headers, params=params, timeout=5.0)
                if resp.status_code != 200:
                    logger.warning("index_http_error", code=code, status=resp.status_code)
                    continue
                data = resp.json()
                if data.get("rt_cd") != "0":
                    logger.warning("index_api_error", code=code, msg=data.get("msg1", "")[:60])
                    continue
                out = data.get("output", {})
                # bstp_nmix_prpr = 업종 지수 현재가 (float string)
                price = float(out.get("bstp_nmix_prpr", 0) or 0)
                if price > 0:
                    results[code] = {
                        "name": name,
                        "price": price,
                        "change_pct": float(out.get("bstp_nmix_prdy_ctrt", 0) or 0),
                        "change_value": float(out.get("bstp_nmix_prdy_vrss", 0) or 0),
                    }
        except Exception as e:
            logger.warning("index_exception", code=code, error=str(e))

    return results


async def fetch_stock_investor_trend(code: str) -> dict:
    """Fetch per-stock investor trend (FHKST01010900).

    Returns aggregated daily net-buy amounts (원) for:
      foreigner / institution / individual
    Note: KIS returns per-tick or per-minute rows; we sum today's rows.
    """
    settings = get_settings()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    token = await get_access_token()

    url = f"{rest_url}/uapi/domestic-stock/v1/quotations/inquire-investor"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "FHKST01010900",
        "custtype": "P",
    }
    params = {
        "fid_cond_mrkt_div_code": "J",
        "fid_input_iscd": code,
    }

    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(url, headers=headers, params=params, timeout=5.0)
            if resp.status_code != 200:
                return {"foreigner": 0, "institution": 0, "individual": 0}
            data = resp.json()
            if data.get("rt_cd") != "0":
                return {"foreigner": 0, "institution": 0, "individual": 0}

        output = data.get("output", [])
        if not output:
            return {"foreigner": 0, "institution": 0, "individual": 0}

        # 가장 최근 행(오늘 또는 최신 일자)에서 순매수 거래대금 취득
        # KIS 필드: frgn_ntby_tr_pbmn (외인), orgn_ntby_tr_pbmn (기관), prsn_ntby_tr_pbmn (개인)
        latest = output[0] if isinstance(output, list) else output
        return {
            "foreigner": int(latest.get("frgn_ntby_tr_pbmn", 0) or 0),
            "institution": int(latest.get("orgn_ntby_tr_pbmn", 0) or 0),
            "individual": int(latest.get("prsn_ntby_tr_pbmn", 0) or 0),
        }
    except Exception as e:
        logger.warning("investor_trend_exception", code=code, error=str(e))
        return {"foreigner": 0, "institution": 0, "individual": 0}


async def fetch_index_investor_trend(index_code: str) -> dict:
    """Fetch KOSPI/KOSDAQ aggregate investor trend (FHPTJ04400000).

    index_code: "0001"=KOSPI, "1001"=KOSDAQ
    Returns: {foreigner, institution, individual} in 원 (net buy).
    """
    settings = get_settings()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    token = await get_access_token()

    url = f"{rest_url}/uapi/domestic-stock/v1/quotations/inquire-investor"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "FHPTJ04400000",
        "custtype": "P",
    }
    params = {
        "fid_cond_mrkt_div_code": "U",
        "fid_cond_scr_div_code": "20440",
        "fid_input_iscd": index_code,
        "fid_div_cls_code": "0",
    }

    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(url, headers=headers, params=params, timeout=5.0)
            if resp.status_code != 200:
                return {"foreigner": 0, "institution": 0, "individual": 0}
            data = resp.json()
            if data.get("rt_cd") != "0":
                logger.warning("index_inv_api_error", code=index_code, msg=data.get("msg1", "")[:60])
                return {"foreigner": 0, "institution": 0, "individual": 0}

        output = data.get("output", [])
        if not output:
            return {"foreigner": 0, "institution": 0, "individual": 0}
        latest = output[0] if isinstance(output, list) else output
        return {
            "foreigner": int(latest.get("frgn_ntby_tr_pbmn", 0) or 0),
            "institution": int(latest.get("orgn_ntby_tr_pbmn", 0) or 0),
            "individual": int(latest.get("prsn_ntby_tr_pbmn", 0) or 0),
        }
    except Exception as e:
        logger.warning("index_investor_exception", code=index_code, error=str(e))
        return {"foreigner": 0, "institution": 0, "individual": 0}
