import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from threading import Lock
from time import time
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import DateTime, ForeignKey, String, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

COOKIE = "expense_session"
SESSION_DURATION = timedelta(days=7)
LOGIN_WINDOW_SECONDS = 10 * 60
router = APIRouter(prefix="/api/auth")


class AuthBase(DeclarativeBase):
    pass


class Workspace(AuthBase):
    __tablename__ = "business_workspaces"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))


class Account(AuthBase):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("business_workspaces.id"), index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(128))


class LoginSession(AuthBase):
    __tablename__ = "login_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Identity(BaseModel):
    id: str
    email: str
    workspace_id: str
    workspace_name: str


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=254)
    password: SecretStr


class Registration(Credentials):
    workspace_name: str = Field(max_length=100)


def workspace_identity(account: Account, workspace: Workspace) -> Identity:
    return Identity(id=account.id, email=account.email, workspace_id=workspace.id, workspace_name=workspace.name)


def scrypt_digest(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=3, maxmem=64 * 1024**2, dklen=32)


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"{salt.hex()}:{scrypt_digest(password, salt).hex()}"


def password_matches(password: str, stored: str) -> bool:
    salt, digest = stored.split(":")
    return hmac.compare_digest(scrypt_digest(password, bytes.fromhex(salt)), bytes.fromhex(digest))


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def secure_cookie() -> bool:
    return os.getenv("AUTH_COOKIE_SECURE", "true").lower() != "false"


def allowed_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    configured = os.getenv("APP_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173")
    trusted = {item.strip().rstrip("/") for item in configured.split(",") if item.strip()}
    return request.headers.get("sec-fetch-site") != "cross-site" and (origin is None or origin in trusted)


@lru_cache
def limiter_connection(url: str) -> Redis:
    return Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3)


class LocalRateLimits:
    def __init__(self):
        self.window = 0
        self.counts: dict[str, int] = {}
        self.lock = Lock()

    def increment(self, key: str, window: int) -> int:
        with self.lock:
            if window != self.window:
                self.window = window
                self.counts.clear()
            self.counts[key] = self.counts.get(key, 0) + 1
            return self.counts[key]


def limit_attempt(request: Request, bucket: str, limit: int) -> None:
    window = int(time()) // LOGIN_WINDOW_SECONDS
    address = request.client.host if request.client else "unknown"
    key = f"auth:{bucket}:{token_digest(address)}:{window}"
    url = request.app.state.auth_redis_url
    if url:
        try:
            with limiter_connection(url).pipeline() as pipe:
                count, _ = pipe.incr(key).expire(key, LOGIN_WINDOW_SECONDS).execute()
        except RedisError as exc:
            raise HTTPException(503, "Sign-in is temporarily unavailable. Try again soon.") from exc
    else:
        count = request.app.state.auth_rate_limits.increment(key, window)
    if count > limit:
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")


def current_account(request: Request) -> Identity:
    token = request.cookies.get(COOKIE)
    if token and len(token) == 43:
        with Session(request.app.state.engine) as session:
            identity = session.execute(
                select(Account, Workspace).select_from(Account)
                .join(LoginSession, LoginSession.account_id == Account.id)
                .join(Workspace, Workspace.id == Account.workspace_id)
                .where(LoginSession.token_hash == token_digest(token), LoginSession.expires_at > datetime.now(timezone.utc)),
            ).one_or_none()
            if identity:
                return workspace_identity(*identity)
    raise HTTPException(401, "Sign in to continue.")


CurrentAccount = Annotated[Identity, Depends(current_account)]


def start_session(session: Session, account: Account, request: Request, response: Response) -> None:
    now = datetime.now(timezone.utc)
    previous_token = request.cookies.get(COOKIE)
    session.execute(delete(LoginSession).where(LoginSession.expires_at <= now))
    if previous_token and len(previous_token) == 43:
        session.execute(delete(LoginSession).where(LoginSession.token_hash == token_digest(previous_token)))
    token = secrets.token_urlsafe(32)
    session.add(LoginSession(token_hash=token_digest(token), account_id=account.id, expires_at=now + SESSION_DURATION))
    session.flush()
    response.set_cookie(
        COOKIE, token, max_age=int(SESSION_DURATION.total_seconds()), httponly=True,
        secure=secure_cookie(), samesite="lax", path="/",
    )


@router.post("/register", status_code=201)
def register(data: Registration, request: Request, response: Response) -> Identity:
    limit_attempt(request, "registration", 10)
    email = data.email.strip().casefold()
    password = data.password.get_secret_value()
    name = data.workspace_name.strip()
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(422, "Enter a valid email address.")
    if not name or not 15 <= len(password) <= 128:
        raise HTTPException(422, "Enter a business name and a password of 15 to 128 characters.")
    with Session(request.app.state.engine) as session:
        workspace = Workspace(id=uuid4().hex, name=name)
        account = Account(id=uuid4().hex, workspace_id=workspace.id, email=email, password_hash=password_hash(password))
        try:
            session.add(workspace)
            session.flush()
            session.add(account)
            session.flush()
            start_session(session, account, request, response)
            identity = workspace_identity(account, workspace)
            session.commit()
        except IntegrityError as exc:
            raise HTTPException(409, "This account already exists. Try signing in.") from exc
        return identity


@router.post("/login")
def login(data: Credentials, request: Request, response: Response) -> Identity:
    limit_attempt(request, "login", 20)
    password = data.password.get_secret_value()
    if not 15 <= len(password) <= 128:
        raise HTTPException(401, "Email or password is incorrect.")
    with Session(request.app.state.engine) as session:
        row = session.execute(select(Account, Workspace).join(Workspace, Workspace.id == Account.workspace_id)
                              .where(Account.email == data.email.strip().casefold())).one_or_none()
        stored = row[0].password_hash if row else ("00" * 16 + ":" + "00" * 32)
        matches = password_matches(password, stored)
        if not row or not matches:
            raise HTTPException(401, "Email or password is incorrect.")
        account, workspace = row
        identity = workspace_identity(account, workspace)
        start_session(session, account, request, response)
        session.commit()
        return identity


@router.get("/me")
def me(account: CurrentAccount) -> Identity:
    return account


@router.post("/logout", status_code=204, response_class=Response)
def logout(request: Request) -> Response:
    token = request.cookies.get(COOKIE)
    if token and len(token) == 43:
        with Session(request.app.state.engine) as session:
            session.execute(delete(LoginSession).where(LoginSession.token_hash == token_digest(token)))
            session.commit()
    response = Response(status_code=204)
    response.delete_cookie(COOKIE, path="/", secure=secure_cookie(), httponly=True, samesite="lax")
    return response
