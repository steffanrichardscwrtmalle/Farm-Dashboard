from app.services.xero_auth import _decrypt_token, _encrypt_token
from app.services.xero_oauth import build_oauth_state, parse_oauth_state


def test_xero_token_roundtrip():
    token = "sample-refresh-token-value"
    assert _decrypt_token(_encrypt_token(token)) == token


def test_oauth_state_roundtrip():
    state = build_oauth_state(user_id=7, return_to="/xero")
    parsed = parse_oauth_state(state)
    assert parsed["user_id"] == 7
    assert parsed["return_to"] == "/xero"
