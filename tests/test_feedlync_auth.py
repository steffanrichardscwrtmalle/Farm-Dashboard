from app.services.feedlync_auth import _decrypt_token, _encrypt_token
from app.services.feedlync_oauth import build_authorize_url, generate_pkce_pair


def test_pkce_pair_is_valid() -> None:
    verifier, challenge = generate_pkce_pair()
    assert len(verifier) > 40
    assert len(challenge) > 20
    assert "=" not in challenge


def test_build_authorize_url_contains_required_params() -> None:
    _, challenge = generate_pkce_pair()
    url = build_authorize_url(code_challenge=challenge, state="test-state")
    assert "client_id=" in url
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert "state=test-state" in url
    assert "response_type=code" in url


def test_encrypt_decrypt_roundtrip() -> None:
    token = "eyJ.test.refresh.token"
    encrypted = _encrypt_token(token)
    assert encrypted != token
    assert _decrypt_token(encrypted) == token
