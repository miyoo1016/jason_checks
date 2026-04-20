import asyncio
import httpx
import json
from pathlib import Path

async def get_access_token(app_key, app_secret, rest_url):
    endpoint = f"{rest_url}/oauth2/tokenP"
    body = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.post(endpoint, json=body)
        return resp.json()["access_token"]

async def test_base_logic():
    env_content = Path(".env").read_text()
    creds = {line.split("=")[0].strip(): line.split("=")[1].strip() for line in env_content.splitlines() if "=" in line}
    app_key, app_secret = creds.get("KIS_APP_KEY"), creds.get("KIS_APP_SECRET")
    rest_url = "https://openapi.koreainvestment.com:9443"
    token = await get_access_token(app_key, app_secret, rest_url)
    
    # 1. Test the one that worked before (Fluctuation ranking)
    path = "/uapi/domestic-stock/v1/ranking/fluctuation"
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
        "fid_input_iscd": "0000", "fid_rank_sort_cls_code": "0",
        "fid_prc_cls_code": "1", "fid_input_price_1": "", "fid_input_price_2": "",
        "fid_vol_cnt": "", "fid_trgt_cls_code": "0", "fid_trgt_exls_cls_code": "0",
        "fid_div_cls_code": "0", "fid_rsfl_rate1": "", "fid_rsfl_rate2": "",
    }
    
    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.get(f"{rest_url}{path}", headers=headers, params=params)
        print(f"Standard Ranking (/fluctuation): {resp.status_code}")
        
        # 2. Try to guess NXT URL based on successful pattern
        # Maybe it's just 'after-hour-rank' or similar?
        # Let's try some very simple ones
        guesses = [
            "/uapi/domestic-stock/v1/ranking/after-hour-fluctuation",
            "/uapi/domestic-stock/v1/ranking/after-hour-updown",
            "/uapi/domestic-stock/v1/ranking/after-hour-single-price-rank",
        ]
        for g in guesses:
            headers["tr_id"] = "FHPST02400000"
            resp = await client.get(f"{rest_url}{g}", headers=headers, params=params)
            print(f"Guessed {g}: {resp.status_code}")

if __name__ == "__main__":
    asyncio.run(test_base_logic())
