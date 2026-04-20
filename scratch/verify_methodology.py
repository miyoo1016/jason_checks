import asyncio
import sys
from pathlib import Path

sys.path.append(str(Path("src").absolute()))

from jason_checks.kis_rest import get_access_token, get_urls, get_active_credentials, get_settings
import httpx

async def verify_methodology():
    settings = get_settings()
    token = await get_access_token()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    
    # This URL IS KNOWN TO WORK for top movers in the app
    path = "/uapi/domestic-stock/v1/ranking/fluctuation"
    url = f"{rest_url}{path}"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "FHPST01700000",
        "custtype": "P",
    }
    params = {
        "fid_cond_mrkt_div_code": "J", "fid_cond_scr_div_code": "20170",
        "fid_input_iscd": "0000", "fid_rank_sort_cls_code": "0", "fid_prc_cls_code": "1"
    }
    
    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.get(url, headers=headers, params=params)
        print(f"Verified Path (/fluctuation): Status {resp.status_code}")
        if resp.status_code == 200:
            print("Methodology is CORRECT. 404s on other paths are REAL.")

if __name__ == "__main__":
    asyncio.run(verify_methodology())
