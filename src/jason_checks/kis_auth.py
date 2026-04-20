"""KIS authentication - Approval Key issuance with 24h cache."""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
import httpx
import structlog

from jason_checks.config import get_urls, get_active_credentials, get_settings

logger = structlog.get_logger()

APPROVAL_KEY_CACHE_FILE = Path("data/.approval_key_cache.json")


async def issue_approval_key() -> str:
    """
    Issue Approval Key from KIS (for WebSocket).

    KIS endpoint: POST /oauth2/Approval
    Body: {grant_type, appkey, secretkey}

    Returns:
        Approval key string (24h valid)
    """
    settings = get_settings()
    app_key, app_secret, _ = get_active_credentials()
    rest_url, _ = get_urls(settings.kis_mode)
    # Reverting to rest_url (port 9443) as 443 is unreachable in this environment
    auth_url = rest_url

    # Check cache
    if APPROVAL_KEY_CACHE_FILE.exists():
        try:
            cache = json.loads(APPROVAL_KEY_CACHE_FILE.read_text())
            expire_at = datetime.fromisoformat(cache.get("expire_at", ""))
            if expire_at > datetime.now():
                logger.info("approval_key_from_cache")
                return cache["approval_key"]
        except Exception as e:
            logger.warning("cache_read_failed", error=str(e))

    # Issue new key
    endpoint = f"{auth_url}/oauth2/Approval"
    body = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "secretkey": app_secret,
    }
    headers = {"content-type": "application/json; charset=utf-8"}

    async with httpx.AsyncClient(verify=False) as client:
        try:
            resp = await client.post(endpoint, headers=headers, json=body, timeout=10.0)
            if resp.status_code != 200:
                logger.error("approval_key_request_failed", status=resp.status_code, text=resp.text)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("approval_key_exception", error=str(e))
            raise e

    approval_key = data.get("approval_key")
    if not approval_key:
        raise ValueError(f"No approval_key in response: {data}")

    # Cache it
    APPROVAL_KEY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cache_data = {
        "approval_key": approval_key,
        "issued_at": datetime.now().isoformat(),
        "expire_at": (datetime.now() + timedelta(hours=23)).isoformat(),
    }
    APPROVAL_KEY_CACHE_FILE.write_text(json.dumps(cache_data, indent=2))

    logger.info("approval_key_issued")
    return approval_key


async def get_approval_key() -> str:
    """Get cached approval key or issue new one."""
    return await issue_approval_key()


if __name__ == "__main__":
    async def test():
        try:
            key = await issue_approval_key()
            print(f"✓ Approval Key: {key}")
        except Exception as e:
            print(f"✗ Error: {type(e).__name__}: {e}")

    asyncio.run(test())
