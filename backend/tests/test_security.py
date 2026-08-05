from app.core.security import LoginThrottle, create_access_token, decode_access_token, hash_password, verify_password


def test_password_hash_round_trip() -> None:
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong", encoded)
    assert "correct horse" not in encoded


def test_access_token_rejects_tampering() -> None:
    token = create_access_token("user-1", "a-secret-long-enough")
    assert decode_access_token(token, "a-secret-long-enough")["sub"] == "user-1"
    assert decode_access_token(token + "x", "a-secret-long-enough") is None


def test_login_throttle_blocks_window_and_resets() -> None:
    throttle = LoginThrottle(max_failures=2, window_seconds=10)
    assert throttle.is_allowed("client", now=100)
    throttle.record_failure("client", now=100)
    throttle.record_failure("client", now=101)
    assert not throttle.is_allowed("client", now=102)
    assert throttle.is_allowed("client", now=112)
    throttle.record_failure("client", now=112)
    throttle.reset("client")
    assert throttle.is_allowed("client", now=113)
