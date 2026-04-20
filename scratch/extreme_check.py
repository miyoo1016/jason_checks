import asyncio
import sys
from pathlib import Path

sys.path.append(str(Path("src").absolute()))

from jason_checks.kis_rest import get_access_token, get_urls, get_active_credentials, get_settings
import httpx

async def extreme_check():
    settings = get_settings()
    token = await get_access_token()
    app_key, app_secret, _ = get_active_credentials()
    
    # Try both domains and both ports
    domains = [
        "https://openapi.koreainvestment.com:9443",
        "https://openapi.koreainvestment.com"
    ]
    
    paths = [
        "/uapi/domestic-stock/v1/quotations/after-hour-single-price-quotation",
        "/uapi/domestic-stock/v1/ranking/after-hour-single-price-fluct",
        "/uapi/domestic-stock/v1/ranking/fluctuation" # To verify the base
    ]
    
    async with httpx.AsyncClient(verify=False) as client:
        for domain in domains:
            for path in paths:
                url = f"{domain}{path}"
                tr_id = "FHPST01700000" if "fluctuation" in path else ("FHPST02400000" if "ranking" in path else "HHKST03040000")
                headers = {
                    "content-type": "application/json; charset=utf-8",
                    "authorization": f"Bearer {token}",
                    "appkey": app_key,
                    "appsecret": app_secret,
                    "tr_id": tr_id,
                    "custtype": "P",
                }
                params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "005930"}
                if "ranking" in path or "fluctuation" in path:
                    params.update({"fid_cond_scr_div_code": "20240" if "ranking" in path else "20170", "fid_rank_sort_cls_code": "0", "fid_prc_cls_code": "1"})
                    params["fid_input_iscd"] = "0000"

                try:
                    resp = await client.get(url, headers=headers, params=params, timeout=5.0)
                    print(f"URL: {url} | TR: {tr_id} | Status: {resp.status_code}")
                    if resp.status_code == 200:
                        print(f"!!! SUCCESS !!! -> {url}")
                except Exception as e:
                    print(f"URL: {url} | Error: {str(e)}")

if __name__ == "__main__":
    asyncio.run(extreme_check())
