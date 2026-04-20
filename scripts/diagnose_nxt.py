"""NXT (Nextrade) API 진단 스크립트.

여러 TR_ID/엔드포인트/시장코드 조합을 테스트해서 어떤 것이 실제로 Nextrade 데이터를 반환하는지 확인.
실행 방법: python scripts/diagnose_nxt.py
"""

import asyncio
import json
import httpx
import sys
from pathlib import Path

# Ensure src is in path
sys.path.append(str(Path(__file__).parent.parent / "src"))

from jason_checks.config import get_urls, get_active_credentials, get_settings
from jason_checks.kis_rest import get_access_token


# 테스트 종목 (삼성전자 - NXT 상장 확인됨)
TEST_CODE = "005930"


async def test_rest_endpoint(name: str, url_path: str, tr_id: str, params: dict):
    """단일 REST 엔드포인트 테스트."""
    settings = get_settings()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    token = await get_access_token()

    url = f"{rest_url}{url_path}"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": tr_id,
        "custtype": "P",
    }

    print(f"\n{'='*60}")
    print(f"[{name}] TR_ID={tr_id}")
    print(f"URL: {url}")
    print(f"Params: {params}")

    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(url, headers=headers, params=params, timeout=10.0)
        print(f"Status: {resp.status_code}")
        data = resp.json() if resp.status_code == 200 else {}
        print(f"rt_cd: {data.get('rt_cd')}, msg_cd: {data.get('msg_cd')}, msg1: {data.get('msg1', '')[:80]}")
        output = data.get("output") or data.get("output1") or data.get("output2") or {}
        if output:
            if isinstance(output, list):
                output = output[0] if output else {}
            keys = list(output.keys())[:10]
            print(f"Output keys (first 10): {keys}")
            # 주요 가격 필드 표시
            for k in ("stck_prpr", "stck_oprc", "prdy_ctrt", "acml_vol", "nxt_prpr"):
                if k in output:
                    print(f"  {k} = {output[k]}")
        else:
            print(f"⚠ No output data. Full response (first 200 chars): {json.dumps(data)[:200]}")
    except Exception as e:
        print(f"✗ ERROR: {e}")


async def main():
    """모든 후보 조합 테스트."""

    # Test 1: 표준 현재가 + 시장코드 NX
    await test_rest_endpoint(
        "후보 A: 표준 현재가 API + 시장 NX",
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        "FHKST01010100",
        {"fid_cond_mrkt_div_code": "NX", "fid_input_iscd": TEST_CODE},
    )

    # Test 2: 표준 현재가 + 시장코드 J (대조군)
    await test_rest_endpoint(
        "대조군: 표준 현재가 API + 시장 J",
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        "FHKST01010100",
        {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": TEST_CODE},
    )

    # Test 3: 통합 체결 조회
    await test_rest_endpoint(
        "후보 B: 체결 조회 + 시장 NX",
        "/uapi/domestic-stock/v1/quotations/inquire-ccnl",
        "FHKST01010300",
        {"fid_cond_mrkt_div_code": "NX", "fid_input_iscd": TEST_CODE},
    )

    # Test 4: 시간외 단일가 (구식 KRX)
    await test_rest_endpoint(
        "후보 C: 시간외 단일가 순위 (구식 KRX)",
        "/uapi/domestic-stock/v1/ranking/after-hour-single-price-fluct",
        "FHPST02400000",
        {
            "fid_cond_mrkt_div_code": "J",
            "fid_cond_scr_div_code": "20240",
            "fid_input_iscd": "0000",
            "fid_rank_sort_cls_code": "0",
            "fid_prc_cls_code": "1",
            "fid_input_price_1": "", "fid_input_price_2": "", "fid_vol_cnt": "",
            "fid_trgt_cls_code": "0", "fid_trgt_exls_cls_code": "0", "fid_div_cls_code": "0",
            "fid_rsfl_rate1": "", "fid_rsfl_rate2": "",
        },
    )

    # Test 5: NXT 전용 후보 (있다면)
    await test_rest_endpoint(
        "후보 D: NXT 전용 엔드포인트 후보",
        "/uapi/domestic-stock/v1/quotations/nxt-price",
        "FHKSTNX0100",
        {"fid_cond_mrkt_div_code": "NX", "fid_input_iscd": TEST_CODE},
    )

    print(f"\n{'='*60}")
    print("진단 완료. 각 테스트의 Status, rt_cd, Output keys를 확인하여")
    print("실제 동작하는 NXT 엔드포인트를 특정하세요.")


if __name__ == "__main__":
    asyncio.run(main())
