"""Pairing exchanges for supported T3 authentication protocols."""

from t3_controller.transport import http_form, http_json


def oauth_exchange_payload(credential: str) -> dict[str, str]:
    return {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": credential,
        "subject_token_type": "urn:t3:params:oauth:token-type:environment-bootstrap",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "client_label": "hermes-agent",
        "client_device_type": "bot",
        "client_os": "macos",
    }


def bearer_token_from_oauth(result: dict) -> str:
    token = result.get("access_token")
    if result.get("token_type") != "Bearer" or not isinstance(token, str) or not token.strip():
        raise RuntimeError("T3 OAuth exchange did not return a bearer access token")
    return token.strip()


def bearer_token_from_bootstrap(result: dict) -> str:
    token = result.get("sessionToken")
    if result.get("authenticated") is not True or not isinstance(token, str) or not token.strip():
        raise RuntimeError("T3 pairing response did not include an authenticated bearer session")
    return token.strip()


def bootstrap_bearer(http_url: str, credential: str) -> str:
    session_state = http_json(f"{http_url}/api/auth/session", None)
    session_methods = session_state.get("auth", {}).get("sessionMethods", [])
    if "bearer-access-token" in session_methods:
        result = http_form(f"{http_url}/oauth/token", oauth_exchange_payload(credential))
        return bearer_token_from_oauth(result)

    result = http_json(
        f"{http_url}/api/auth/bootstrap/bearer",
        None,
        "POST",
        {"credential": credential},
    )
    return bearer_token_from_bootstrap(result)
