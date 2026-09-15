from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException


@dataclass(frozen=True)
class TelegramUser:
    id: int
    first_name: str = ""
    username: str | None = None


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> TelegramUser:
    """Validate Telegram Mini App initData according to Telegram's HMAC scheme."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise ValueError("hash is missing")

    auth_date_raw = pairs.get("auth_date")
    if not auth_date_raw:
        raise ValueError("auth_date is missing")
    auth_date = int(auth_date_raw)
    if max_age_seconds > 0 and abs(int(time.time()) - auth_date) > max_age_seconds:
        raise ValueError("initData is expired")

    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise ValueError("invalid initData signature")

    user_raw = pairs.get("user")
    if not user_raw:
        raise ValueError("user is missing")
    user_data = json.loads(user_raw)
    return TelegramUser(
        id=int(user_data["id"]),
        first_name=str(user_data.get("first_name", "")),
        username=user_data.get("username"),
    )


async def current_user(x_telegram_init_data: str | None = Header(default=None)) -> TelegramUser:
    dev_mode = os.getenv("DEV_MODE", "false").lower() in {"1", "true", "yes", "on"}
    bot_token = os.getenv("BOT_TOKEN", "")
    max_age = int(os.getenv("INIT_DATA_MAX_AGE_SECONDS", "86400"))

    if x_telegram_init_data and bot_token:
        try:
            return validate_init_data(x_telegram_init_data, bot_token, max_age)
        except Exception as exc:
            raise HTTPException(status_code=401, detail=f"Telegram auth failed: {exc}") from exc

    if dev_mode:
        return TelegramUser(id=1, first_name="Локальный пользователь")

    raise HTTPException(status_code=401, detail="Open the app from Telegram or enable DEV_MODE locally")
