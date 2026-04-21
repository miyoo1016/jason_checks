import asyncio
import sys
import json
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).parent.parent / "src"))

from jason_checks.kis_rest import get_access_token, get_urls, get_active_credentials, get_settings
import httpx

async def verify_investor_endpoints():
    settings = get_settings()
    token = await get_access_token()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    
    print(f"--- KIS Investor Endpoint Verification (Mode: {settings.kis_mode}) ---")
    
    # 1. Index Investor Trend (KOSPI 0001)
    url = f"{rest_url}/uapi/domestic-stock/v1/quotations/inquire-investor"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "FHPTJ04400000",
        "custtype": "P",
    }
    
    # Try the confirmed parameter combination
    params = {
        "fid_cond_mrkt_div_code": "U",
        "fid_cond_scr_div_code": "20440",
        "fid_input_iscd": "0001",
        "fid_div_cls_code": "0",
        "fid_rank_sort_cls_code": "0",
        "fid_etc_cls_code": "0",
    }
    
    print(f"\n[1] Testing Index Investor (KOSPI 0001)...")
    async with httpx.AsyncClient(verify=False) as client:
        try:
            resp = await client.get(url, headers=headers, params=params, timeout=5.0)
            print(f"Status: {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                print(f"rt_cd: {data.get('rt_cd')}, msg: {data.get('msg1')}")
                
                # Dump EVERYTHING
                print("\nFULL RESPONSE DUMP:")
                print(json.dumps(data, indent=2, ensure_ascii=False))
            else:
                print(f"Error: {resp.text}")
        except Exception as e:
            print(f"Exception: {str(e)}")

if __name__ == "__main__":
    asyncio.run(verify_investor_endpoints())
