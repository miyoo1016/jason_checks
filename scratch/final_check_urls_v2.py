import asyncio
import sys
import os
from pathlib import Path

sys.path.append(str(Path("src").absolute()))

from jason_checks.kis_rest import get_access_token, get_urls, get_active_credentials, get_settings
import httpx

async def final_check():
    settings = get_settings()
    token = await get_access_token()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    
    candidates = [
        # Quotation - hyphens are tricky
        ("/uapi/domestic-stock/v1/quotations/after-hour-single-price-quotation", "HHKST03040000"),
        ("/uapi/domestic-stock/v1/quotations/afterhour-single-price-quotation", "HHKST03040000"),
        ("/uapi/domestic-stock/v1/quotations/after-hours-single-price-quotation", "HHKST03040000"),
        # Ranking
        ("/uapi/domestic-stock/v1/ranking/after-hour-single-price-fluct", "FHPST02400000"),
        ("/uapi/domestic-stock/v1/ranking/after-hour-single-price-updown", "FHPST02400000"),
        ("/uapi/domestic-stock/v1/ranking/market-time-updown", "FHPST02400000"),
        # Try without ranking/quotation subpaths
        ("/uapi/domestic-stock/v1/after-hour-single-price-fluct", "FHPST02400000"),
        ("/uapi/domestic-stock/v1/after-hour-single-price-quotation", "HHKST03040000"),
    ]
    
    async with httpx.AsyncClient(verify=False) as client:
        for path, tr_id in candidates:
            url = f"{rest_url}{path}"
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {token}",
                "appkey": app_key,
                "appsecret": app_secret,
                "tr_id": tr_id,
                "custtype": "P",
            }
            params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "005930"}
            if tr_id == "FHPST02400000":
                params.update({"fid_cond_scr_div_code": "20240", "fid_rank_sort_cls_code": "0", "fid_prc_cls_code": "1"})
                
            resp = await client.get(url, headers=headers, params=params)
            print(f"Path: {path} | Status: {resp.status_code}")
            if resp.status_code == 200:
                print(f"!!! FOUND WORKING URL: {path} !!!")
                return path

if __name__ == "__main__":
    asyncio.run(final_check())
