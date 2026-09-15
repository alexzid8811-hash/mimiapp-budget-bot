import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from app.auth import validate_init_data


def make_init_data(token: str, user_id: int = 42) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAExample",
        "user": json.dumps({"id": user_id, "first_name": "Alex"}, separators=(",", ":"), ensure_ascii=False),
    }
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_validate_init_data():
    token = "123456:TEST"
    user = validate_init_data(make_init_data(token), token)
    assert user.id == 42
    assert user.first_name == "Alex"
