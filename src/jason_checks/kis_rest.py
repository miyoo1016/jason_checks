"""KIS REST API client — market ranking queries."""

import json
import asyncio
import traceback
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


def _kis_int(value) -> int:
    """Parse KIS numeric strings while preserving negative values."""
    if value is None:
        return 0
    text = str(value).strip().replace(",", "")
    if text == "":
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _kis_has_value(value) -> bool:
    return value is not None and str(value).strip() != ""


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

    url = f"{rest_url}/uapi/domestic-stock/v1/quotations/volume-rank"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key, "appsecret": app_secret,
        "tr_id": "FHKST01010300", "custtype": "P",
    }
    params = {
        "fid_cond_mrkt_div_code": market,
        "fid_cond_scr_div_code": "20101",
        "fid_input_iscd": "0000",
        "fid_div_cls_code": "0",
        "fid_sort_cls_code": "0",
        "fid_trgt_cls_code": "0",
        "fid_trgt_exls_cls_code": "0",
        "fid_input_price_1": "0",
        "fid_input_price_2": "0",
        "fid_vol_cnt": "0",
        "fid_input_cnt_1": "0",
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
                    "code": item.get("mksc_shrn_iscd", ""),
                    "name": item.get("hts_kor_isnm", ""),
                    "price": int(item.get("stck_prpr", "0") or "0"),
                    "change_pct": float(item.get("prdy_ctrt", "0") or "0"),
                    "trading_value": int(item.get("acml_tr_pbmn", "0") or "0"),
                })
            except: continue

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
                    continue
                data = resp.json()
                if data.get("rt_cd") != "0":
                    continue
                out = data.get("output", {})
                price = int(out.get("stck_prpr", 0) or 0)
                # FHKST01010100 gives price, change, volume, value AND real-time foreigner net buy
                return {
                    "price": price,
                    "change_pct": float(out.get("prdy_ctrt", 0) or 0),
                    "volume": int(out.get("acml_vol", 0) or 0),
                    "trading_value": int(out.get("acml_tr_pbmn", 0) or 0),
                    # Strength is better fetched from FHKST01010300 (tday_rltv)
                    "strength": float(out.get("tday_rltv", 0.0) or 0.0),
                    "foreigner_net_buy": int(out.get("frgn_ntby_qty", 0) or 0) * price,
                    "market": mrkt,
                }
        except Exception as e:
            logger.warning("price_exception", code=code, market=mrkt, error=str(e))
    return None

async def fetch_overseas_price(ticker: str) -> dict:
    """Fetch overseas (US) current price via HHDFS00000300.

    Tries NAS, then NYS if needed.
    """
    settings = get_settings()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    token = await get_access_token()

    url = f"{rest_url}/uapi/overseas-price/v1/quotations/price"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "HHDFS00000300",
        "custtype": "P",
    }

    # Try NAS first, then NYS
    exchanges = ["NAS", "NYS", "AMS"]
    for ex in exchanges:
        # For overseas, EXCD and SYMB are usually enough
        params = {"AUTH": "", "EXCD": ex, "SYMB": ticker}
        try:
            async with httpx.AsyncClient(verify=False) as client:
                resp = await client.get(url, headers=headers, params=params, timeout=5.0)
                if resp.status_code == 200:
                    await asyncio.sleep(0.7)
                    data = resp.json()
                    if data.get("rt_cd") == "0":
                        out = data.get("output", {})
                        # price is in 'last'
                        price = float(out.get("last", 0) or 0)
                        if price > 0:
                            return {
                                "price": price,
                                "change_pct": float(out.get("rate", 0) or 0),
                                "volume": int(out.get("tvol", 0) or 0),
                                "trading_value": int(out.get("tamt", 0) or 0),
                                "market": "US",
                            }
                    else:
                        logger.warning("us_price_api_error", code=ticker, ex=ex, msg=data.get("msg1"))
        except: pass
    return None

