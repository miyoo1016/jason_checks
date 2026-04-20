import asyncio
import sys
from pathlib import Path

sys.path.append(str(Path("src").absolute()))

from jason_checks.kis_rest import get_access_token, get_urls, get_active_credentials, get_settings
import httpx

async def brute_force_with_real_token():
    settings = get_settings()
    token = await get_access_token() # Uses the proven logic from the app
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    
    print(f"Token obtained successfully. Testing URLs on {rest_url}...")
    
    # Let's try every possible variation of the path
    parts = ["after-hour", "afterhour", "after-hours", "afterhours"]
    middles = ["single-price-", "single-", ""]
    ends = ["fluct", "fluctuation", "updown", "rank", "quotation", "pcon"]
    
    async with httpx.AsyncClient(verify=False) as client:
        for p in parts:
            for m in middles:
                for e in ends:
                    path = f"/uapi/domestic-stock/v1/ranking/{p}-{m}{e}"
                    tr_id = "FHPST02400000"
                    headers = {
                        "content-type": "application/json; charset=utf-8",
                        "authorization": f"Bearer {token}",
                        "appkey": app_key, "appsecret": app_secret,
                        "tr_id": tr_id, "custtype": "P",
                    }
                    params = {
                        "fid_cond_mrkt_div_code": "J", "fid_cond_scr_div_code": "20240",
                        "fid_input_iscd": "0000", "fid_rank_sort_cls_code": "0", "fid_prc_cls_code": "1"
                    }
                    try:
                        resp = await client.get(f"{rest_url}{path}", headers=headers, params=params)
                        if resp.status_code == 200:
                            print(f"!!! SUCCESS (Ranking) !!! -> {path}")
                            return
                    except: pass

                    path_q = f"/uapi/domestic-stock/v1/quotations/{p}-{m}{e}"
                    headers["tr_id"] = "HHKST03040000"
                    params_q = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "005930"}
                    try:
                        resp = await client.get(f"{rest_url}{path_q}", headers=headers, params=params_q)
                        if resp.status_code == 200:
                            print(f"!!! SUCCESS (Quotation) !!! -> {path_q}")
                            return
                    except: pass

if __name__ == "__main__":
    asyncio.run(brute_force_with_real_token())
