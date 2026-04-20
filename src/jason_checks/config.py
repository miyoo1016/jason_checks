"""Configuration module - handles .env loading and KIS URL routing."""

import os
from pathlib import Path
from typing import Literal
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Settings loaded from .env file."""

    kis_mode: Literal["paper", "live"] = "paper"

    # Paper (mock) credentials
    kis_app_key: str = ""
    kis_app_secret: str = ""
    kis_account_no: str = ""

    # Live (production) credentials (optional)
    kis_app_key_live: str = ""
    kis_app_secret_live: str = ""
    kis_account_no_live: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


def get_settings() -> Settings:
    """Load settings from .env file."""
    return Settings()


def get_urls(mode: Literal["paper", "live"] = "paper") -> tuple[str, str]:
    """
    Get KIS API URLs based on mode.

    Args:
        mode: "paper" for mock trading, "live" for real trading.

    Returns:
        Tuple of (rest_base_url, websocket_url)
    """
    if mode == "paper":
        return (
            "https://openapivts.koreainvestment.com:29443",
            "ws://ops.koreainvestment.com:21000"
        )
    else:  # live
        return (
            "https://openapi.koreainvestment.com:9443",
            "ws://ops.koreainvestment.com:31000"
        )


def get_active_credentials() -> tuple[str, str, str]:
    """
    Get active credentials based on KIS_MODE.

    Returns:
        Tuple of (app_key, app_secret, account_no)
    """
    settings = get_settings()

    if settings.kis_mode == "paper":
        return (
            settings.kis_app_key,
            settings.kis_app_secret,
            settings.kis_account_no
        )
    else:
        return (
            settings.kis_app_key_live,
            settings.kis_app_secret_live,
            settings.kis_account_no_live
        )


if __name__ == "__main__":
    settings = get_settings()
    print(f"KIS Mode: {settings.kis_mode}")
    rest_url, ws_url = get_urls(settings.kis_mode)
    print(f"REST URL: {rest_url}")
    print(f"WebSocket URL: {ws_url}")
    app_key, app_secret, account = get_active_credentials()
    print(f"Account: {account}")
    print(f"App Key (first 10): {app_key[:10]}...")
