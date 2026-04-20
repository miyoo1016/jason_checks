import asyncio
import httpx
import json
from pathlib import Path
from datetime import datetime, timedelta

# Mocking parts of the app to test URLs directly
ACCESS_TOKEN_CACHE = Path("data/.access_token_cache.json")

async def get_access_token(app_key, app_secret, rest_url) -> str:
    # Try cache first
    if ACCESS_TOKEN_CACHE.exists():
        try:
            cache = json.loads(ACCESS_TOKEN_CACHE.read_text())
            if datetime.fromisoformat(cache["expire_at"]) > datetime.now():
                return cache["access_token"]
        except: pass

    endpoint = f"{rest_url}/oauth2/tokenP"
    body = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.post(endpoint, json=body)
        data = resp.json()
        return data["access_token"]

async def test_urls():
    # Load credentials from .env
    env_content = Path(".env").read_text()
    creds = {}
    for line in env_content.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip()
    
    app_key = creds.get("KIS_APP_KEY")
    app_secret = creds.get("KIS_APP_SECRET")
    rest_url = "https://openapi.koreainvestment.com:9443"
    
    token = await get_access_token(app_key, app_secret, rest_url)
    
    candidates = [
        "/uapi/domestic-stock/v1/quotations/after-hour-single-price-quotation",
        "/uapi/domestic-stock/v1/quotations/afterhour-single-price-quotation",
        "/uapi/domestic-stock/v1/quotations/after-hours-single-price-quotation",
        "/uapi/domestic-stock/v1/quotations/afterhours-single-price-quotation",
        "/uapi/domestic-stock/v1/ranking/after-hour-single-price-fluct",
        "/uapi/domestic-stock/v1/ranking/after-hours-single-price-fluct",
    ]
    
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "custtype": "P",
    }
    
    results = []
    async with httpx.AsyncClient(verify=False) as client:
        for path in candidates:
            url = f"{rest_url}{path}"
            # For ranking TR
            tr_id = "FHPST02400000" if "ranking" in path else "HHKST03040000"
            headers["tr_id"] = tr_id
            
            params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "005930"}
            if "ranking" in path:
                params = {
                    "fid_cond_mrkt_div_code": "J",
                    "fid_cond_scr_div_code": "20240",
                    "fid_input_iscd": "0000",
                    "fid_rank_sort_cls_code": "0",
                    "fid_prc_cls_code": "1"
                }

            resp = await client.get(url, headers=headers, params=params)
            results.append({"path": path, "status": resp.status_code, "data": resp.text[:100]})
            print(f"Tested {path}: {resp.status_code}")
            if resp.status_code == 200:
                print(f"SUCCESS on {path}!")
    
    return results

if __name__ == "__main__":
    asyncio.run(test_urls())