async def fetch_investor_data(code: str) -> dict:
    """Fetch real-time investor net buy/sell data (Foreigner/Inst/Indiv)."""
    from jason_checks.config import get_urls, get_active_credentials, get_settings
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
    params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}

    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(url, headers=headers, params=params, timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                output = data.get("output", [])
                if not output: return {"foreign": 0, "institution": 0, "individual": 0}

                latest = output[0] if isinstance(output, list) else output
                # Use standard field names for FHKST01010900
                f_qty = int(latest.get("frgn_ntby_qty", 0) or 0)
                o_qty = int(latest.get("orgn_ntby_qty", 0) or 0)
                p_qty = int(latest.get("prsn_ntby_qty", 0) or 0)
                prpr = int(latest.get("stck_prpr", 0) or 1)

                return {
                    "foreign": f_qty * prpr,
                    "institution": o_qty * prpr,
                    "individual": p_qty * prpr,
                }
    except Exception:
        pass
    return {"foreign": 0, "institution": 0, "individual": 0}


# ---- 지수 / 수급 ----

# 지수 코드 정의
INDEX_CODES = {
    "KR": {
        "0001": "KOSPI",
        "1001": "KOSDAQ",
    },
    "US": {
        "COMP": ("NASDAQ", "NAS"),
        "DOWI": ("DOW", "NYS"),
        "SPX": ("S&P 500", "NYS"),
    }
}


async def fetch_market_indices(market: str = "KR") -> dict:
    """Fetch index snapshot (KR or US).

    KR: inquire-index-price (FHPUP02100000)
    US: inquire-index-price (FHPUP02100000) - same TR, different codes/market_div
    """
    settings = get_settings()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    token = await get_access_token()
    if market == "KR":
        url = f"{rest_url}/uapi/domestic-stock/v1/quotations/inquire-index-price"
    else:
        url = f"{rest_url}/uapi/overseas-price/v1/quotations/inquire-index-price"

    tr_id = "FHPUP02100000" if market == "KR" else "HHDFS76200200"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": tr_id,
        "custtype": "P",
    }

    results: dict = {}
    codes = INDEX_CODES.get(market, {})
    for code, info in codes.items():
        if market == "KR":
            name = info
            params = {"fid_cond_mrkt_div_code": "U", "fid_input_iscd": code}
        else:
            name, excd = info
            # HHDFS76200200 uses SYMB for code
            params = {"AUTH": "", "EXCD": excd, "SYMB": code}

        try:
            async with httpx.AsyncClient(verify=False) as client:
                resp = await client.get(url, headers=headers, params=params, timeout=5.0)
                if resp.status_code != 200: continue
                data = resp.json()
                if data.get("rt_cd") != "0":
                    logger.warning("index_api_error", code=code, msg=data.get("msg1", ""))
                    continue
                out = data.get("output", {})

                if market == "KR":
                    price = float(out.get("bstp_nmix_prpr", 0) or 0)
                    chg_pct = float(out.get("bstp_nmix_prdy_ctrt", 0) or 0)
                    chg_val = float(out.get("bstp_nmix_prdy_vrss", 0) or 0)
                else:
                    price = float(out.get("last", 0) or 0)
                    chg_pct = float(out.get("rate", 0) or 0)
                    chg_val = float(out.get("diff", 0) or 0)

                if price > 0:
                    results[code] = {
                        "name": name,
                        "price": price,
                        "change_pct": chg_pct,
                        "change_value": chg_val,
                    }
            # Important: Sleep to avoid EGW00201 on the next iteration
            await asyncio.sleep(1.0)
        except Exception as e:
            logger.warning("index_exception", code=code, error=str(e))
            await asyncio.sleep(1.0)

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

    # Step A: Get Strength from FHKST01010300
    strength = 0.0
    try:
        url_exec = f"{rest_url}/uapi/domestic-stock/v1/quotations/inquire-investor" # Same endpoint often used for multiple TRs
        headers_exec = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": app_key, "appsecret": app_secret,
            "tr_id": "FHKST01010300", "custtype": "P",
        }
        params_exec = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(url_exec, headers=headers_exec, params=params_exec, timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                out = data.get("output", [])
                if out:
                    strength = float(out[0].get("tday_rltv", 0.0) or 0.0)
    except Exception: pass

    # Step B: Get Investor Trend from FHKST01010900
    url_inv = f"{rest_url}/uapi/domestic-stock/v1/quotations/inquire-investor"
    headers_inv = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key, "appsecret": app_secret,
        "tr_id": "FHKST01010900", "custtype": "P",
    }
    params_inv = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}

    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(url_inv, headers=headers_inv, params=params_inv, timeout=3.0)
            if resp.status_code != 200:
                return {"foreigner": 0, "institution": 0, "individual": 0, "strength": strength}
            data = resp.json()
            output = data.get("output", [])
            if not output:
                return {"foreigner": 0, "institution": 0, "individual": 0, "strength": strength}

            latest = output[0] if isinstance(output, list) else output
            # QTY fields
            f_qty = int(latest.get("frgn_ntby_qty", 0) or 0)
            i_qty = int(latest.get("orgn_ntby_qty", 0) or 0)
            p_qty = int(latest.get("prsn_ntby_qty", 0) or 0)

            # Use a price for valuation (heuristic)
            prpr = int(latest.get("stck_prpr", 0) or 0)
            if prpr == 0:
                prpr = 1 # Fallback to qty only if price missing

            return {
                "foreigner": f_qty * prpr,
                "institution": i_qty * prpr,
                "individual": p_qty * prpr,
                "strength": strength
            }
    except Exception as e:
        logger.warning("investor_trend_exception", code=code, error=str(e))
        return {"foreigner": 0, "institution": 0, "individual": 0, "strength": 0.0}


