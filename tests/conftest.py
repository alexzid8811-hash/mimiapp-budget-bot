import pytest

from app.db import reset_current_user, set_current_user


@pytest.fixture(autouse=True)
def default_test_user():
    """Most tests work as user 1.  Production code has no such default:
    connect() fails without a user (see test_user_isolation)."""
    token = set_current_user(1)
    yield
    reset_current_user(token)
