"""Database-backed login throttling; no separate cache or Cloudflare is needed."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import math
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Optional

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.schemas import LoginThrottle, utc_now


logger = logging.getLogger(__name__)


def _positive_setting(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} harus berupa bilangan bulat positif.") from exc
    if value < 1:
        raise RuntimeError(f"{name} harus lebih besar dari nol.")
    return value


@dataclass(frozen=True)
class LoginThrottleSettings:
    account_max_failures: int = 5
    ip_max_attempts: int = 30
    window_seconds: int = 900
    trusted_proxy_cidrs: tuple[str, ...] = ()

    @classmethod
    def from_environment(cls) -> "LoginThrottleSettings":
        cidrs = tuple(
            value.strip()
            for value in os.getenv("LOGIN_TRUSTED_PROXY_CIDRS", "").split(",")
            if value.strip()
        )
        # Reject bad configuration instead of silently trusting a wider subnet.
        for cidr in cidrs:
            ipaddress.ip_network(cidr, strict=False)
        return cls(
            account_max_failures=_positive_setting("LOGIN_ACCOUNT_MAX_FAILURES", 5),
            ip_max_attempts=_positive_setting("LOGIN_IP_MAX_ATTEMPTS", 30),
            window_seconds=_positive_setting("LOGIN_WINDOW_SECONDS", 900),
            trusted_proxy_cidrs=cidrs,
        )


class LoginThrottleService:
    def __init__(self, settings: Optional[LoginThrottleSettings] = None, now_provider=utc_now):
        self.settings = settings or LoginThrottleSettings.from_environment()
        self.now_provider = now_provider
        self.trusted_proxies = tuple(
            ipaddress.ip_network(cidr, strict=False)
            for cidr in self.settings.trusted_proxy_cidrs
        )

    @staticmethod
    def _ip(value: str):
        try:
            return ipaddress.ip_address(value.strip())
        except ValueError:
            return None

    def _trusted(self, address) -> bool:
        return any(address in network for network in self.trusted_proxies)

    def client_ip(self, request: Optional[Request]) -> str:
        """Trust forwarding headers only from an explicitly configured socket peer.

        Uvicorn must use --no-proxy-headers so an untrusted client cannot rewrite
        request.client before this check. Cloudflared may supply CF-Connecting-IP.
        """
        peer = self._ip(request.client.host) if request and request.client else None
        if peer is None:
            return "unknown"
        if not self._trusted(peer):
            return str(peer)

        cloudflare_ip = self._ip(request.headers.get("cf-connecting-ip", ""))
        if cloudflare_ip is not None:
            return str(cloudflare_ip)

        forwarded = request.headers.get("x-forwarded-for", "")
        if not forwarded or len(forwarded) > 4096:
            return str(peer)
        chain = forwarded.split(",")
        if len(chain) > 16:
            return str(peer)
        current = peer
        for item in reversed(chain):
            if not self._trusted(current):
                break
            address = self._ip(item)
            if address is None:
                return str(peer)
            current = address
        return str(current)

    @staticmethod
    def _key(kind: str, value: str) -> str:
        return f"{kind}:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _lock(db: Session, keys: tuple[str, ...]) -> None:
        # Tests use SQLite. PostgreSQL advisory locks also serialize creation
        # of absent buckets, which row locks alone cannot do safely.
        if db.get_bind().dialect.name == "postgresql":
            for key in sorted(keys):
                lock_key = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
                db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

    @staticmethod
    def _bucket(db: Session, key: str, now, window: int) -> LoginThrottle:
        bucket = db.query(LoginThrottle).filter(LoginThrottle.key == key).with_for_update().first()
        if bucket is None:
            bucket = LoginThrottle(key=key, attempt_count=0, expires_at=now + timedelta(seconds=window))
            db.add(bucket)
        elif bucket.expires_at <= now:
            bucket.attempt_count = 0
            bucket.expires_at = now + timedelta(seconds=window)
        return bucket

    def verify(self, db: Session, identifier: str, client_ip: str, verify_credentials: Callable[[], bool]) -> bool:
        """Count every IP attempt and failed account attempts in one transaction.

        Locks remain held while the password is checked, so concurrent attempts
        cannot all pass admission before their failures are counted. Buckets
        contain hashes of identifiers/IPs, never passwords or tokens.
        """
        account_key = self._key("account", identifier.strip().casefold())
        ip_key = self._key("ip", client_ip)
        now = self.now_provider()
        try:
            # Prune before acquiring admission locks to keep lock order stable.
            db.query(LoginThrottle).filter(LoginThrottle.expires_at <= now).delete(synchronize_session=False)
            self._lock(db, (account_key, ip_key))
            account = self._bucket(db, account_key, now, self.settings.window_seconds)
            source = self._bucket(db, ip_key, now, self.settings.window_seconds)
            exceeded = [
                bucket
                for bucket, limit in (
                    (account, self.settings.account_max_failures),
                    (source, self.settings.ip_max_attempts),
                )
                if bucket.attempt_count >= limit
            ]
            if exceeded:
                retry_after = max(1, math.ceil(max((bucket.expires_at - now).total_seconds() for bucket in exceeded)))
                db.commit()
                logger.warning("login_throttled account=%s source=%s", account_key[-12:], ip_key[-12:])
                raise HTTPException(
                    status_code=429,
                    detail="Terlalu banyak percobaan login. Silakan coba kembali setelah beberapa saat.",
                    headers={"Retry-After": str(retry_after), "Cache-Control": "no-store", "Pragma": "no-cache"},
                )

            source.attempt_count += 1
            valid = bool(verify_credentials())
            if valid:
                # A successful password clears failures without clearing the
                # IP's total-attempt budget (which also limits password spray).
                account.attempt_count = 0
            else:
                account.attempt_count += 1
            db.commit()
            if not valid:
                logger.warning("login_failed account=%s source=%s", account_key[-12:], ip_key[-12:])
            return valid
        except HTTPException:
            raise
        except Exception:
            db.rollback()
            # No exception/SQL parameters in public errors or auth logs.
            logger.error("login_throttle_unavailable")
            raise HTTPException(
                status_code=503,
                detail="Login sementara tidak tersedia. Silakan coba kembali.",
                headers={"Retry-After": "5", "Cache-Control": "no-store"},
            ) from None
