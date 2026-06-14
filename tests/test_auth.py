import pytest
from app.security.auth import hash_password, verify_password, validate_password_strength


def test_hash_password():
    h = hash_password("Test1234!")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password("Test1234!", h)
    assert not verify_password("wrong", h)


def test_password_strength():
    assert validate_password_strength("short1!") is not None
    assert validate_password_strength("nouppercase1!") is not None
    assert validate_password_strength("NOLOWERCASE1!") is not None
    assert validate_password_strength("NoDigit!") is not None
    assert validate_password_strength("NoSpecial1") is not None
    assert validate_password_strength("Valid123!") is None
