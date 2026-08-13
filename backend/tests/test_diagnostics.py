from app.services.diagnostics import safe_error_message


def test_safe_error_message_preserves_diagnostics_and_redacts_credentials() -> None:
    message = safe_error_message(
        "request failed bearer secret-token api_key=sk-sensitive123 https://user:pass@example.com/repo"
    )

    assert "request failed" in message
    assert "secret-token" not in message
    assert "sk-sensitive123" not in message
    assert "user:pass" not in message
    assert message.count("[REDACTED]") >= 3
