import asyncio
import httpx
from pathlib import Path
import json

async def official_sample_test():
    # Load credentials
    env_content = Path(".env").read_text()
    creds = {line.split("=")[0].strip(): line.split("=")[1].strip() for line in env_content.splitlines() if "=" in line}
    app_key, app_secret = creds.get("KIS_APP_KEY"), creds.get("KIS_APP_SECRET")
    rest_url = "https://openapi.koreainvestment.com:9443"
    
    # Get token
    body = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.post(f"{rest_url}/oauth2/tokenP", json=body)
        token = resp.json()["access_token"]
        
        # EXACT path from official GitHub sample
        path = "/uapi/domestic-stock/v1/ranking/after-hour-single-price-fluct"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": "FHPST02400000",
            "custtype": "P",
        }
        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_cond_scr_div_code": "20240",
            "fid_input_iscd": "0000",
            "fid_rank_sort_cls_code": "0",
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
        
        print(f"Testing EXACT official URL and params...")
        resp = await client.get(f"{rest_url}{path}", headers=headers, params=params)
        print(f"Status: {resp.status_code}")
        print(f"Response: {resp.text}")
        
        if resp.status_code == 200:
            print("!!! OFFICIAL SAMPLE WORKS !!!")

if __name__ == "__main__":
    asyncio.run(official_sample_test())
