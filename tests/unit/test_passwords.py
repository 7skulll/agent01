"""密码摘要与账号字段校验，不访问数据库。"""

import pytest

from app.errors import ChatError
from app.users import (
    hash_password,
    permissions_for,
    validate_display_name,
    validate_role,
    validate_username,
    verify_password,
)


def test_password_hash_is_salted_and_verifiable():
    first = hash_password("correct-horse-battery")
    second = hash_password("correct-horse-battery")
    assert first != second
    assert first.startswith("scrypt$")
    assert "correct-horse-battery" not in first
    assert verify_password("correct-horse-battery", first)
    assert not verify_password("wrong-password", first)


def test_malformed_or_foreign_hash_never_verifies():
    assert not verify_password("x", "")
    assert not verify_password("x", "plain-text")
    assert not verify_password("x", "bcrypt$1$2$3$4$5")
    assert not verify_password("x", "scrypt$1$2$3$nothex$alsonothex")


@pytest.mark.parametrize("username", ["ab", "", "  ", "管理员", "a" * 33, "-lead", "has space"])
def test_invalid_usernames_are_rejected(username):
    with pytest.raises(ChatError) as error:
        validate_username(username)
    assert error.value.code == "invalid_username"


@pytest.mark.parametrize("username", ["abc", "alice", "a_b.c-d", "A1" + "x" * 30])
def test_valid_usernames_pass(username):
    assert validate_username(username) == username.strip()


def test_role_and_display_name_validation():
    assert validate_role("admin") == "admin"
    with pytest.raises(ChatError):
        validate_role("owner")
    assert validate_display_name("", "alice") == "alice"
    assert validate_display_name(" 员工 A ", "alice") == "员工 A"
    with pytest.raises(ChatError):
        validate_display_name("bad\nname", "alice")


def test_permissions_follow_role():
    assert permissions_for("admin") == permissions_for("admin")
    assert permissions_for("admin").manage_users is True
    assert permissions_for("staff").ingest_files is True
    assert permissions_for("staff").manage_users is False
    assert permissions_for("viewer").ingest_files is False
    assert permissions_for("viewer").manage_models is False
