import asyncio
from jason_checks.kis_rest import fetch_stock_investor_trend, get_http_client, get_access_token, get_active_credentials, get_settings, get_urls

async def main():
    settings = get_settings()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    token = await get_access_token()

    url = f"{rest_url}/uapi/domestic-stock/v1/quotations/inquire-investor"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": "FHKST01010900",
        "custtype": "P",
    }
    params = {
        "fid_cond_mrkt_div_code": "J",
        "fid_input_iscd": "000660",
    }

    client = get_http_client()
    resp = await client.get(url, headers=headers, params=params, timeout=5.0)
    data = resp.json()
    print("OUTPUT[0]:", data.get("output", [])[0] if data.get("output") else "NONE")

if __name__ == "__main__":
    asyncio.run(main())
