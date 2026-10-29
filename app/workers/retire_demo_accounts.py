"""Explicit, data-preserving retirement of the former built-in demo accounts.

Run after the schema migration: python -m app.workers.retire_demo_accounts
Add --apply to deactivate the accounts. It never deletes documents/profiles.
"""

import argparse
import json
import secrets

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.session import SessionLocal
from app.models.schemas import Document, PlagiarismCheck, User
from app.routes.auth import password_hash


FORMER_DEMO_ROLES = {
    "198804152011011002": "admin",
    "0412087501": "dosen",
    "20210801142": "mahasiswa",
}


def retire_demo_accounts(db: Session, *, apply: bool = False) -> dict:
    admin = db.query(User).filter(User.identifier == "cihuy").first()
    if not admin or not admin.is_active or admin.role not in {"admin", "super_admin"}:
        raise RuntimeError("Admin cihuy yang aktif harus tersedia sebelum akun contoh dinonaktifkan.")

    candidates = (
        db.query(User)
        .filter(User.identifier.in_(FORMER_DEMO_ROLES))
        .with_for_update()
        .all()
    )
    # Validate the entire selection before changing any account.
    if any(user.role != FORMER_DEMO_ROLES[user.identifier] for user in candidates):
        raise RuntimeError("Role akun contoh telah berubah; periksa manual sebelum melanjutkan.")

    retained_documents = 0
    retained_checks = 0
    changed = 0
    for user in candidates:
        retained_documents += db.query(func.count(Document.id)).filter(Document.user_id == user.id).scalar()
        retained_checks += db.query(func.count(PlagiarismCheck.id)).filter(PlagiarismCheck.user_id == user.id).scalar()
        if user.is_active:
            changed += 1
            if apply:
                user.is_active = False
                user.session_version = int(user.session_version or 0) + 1
                # Old demo credentials also stop working if old application
                # code is restored during rollback. No password is displayed.
                user.password = password_hash.hash(secrets.token_urlsafe(48))
    if apply:
        db.commit()
    else:
        db.rollback()
    return {
        "applied": apply,
        "accounts_selected": len(candidates),
        "accounts_to_retire": changed,
        "documents_retained": retained_documents,
        "checks_retained": retained_checks,
        "retained_admin": "cihuy",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as session:
        print(json.dumps(retire_demo_accounts(session, apply=args.apply)))
