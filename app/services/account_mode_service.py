"""A restricted student view of the local cihuy administrator; never mutate its DB role."""
from dataclasses import dataclass, field

from fastapi import HTTPException

from app.models.schemas import User


@dataclass(frozen=True)
class StudentModeUser:
    account: User
    role: str = field(default="mahasiswa", init=False)

    def __getattr__(self, name):
        return getattr(self.account, name)


def account_user(user):
    return user.account if isinstance(user, StudentModeUser) else user


def can_choose_login_mode(user) -> bool:
    user = account_user(user)
    return bool(user is not None and user.identifier == "cihuy" and user.is_active
                and user.auth_provider == "local" and user.role in ("admin", "super_admin"))


def student_mode_user(user: User) -> StudentModeUser:
    if not can_choose_login_mode(user):
        raise HTTPException(401, "Mode mahasiswa akun uji sudah tidak tersedia. Silakan login kembali.",
                            headers={"Cache-Control": "no-store"})
    return StudentModeUser(account_user(user))


def preserve_effective_user(locked: User, current_user):
    """Row locks must not silently restore administrator privileges in student mode."""
    return student_mode_user(locked) if isinstance(current_user, StudentModeUser) else locked
