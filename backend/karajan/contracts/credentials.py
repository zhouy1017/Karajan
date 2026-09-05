"""Reject credential-bearing keys in configuration values before recording them."""

def contains_credential(value: object) -> bool:
    if isinstance(value, dict):
        forbidden = {
            "api_key",
            "apikey",
            "access_token",
            "refresh_token",
            "authorization",
            "password",
            "client_secret",
            "secret",
            "token",
            "env",
            "environment",
            "headers",
        }
        return any(
            str(key).lower().replace("-", "_") in forbidden
            or str(key)
            .lower()
            .replace("-", "_")
            .endswith(
                ("_api_key", "_auth_token", "_access_token", "_refresh_token", "_client_secret")
            )
            or contains_credential(item)
            for key, item in value.items()
        )
    return isinstance(value, list) and any(contains_credential(item) for item in value)