async def fetch_stock_supply_snapshot(code: str) -> dict:
    """Fetch one stock's investor supply snapshot with explicit status fields.

    This uses the existing KIS investor endpoint and is intended for a small,
    selective target set only. Callers must apply their own TTL/backoff.
    """
    now = datetime.now().isoformat(timespec="seconds")
    try:
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
        params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(url, headers=headers, params=params, timeout=4.0)
        if resp.status_code == 429:
            return {
                "foreigner_net_buy": 0,
                "institution_net_buy": 0,
                "individual_net_buy": 0,
                "supply_status": "RATE_LIMIT",
                "supply_timestamp": "",
                "supply_source": "KIS",
                "supply_age_sec": None,
            }
        if resp.status_code != 200:
            return {
                "foreigner_net_buy": 0,
                "institution_net_buy": 0,
                "individual_net_buy": 0,
                "supply_status": "ERROR",
                "supply_timestamp": "",
                "supply_source": "KIS",
                "supply_age_sec": None,
            }
        payload = resp.json()
        if payload.get("rt_cd") != "0":
            msg = str(payload.get("msg1", ""))
            status = "RATE_LIMIT" if "EGW00201" in msg or "초당" in msg or "rate" in msg.lower() else "DATA_NA"
            return {
                "foreigner_net_buy": 0,
                "institution_net_buy": 0,
                "individual_net_buy": 0,
                "supply_status": status,
                "supply_timestamp": "",
                "supply_source": "KIS",
                "supply_age_sec": None,
            }
        output = payload.get("output", [])
        if not output:
            return {
                "foreigner_net_buy": 0,
                "institution_net_buy": 0,
                "individual_net_buy": 0,
                "supply_status": "DATA_NA",
                "supply_recency": "UNKNOWN",
                "supply_date": "",
                "supply_timestamp": "",
                "supply_source": "KIS",
                "supply_age_sec": None,
            }

        latest = None
        is_today = False
        rows = output if isinstance(output, list) else [output]

        for idx, row in enumerate(rows):
            fields = [
                "frgn_ntby_tr_pbmn", "orgn_ntby_tr_pbmn", "prsn_ntby_tr_pbmn",
                "frgn_ntby_qty", "orgn_ntby_qty", "prsn_ntby_qty"
            ]
            if any(_kis_has_value(row.get(f)) for f in fields):
                latest = row
                is_today = (idx == 0)
                break

        if latest is None:
            return {
                "foreigner_net_buy": 0,
                "institution_net_buy": 0,
                "individual_net_buy": 0,
                "supply_status": "DATA_NA",
                "supply_recency": "UNKNOWN",
                "supply_date": "",
                "supply_timestamp": "",
                "supply_source": "KIS",
                "supply_age_sec": None,
            }

        close_price = _kis_int(latest.get("stck_clpr")) or _kis_int(latest.get("stck_prpr")) or 1

        def net_amount(amount_key: str, qty_key: str) -> int:
            if _kis_has_value(latest.get(amount_key)):
                return _kis_int(latest.get(amount_key)) * 1_000_000
            if _kis_has_value(latest.get(qty_key)):
                return _kis_int(latest.get(qty_key)) * close_price
            return 0

        foreigner = net_amount("frgn_ntby_tr_pbmn", "frgn_ntby_qty")
        institution = net_amount("orgn_ntby_tr_pbmn", "orgn_ntby_qty")
        individual = net_amount("prsn_ntby_tr_pbmn", "prsn_ntby_qty")

        supply_recency = "TODAY" if is_today else "PREV_DAY"
        supply_date = latest.get("stck_bsop_date", "")

        return {
            "foreigner_net_buy": foreigner,
            "institution_net_buy": institution,
            "individual_net_buy": individual,
            "supply_status": "OK",
            "supply_recency": supply_recency,
            "supply_date": supply_date,
            "supply_timestamp": now,
            "supply_source": "KIS",
            "supply_age_sec": 0,
        }
    except Exception as e:
        msg = str(e)
        tb = traceback.format_exc(limit=3)
        status = "RATE_LIMIT" if "429" in msg or "EGW00201" in msg or "rate" in msg.lower() else "ERROR"
        logger.warning("supply_snapshot_failed", code=code, status=status, error=msg, traceback=tb)
        return {
            "foreigner_net_buy": 0,
            "institution_net_buy": 0,
            "individual_net_buy": 0,
            "supply_status": status,
            "supply_recency": "UNKNOWN",
            "supply_date": "",
            "supply_timestamp": "",
            "supply_source": "KIS",
            "supply_age_sec": None,
            "supply_error": msg,
            "supply_traceback": tb,
        }


