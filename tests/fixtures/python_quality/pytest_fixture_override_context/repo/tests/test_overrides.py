import pytest
from src.app import app, require_account


@pytest.fixture
def account_override():
    def override_account() -> str:
        return "account-test"

    app.dependency_overrides[require_account] = override_account
    yield
    app.dependency_overrides.clear()


def test_uses_override(account_override) -> None:
    assert app.dependency_overrides[require_account]() == "account-test"
