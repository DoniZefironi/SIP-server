import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from secrets import token_urlsafe
from uuid import uuid4

from anyio import CapacityLimiter, to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuthLoginLimit, AuthRefreshToken, AuthSession, AuthUser

ACCESS_TTL = timedelta(minutes=10)
SESSION_TTL = timedelta(days=7)
SESSION_IDLE_TTL = timedelta(days=1)
_LOGIN_WINDOW = timedelta(minutes=15)
_MAX_CACHED_BLOCKS = 4096
_ACCOUNT_ADMIN_LOCK = 1842936619
_TOKEN_FORMAT = re.compile(r"[A-Za-z0-9_-]{43}")
_PASSWORD_HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
_DUMMY_HASH = _PASSWORD_HASHER.hash(token_urlsafe(32))
_PASSWORD_LIMITER = CapacityLimiter(4)
_blocked_logins: OrderedDict[str, datetime] = OrderedDict()


class Role(StrEnum):
    VIEWER = "viewer"
    MANAGER = "manager"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class SessionTokens:
    access: str
    refresh: str
    refresh_ttl: int


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def token_digest(token: str | None) -> str | None:
    return _digest(token) if token is not None and _TOKEN_FORMAT.fullmatch(token) else None


async def hash_password(password: str) -> str:
    return await to_thread.run_sync(_PASSWORD_HASHER.hash, password, limiter=_PASSWORD_LIMITER)


async def verify_password(password: str, password_hash: str) -> bool:
    try:
        return await to_thread.run_sync(
            _PASSWORD_HASHER.verify, password_hash, password, limiter=_PASSWORD_LIMITER,
        )
    except (VerificationError, InvalidHashError):
        return False


async def consume_login_attempt(
    db: AsyncSession, username: str, address: str,
) -> datetime | None:
    now = datetime.now(UTC)
    limits = ((_digest(f"address:{address}"), 60), (_digest(f"username:{username}"), 10))
    for key, _ in limits:
        if (expires := _blocked_logins.get(key)) is not None:
            if expires > now:
                return expires
            _blocked_logins.pop(key, None)

    blocked = None
    async with db.begin():
        for key, limit in limits:
            new_expiry = now + _LOGIN_WINDOW
            expired = AuthLoginLimit.expires_at <= now
            row = (await db.execute(
                insert(AuthLoginLimit)
                .values(key=key, attempts=1, expires_at=new_expiry)
                .on_conflict_do_update(
                    index_elements=[AuthLoginLimit.key],
                    set_={
                        "attempts": case(
                            (expired, 1), else_=func.least(AuthLoginLimit.attempts + 1, limit + 1),
                        ),
                        "expires_at": case((expired, new_expiry), else_=AuthLoginLimit.expires_at),
                    },
                )
                .returning(AuthLoginLimit.attempts, AuthLoginLimit.expires_at)
            )).one()
            if row.attempts > limit:
                blocked = key, row.expires_at
                break
    if blocked is not None:
        key, expires_at = blocked
        _blocked_logins[key] = expires_at
        if len(_blocked_logins) > _MAX_CACHED_BLOCKS:
            _blocked_logins.popitem(last=False)
        return expires_at
    return None


async def _issue_tokens(db: AsyncSession, session: AuthSession, now: datetime) -> SessionTokens:
    access, refresh = token_urlsafe(32), token_urlsafe(32)
    session.idle_expires_at = min(session.expires_at, now + SESSION_IDLE_TTL)
    session.access_digest = _digest(access)
    session.access_expires_at = min(session.idle_expires_at, now + ACCESS_TTL)
    await db.flush()
    db.add(AuthRefreshToken(digest=_digest(refresh), session_id=session.id))
    refresh_ttl = max(1, int((session.idle_expires_at - now).total_seconds()))
    return SessionTokens(access, refresh, refresh_ttl)


async def login(
    db: AsyncSession, username: str, password: str,
) -> tuple[AuthUser, SessionTokens] | None:
    async with db.begin():
        user = await db.scalar(select(AuthUser).where(AuthUser.username == username))
        password_hash = user.password_hash if user is not None else _DUMMY_HASH

    valid = await verify_password(password, password_hash)
    if user is None or not valid:
        return None

    async with db.begin():
        user = await db.get(AuthUser, user.id, with_for_update=True, populate_existing=True)
        if user is None or not user.is_active or user.password_hash != password_hash:
            return None
        now = datetime.now(UTC)
        session = AuthSession(id=uuid4(), user_id=user.id, expires_at=now + SESSION_TTL)
        db.add(session)
        return user, await _issue_tokens(db, session, now)


async def rotate_session(db: AsyncSession, digest: str) -> tuple[AuthUser, SessionTokens] | None:
    async with db.begin():
        identity = (await db.execute(
            select(AuthSession.id, AuthSession.user_id)
            .join(AuthRefreshToken, AuthRefreshToken.session_id == AuthSession.id)
            .where(AuthRefreshToken.digest == digest)
        )).first()
        if identity is None:
            return None
        user = await db.get(AuthUser, identity.user_id, with_for_update=True)
        session = await db.get(AuthSession, identity.id, with_for_update=True)
        if session is None:
            return None
        record = await db.get(AuthRefreshToken, digest)
        now = datetime.now(UTC)
        if (
            record is None or record.used_at is not None or user is None or not user.is_active
            or min(session.expires_at, session.idle_expires_at) <= now
        ):
            await db.delete(session)
            return None
        record.used_at = now
        return user, await _issue_tokens(db, session, now)


async def current_user(db: AsyncSession, digest: str) -> AuthUser | None:
    now = datetime.now(UTC)
    async with db.begin():
        return await db.scalar(
            select(AuthUser).join(AuthSession, AuthSession.user_id == AuthUser.id).where(
                AuthSession.access_digest == digest,
                AuthSession.access_expires_at > now,
                AuthSession.idle_expires_at > now,
                AuthSession.expires_at > now,
                AuthUser.is_active.is_(True),
            )
        )


async def logout(db: AsyncSession, access: str | None, refresh: str | None) -> None:
    if access is None and refresh is None:
        return
    predicates = []
    if access is not None:
        predicates.append(AuthSession.access_digest == access)
    if refresh is not None:
        predicates.append(AuthSession.id.in_(
            select(AuthRefreshToken.session_id).where(AuthRefreshToken.digest == refresh)
        ))
    async with db.begin():
        await db.execute(delete(AuthSession).where(or_(*predicates)))


async def revoke_sessions(db: AsyncSession, user_id: int) -> None:
    await db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))


async def lock_account_management(db: AsyncSession) -> None:
    await db.execute(select(func.pg_advisory_xact_lock(_ACCOUNT_ADMIN_LOCK)))


async def active_admin_count(db: AsyncSession) -> int:
    return (await db.scalar(select(func.count()).select_from(AuthUser).where(
        AuthUser.role == Role.ADMIN, AuthUser.is_active.is_(True),
    ))) or 0


async def prune_auth(db: AsyncSession) -> None:
    now = datetime.now(UTC)
    async with db.begin():
        await db.execute(delete(AuthSession).where(AuthSession.idle_expires_at <= now))
        await db.execute(delete(AuthLoginLimit).where(AuthLoginLimit.expires_at <= now))
