import asyncio
import sys
from pathlib import Path

sys.path.append(str(Path("src").absolute()))

from jason_checks.kis_rest import get_access_token, get_urls, get_active_credentials, get_settings
import httpx

async def scan_urls():
    settings = get_settings()
    token = await get_access_token()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    
    # Generate possible combinations
    prefixes = ["afterhour", "after-hour", "afterhours", "after-hours"]
    middles = ["single-price-", "single-", ""]
    suffixes = ["fluct", "fluctuation", "updown", "rank", "quotation", "quot"]
    
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "custtype": "P",
    }
    
    async with httpx.AsyncClient(verify=False) as client:
        for p in prefixes:
            for m in middles:
                for s in suffixes:
                    path = f"/uapi/domestic-stock/v1/ranking/{p}-{m}{s}"
                    tr_id = "FHPST02400000"
                    headers["tr_id"] = tr_id
                    params = {
                        "fid_cond_mrkt_div_code": "J", "fid_cond_scr_div_code": "20240",
                        "fid_input_iscd": "0000", "fid_rank_sort_cls_code": "0", "fid_prc_cls_code": "1"
                    }
                    try:
                        resp = await client.get(f"{rest_url}{path}", headers=headers, params=params)
                        if resp.status_code == 200:
                            print(f"!!! SUCCESS !!! Path: {path}")
                            return path
                    except: pass
                    
                    # Also try quotations path
                    path_q = f"/uapi/domestic-stock/v1/quotations/{p}-{m}{s}"
                    headers["tr_id"] = "HHKST03040000"
                    params_q = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "005930"}
                    try:
                        resp = await client.get(f"{rest_url}{path_q}", headers=headers, params=params_q)
                        if resp.status_code == 200:
                            print(f"!!! SUCCESS !!! Path: {path_q}")
                            return path_q
                    except: pass

if __name__ == "__main__":
    asyncio.run(scan_urls())
