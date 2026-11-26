import logging
from datetime import UTC, datetime
from math import ceil
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SecretStr, StringConstraints
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import AuthUser
from app.db.session import get_session
from app.services import auth
from app.services.auth import Role, SessionTokens

router = APIRouter(tags=["auth"])
log = logging.getLogger(__name__)
Database = Annotated[AsyncSession, Depends(get_session)]
AuthSettings = Annotated[Settings, Depends(get_settings)]
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _normalize_username(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


Username = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$"),
    BeforeValidator(_normalize_username),
]
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
LoginPassword = Annotated[SecretStr, Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)]
NewPassword = Annotated[
    SecretStr, Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH),
]


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: Username
    password: LoginPassword


class CreateUser(Login):
    password: NewPassword
    role: Role


class UpdateUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role | None = None
    is_active: bool | None = None
    password: NewPassword | None = None


class ChangePassword(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: LoginPassword
    new_password: NewPassword


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    role: Role
    is_active: bool
    created_at: datetime


def _cookie_names(settings: Settings) -> tuple[str, str]:
    prefix = "__Host-" if settings.auth_secure_cookies else ""
    return f"{prefix}savt_access", f"{prefix}savt_refresh"


def _set_cookies(response: Response, settings: Settings, tokens: SessionTokens) -> None:
    for name, value, ttl in zip(
        _cookie_names(settings), (tokens.access, tokens.refresh),
        (min(int(auth.ACCESS_TTL.total_seconds()), tokens.refresh_ttl), tokens.refresh_ttl),
        strict=True,
    ):
        response.set_cookie(
            name, value, max_age=ttl, secure=settings.auth_secure_cookies,
            httponly=True, samesite="strict", path="/",
        )


def _clear_cookies(response: Response, settings: Settings) -> None:
    for name in _cookie_names(settings):
        response.delete_cookie(
            name, secure=settings.auth_secure_cookies, httponly=True, samesite="strict", path="/",
        )


def require_csrf(request: Request) -> None:
    site = request.headers.get("Sec-Fetch-Site")
    if request.headers.get("X-SAVT-CSRF") != "1" or site in {"cross-site", "same-site"}:
        raise HTTPException(403, "Запрос изменения должен исходить от интерфейса телефонии")


async def principal(request: Request, db: Database, settings: AuthSettings) -> AuthUser:
    access, _ = _cookie_names(settings)
    digest = auth.token_digest(request.cookies.get(access))
    if digest is None or (user := await auth.current_user(db, digest)) is None:
        raise HTTPException(401, "Требуется вход")
    if request.method not in _SAFE_METHODS:
        require_csrf(request)
    return user


Principal = Annotated[AuthUser, Depends(principal)]


def manager(user: Principal) -> AuthUser:
    if user.role not in {Role.MANAGER, Role.ADMIN}:
        raise HTTPException(403, "Требуется доступ к управлению АТС")
    return user


def admin(user: Principal) -> AuthUser:
    if user.role != Role.ADMIN:
        raise HTTPException(403, "Требуется роль администратора")
    return user


Admin = Annotated[AuthUser, Depends(admin)]


@router.post("/login", dependencies=[Depends(require_csrf)])
async def login(
    data: Login, request: Request, response: Response, db: Database, settings: AuthSettings,
) -> UserPublic:
    address = request.client.host if request.client else "unknown"
    if expires := await auth.consume_login_attempt(db, data.username, address):
        retry = max(1, ceil((expires - datetime.now(UTC)).total_seconds()))
        raise HTTPException(429, "Слишком много попыток входа", headers={"Retry-After": str(retry)})

    result = await auth.login(db, data.username, data.password.get_secret_value())
    if result is None:
        raise HTTPException(401, "Неверный логин или пароль")
    user, tokens = result
    _set_cookies(response, settings, tokens)
    return UserPublic.model_validate(user)


@router.post("/refresh", response_model=UserPublic, dependencies=[Depends(require_csrf)])
async def refresh(
    request: Request, response: Response, db: Database, settings: AuthSettings,
) -> UserPublic | Response:
    _, refresh_name = _cookie_names(settings)
    digest = auth.token_digest(request.cookies.get(refresh_name))
    if digest is None or (result := await auth.rotate_session(db, digest)) is None:
        failure = JSONResponse({"detail": "Сессия завершена"}, status_code=401)
        _clear_cookies(failure, settings)
        return failure
    user, tokens = result
    _set_cookies(response, settings, tokens)
    return UserPublic.model_validate(user)


@router.post("/logout", status_code=204, dependencies=[Depends(require_csrf)])
async def logout(request: Request, db: Database, settings: AuthSettings) -> Response:
    access_name, refresh_name = _cookie_names(settings)
    await auth.logout(
        db, auth.token_digest(request.cookies.get(access_name)),
        auth.token_digest(request.cookies.get(refresh_name)),
    )
    response = Response(status_code=204)
    _clear_cookies(response, settings)
    return response


@router.get("/me")
async def me(user: Principal) -> UserPublic:
    return UserPublic.model_validate(user)


@router.post("/change-password", status_code=204)
async def change_password(
    data: ChangePassword, user: Principal, db: Database, settings: AuthSettings,
) -> Response:
    password_hash = user.password_hash
    if not await auth.verify_password(data.current_password.get_secret_value(), password_hash):
        raise HTTPException(403, "Текущий пароль неверен")
    new_hash = await auth.hash_password(data.new_password.get_secret_value())
    async with db.begin():
        account = await db.get(AuthUser, user.id, with_for_update=True, populate_existing=True)
        if account is None or not account.is_active or account.password_hash != password_hash:
            raise HTTPException(401, "Сессия завершена")
        account.password_hash = new_hash
        await auth.revoke_sessions(db, account.id)
    log.info("Пользователь %s сменил свой пароль", user.id)
    response = Response(status_code=204)
    _clear_cookies(response, settings)
    return response


@router.get("/users", dependencies=[Depends(admin)])
async def users(
    db: Database,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[UserPublic]:
    async with db.begin():
        query = select(AuthUser).order_by(AuthUser.id).limit(limit).offset(offset)
        accounts = await db.scalars(query)
        return [UserPublic.model_validate(user) for user in accounts]


async def _lock_admin(db: AsyncSession, actor_id: int) -> None:
    await auth.lock_account_management(db)
    actor = await db.get(AuthUser, actor_id, with_for_update=True, populate_existing=True)
    if actor is None or not actor.is_active or actor.role != Role.ADMIN:
        raise HTTPException(403, "Требуется роль администратора")


@router.post("/users", status_code=201)
async def create_user(data: CreateUser, actor: Admin, db: Database) -> UserPublic:
    password_hash = await auth.hash_password(data.password.get_secret_value())
    async with db.begin():
        await _lock_admin(db, actor.id)
        account = await db.scalar(
            insert(AuthUser)
            .values(username=data.username, password_hash=password_hash, role=data.role)
            .on_conflict_do_nothing(index_elements=[AuthUser.username]).returning(AuthUser)
        )
        if account is None:
            raise HTTPException(409, "Такой логин уже существует")
    log.info("Администратор %s создал пользователя %s", actor.id, account.id)
    return UserPublic.model_validate(account)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: Annotated[int, Path(gt=0, lt=2**63)],
    data: UpdateUser,
    actor: Admin,
    db: Database,
) -> UserPublic:
    new_hash = await auth.hash_password(data.password.get_secret_value()) if data.password else None
    async with db.begin():
        await _lock_admin(db, actor.id)
        target = await db.get(AuthUser, user_id, with_for_update=True, populate_existing=True)
        if target is None:
            raise HTTPException(404, "Пользователь не найден")
        role = target.role if data.role is None else data.role
        active = target.is_active if data.is_active is None else data.is_active
        if target.is_active and target.role == Role.ADMIN and (role != Role.ADMIN or not active):
            if await auth.active_admin_count(db) <= 1:
                raise HTTPException(409, "Должен остаться хотя бы один активный администратор")

        if role != target.role or active != target.is_active or new_hash is not None:
            target.role, target.is_active = role, active
            if new_hash is not None:
                target.password_hash = new_hash
            await auth.revoke_sessions(db, target.id)
            log.info("Администратор %s изменил аккаунт %s", actor.id, target.id)
    return UserPublic.model_validate(target)