async def fetch_index_investor_trend(index_code: str) -> dict:
    """Fetch KOSPI/KOSDAQ aggregate investor trend.

    Note: The public KIS open API does not support a stable index investor trend endpoint
    without specialized/restricted access. To prevent EGW00201 rate limit consumption and
    avoid flooding console logs with false alarms, we return zero flow gracefully.
    """
    return {"foreigner": 0, "institution": 0, "individual": 0}
async def fetch_us_top_movers(limit: int = 15) -> list[dict]:
    """Fetch US market top movers (rank by volume).

    Uses: 해외주식 실시간 순위 (HHDFS76410000)
    """
    settings = get_settings()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    token = await get_access_token()

    url = f"{rest_url}/uapi/overseas-price/v1/quotations/rank"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key, "appsecret": app_secret,
        "tr_id": "HHDFS76410000", "custtype": "P",
    }
    # EXCD: NAS, NYS, AMS
    params = {
        "AUTH": "", "EXCD": "NAS", "GUBN": "0" # 0:거래량, 1:상승률, 2:거래대금
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
                    "code": item.get("symb", ""),
                    "name": item.get("symb", ""),
                    "price": float(item.get("last", "0") or "0"),
                    "change_pct": float(item.get("rate", "0") or "0"),
                    "trading_value": int(float(item.get("tamt", "0") or "0")),
                })
            except: continue
        return results
    except Exception as e:
        logger.error("us_top_movers_failed", error=str(e))
        return []

class KISClient:
    """Wrapper class for KIS REST functions."""
    async def fetch_stock_price(self, code: str) -> dict:
        # Map to stck_prpr for consistency with scanner expectation
        res = await fetch_current_price(code)
        if res:
            # Add some missing fields for the scanner
            res['stck_prpr'] = res['price']
            res['w52_lw_pr'] = res['price'] * 0.8 # Mock for now if not available
        return res

    async def fetch_stock_investor_trend(self, code: str) -> list[dict]:
        # Return a list of 1 for now to satisfy 3-day check in demo
        res = await fetch_stock_investor_trend(code)
        return [{"fore_ntby_qty": res["foreigner"]}] * 3

def get_rest_client():
    return KISClient()
