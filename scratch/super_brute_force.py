import asyncio
import sys
from pathlib import Path

sys.path.append(str(Path("src").absolute()))

from jason_checks.kis_rest import get_access_token, get_urls, get_active_credentials, get_settings
import httpx

async def super_brute_force():
    settings = get_settings()
    token = await get_access_token()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    
    # Try different categories
    categories = ["ranking", "quotations", "trading", "market-data"]
    paths = [
        "after-hour-single-price-fluct",
        "after-hour-single-price-quotation",
        "after-hour-single-price-updown",
        "after-hour-single-price-rank"
    ]
    
    async with httpx.AsyncClient(verify=False) as client:
        for cat in categories:
            for p in paths:
                full_path = f"/uapi/domestic-stock/v1/{cat}/{p}"
                tr_id = "FHPST02400000" if "ranking" in cat or "rank" in p else "HHKST03040000"
                headers = {
                    "content-type": "application/json; charset=utf-8",
                    "authorization": f"Bearer {token}",
                    "appkey": app_key, "appsecret": app_secret,
                    "tr_id": tr_id, "custtype": "P",
                }
                params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "005930"}
                if "ranking" in cat or "rank" in p:
                    params.update({"fid_cond_scr_div_code": "20240", "fid_rank_sort_cls_code": "0", "fid_prc_cls_code": "1"})
                    params["fid_input_iscd"] = "0000"
                
                try:
                    resp = await client.get(f"{rest_url}{full_path}", headers=headers, params=params, timeout=2.0)
                    if resp.status_code == 200:
                        print(f"!!! SUCCESS !!! Path: {full_path}")
                        return
                except: pass

if __name__ == "__main__":
    asyncio.run(super_brute_force())
