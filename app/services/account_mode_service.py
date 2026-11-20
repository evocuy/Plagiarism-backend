"""Effective roles for accounts with more than one login mode."""
import os
from dataclasses import dataclass

from fastapi import HTTPException

from app.models.schemas import User
from app.services.sads_auth_service import StudentAuthSettings


@dataclass(frozen=True)
class LoginModeUser:
    account: User
    session_mode: str

    @property
    def role(self) -> str:
        if self.session_mode == "mahasiswa":
            return "mahasiswa"
        return "super_admin" if self.account.role == "super_admin" else "admin"

    def __getattr__(self, name):
        return getattr(self.account, name)


def account_user(user):
    return user.account if isinstance(user, LoginModeUser) else user


def is_sads_tester(identifier: str) -> bool:
    identifiers = os.getenv("SADS_TESTER_NIMS", "").split(",")
    return bool(identifier and identifier in {value.strip() for value in identifiers}
                and StudentAuthSettings.from_environment().provider == "sads")


def can_choose_login_mode(user) -> bool:
    user = account_user(user)
    if user is None or not user.is_active:
        return False
    if user.auth_provider == "sads":
        return user.role == "mahasiswa" and is_sads_tester(user.identifier)
    return user.identifier == "cihuy" and user.auth_provider == "local" and user.role in ("admin", "super_admin")


def user_for_login_mode(user: User, mode: str | None) -> User | LoginModeUser:
    if mode is None:
        return user
    if mode not in ("admin", "mahasiswa") or not can_choose_login_mode(user):
        raise HTTPException(401, "Mode sesi sudah tidak berlaku. Silakan login kembali.",
                            headers={"Cache-Control": "no-store"})
    return LoginModeUser(account_user(user), mode)


def student_mode_user(user: User) -> User | LoginModeUser:
    return user_for_login_mode(user, "mahasiswa")


def preserve_effective_user(locked: User, current_user):
    """Reapply the session's effective role to a locked account row."""
    if isinstance(current_user, LoginModeUser):
        return user_for_login_mode(locked, current_user.session_mode)
    return locked
