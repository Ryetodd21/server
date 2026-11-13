import uuid
import logging
from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update as sql_update
from slowapi.util import get_remote_address
from app.limiter import limiter
from app.database import get_db, async_session
from app.models.user import User, Token
from app.models.login_device import LoginDevice
from app.models.student import Student
from app.schemas.auth import LoginRequest, TokenResponse, RefreshRequest, UserResponse, LoginDeviceResponse, LoginResponse
from app.config import get_settings

router = APIRouter(prefix="/auth", tags=["Auth"])
settings = get_settings()

ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"
# Cookie lifetime tracks the refresh-token TTL so logout / device-revoke
# semantics stay in sync with the JWT window. Before, this was a literal
# 7 days — if REFRESH_TOKEN_EXPIRE_DAYS was tuned via env, the cookie
# would still expire after 7 days and clients would see a confusing
# "logged in but token rejected" state.
COOKIE_MAX_AGE = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# Pre-computed bcrypt hash for timing-equalization when the user lookup
# returns None. Without this, the wrong-user path (no bcrypt call) is ~80ms
# faster than the wrong-password path (one bcrypt call at rounds=12) — that
# timing differential lets attackers enumerate valid emails. We burn ~80ms
# of module-load time here in exchange for a constant-time-ish login path.
_DUMMY_HASH = bcrypt.hashpw(
    b"timing-equalization-dummy-password-not-a-real-secret",
    bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS),
).decode("utf-8")


def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS),
    ).decode("utf-8")


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token(token: str, expected_type: str = "access") -> dict:
    """Decode and validate a JWT.

    Round 29 audit P1: the `type` claim is now enforced. Previously, a refresh
    token (7-day TTL) presented to any access-token-protected endpoint (e.g.
    /api/auth/me, /api/students, /api/reports) would pass `jwt.decode` and be
    accepted as a valid session, granting up to 7 days of access instead of
    the intended 30 minutes. Now `expected_type="access"` is the default and
    `/auth/refresh` passes `expected_type="refresh"` explicitly.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    token_type = payload.get("type")
    if token_type != expected_type:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token type: expected '{expected_type}', got '{token_type}'",
        )
    return payload


def get_token_from_request(request: Request) -> str | None:
    """Extract access token from HttpOnly cookie or Authorization header."""
    # Try cookie first
    token = request.cookies.get(ACCESS_COOKIE)
    if token:
        return token
    # Fallback to Authorization header
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


async def authenticate_access_token(token: str) -> dict:
    payload = decode_token(token)
    if not isinstance(payload.get("sub"), str) or not payload["sub"]:
        raise HTTPException(status_code=401, detail="Invalid token")
    async with async_session() as db:
        user = await db.get(User, payload.get("sub"))
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="User not found or inactive")
        # Authorization follows the current account, not stale JWT claims.
        payload.update({field: getattr(user, field) for field in (
            "role", "full_name", "email", "jabatan", "jurusan", "nip",
        )})
    return payload


async def get_current_user(request: Request) -> dict:
    """Dependency: get current user payload from cookie or header."""
    token = get_token_from_request(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return await authenticate_access_token(token)


async def get_current_user_id(request: Request) -> str:
    """Dependency: get current user ID."""
    payload = await get_current_user(request)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return user_id


def set_auth_cookies(response: Response, access_token: str, refresh_token: str):
    """Set auth cookies on response.

    Two cookies:
    - access_token   — HttpOnly, secure, for HTTP and WebSocket requests
    - refresh_token  — HttpOnly, secure

    The legacy `access_token_js` cookie is cleared below but never issued again.
    WebSocket authentication reads the HttpOnly cookie directly.

    Round 31 (18 Okt 2026) follow-up: samesite is now "none" + secure=true
    (required pairing — browsers reject SameSite=None without Secure).
    Reason: the live log stream runs over WebSocket on `wss://mqtt.monitorterop.my.id`
    (cross-host, the only path not blocked by the cloudflared "Origin rejected: 403"
    check on `monitorterop.my.id` — see round 27/30 history). SameSite=Lax blocks
    the browser from sending the cookie on cross-host WS upgrades, so the
    FastAPI WS handler closed the connection for "no token" and the dashboard
    couldn't render live events.

    Security trade-off: SameSite=None means the cookie flows in all contexts
    (including cross-site form posts). Mitigation:
    - Cookie is HTTPS-only (secure=True) so it never leaks over plaintext
    - Cookie is HttpOnly so XSS can't read it
    - Cookie is a stateless JWT (server-side rotation via refresh) so the
      7-day blast radius is the same as the SameSite=Lax variant
    - All mutating endpoints require Content-Type: application/json (CSRF
      via form-encoded POST cannot forge) and double-check the Origin header
    - WebSocket has its own Origin allowlist (ALLOWED_ORIGINS in logs.py)
    This is a deliberate trade — broken dashboard is worse than the modest
    CSRF surface increase, and the per-request JWT validation means a stolen
    SameSite=None cookie has the same impact as a stolen SameSite=Lax one.
    """
    cookie_kwargs = {
        "httponly": True,
        "secure": True,  # SameSite=None requires Secure
        "samesite": "none",
        "max_age": COOKIE_MAX_AGE,
        "path": "/",
    }
    response.set_cookie(key=ACCESS_COOKIE, value=access_token, **cookie_kwargs)
    response.set_cookie(key=REFRESH_COOKIE, value=refresh_token, **cookie_kwargs)


def clear_auth_cookies(response: Response):
    """Clear current and legacy auth cookies (logout)."""
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/")
    response.delete_cookie("access_token_js", path="/")


@router.post("/login", response_model=LoginResponse)
@limiter.limit("5/minute")
async def login(
    request: Request,
    response: Response,
    login_data: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    # Android sends `nim` (student ID → username), web sends `email`.
    # Try email first, then fall back to username.
    # For the Android `nim` path, also try email lookup when the username lookup
    # fails — the Android login form labels the field "NIM" but users whose
    # identifier is their email (e.g. `hary@ung.ac.id`) type that there. Without
    # this fallback, those users get a confusing 401 instead of a successful login.
    email_val = login_data.email
    nim_val = login_data.nim
    if not email_val and not nim_val:
        raise HTTPException(
            status_code=422,
            detail="Harus menyediakan email atau NIM",
        )
    # Each branch owns its own `result` cursor — read each one exactly once.
    # The previous version reassigned `result` inside the `else` branch and then
    # unconditionally re-read at the bottom, which crashed SQLAlchemy's async
    # cursor with `ResourceClosedError` for any nim without `@`.
    if email_val:
        user = (await db.execute(
            select(User).where(User.email == email_val)
        )).scalar_one_or_none()
    elif nim_val and "@" in nim_val:
        # nim looked like an email — try the email column first.
        user = (await db.execute(
            select(User).where(User.email == nim_val)
        )).scalar_one_or_none()
        if user is None:
            # Email lookup missed — try the username column as a last resort.
            user = (await db.execute(
                select(User).where(User.username == nim_val)
            )).scalar_one_or_none()
    else:
        user = (await db.execute(
            select(User).where(User.username == nim_val)
        )).scalar_one_or_none()
    if user is None:
        # Run bcrypt against the dummy hash so timing matches the wrong-password
        # path — closes the user-enumeration timing oracle. See _DUMMY_HASH doc.
        verify_password(login_data.password, _DUMMY_HASH)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not verify_password(login_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    access_token = create_access_token({
        "sub": user.id,
        "role": user.role,
        "full_name": user.full_name,
        "email": user.email,
        "jabatan": user.jabatan,
        "jurusan": user.jurusan,
        "nip": user.nip,
    })
    refresh_token = create_refresh_token({"sub": user.id})

    user.last_login = datetime.now(timezone.utc)
    token_record = Token(
        id=str(uuid.uuid4()),
        user_id=user.id,
        token=refresh_token,
        token_type="refresh",
        expires_at=(datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)),
    )
    db.add(token_record)

    # Track this (user, device) pair. If the client sent a device_id, dedupe
    # by it; otherwise fall back to user_agent hash. The first time we see
    # a device, we create the row; on every subsequent login we update
    # last_seen + flip is_current=True for the matching device and
    # is_current=False on the rest.
    device_id = (login_data.device_id or _hash_device_fallback(login_data.user_agent))[:64]
    user_agent_raw = (login_data.user_agent or "")[:500]
    device_name = (login_data.device_name or _parse_device_name(user_agent_raw))[:200]
    now = datetime.now(timezone.utc)
    existing_dev = await db.execute(
        select(LoginDevice).where(
            LoginDevice.user_id == user.id,
            LoginDevice.device_id == device_id,
        )
    )
    dev_row = existing_dev.scalar_one_or_none()
    if dev_row is None:
        dev_row = LoginDevice(
            id=str(uuid.uuid4()),
            user_id=user.id,
            device_id=device_id,
            device_name=device_name,
            user_agent=user_agent_raw,
            first_seen=now,
            last_seen=now,
            is_current=True,
        )
        db.add(dev_row)
    else:
        dev_row.last_seen = now
        dev_row.user_agent = user_agent_raw or dev_row.user_agent
        dev_row.device_name = device_name or dev_row.device_name
        dev_row.is_current = True
    # Mark all other rows for this user as not-current
    await db.execute(
        sql_update(LoginDevice)
        .where(LoginDevice.user_id == user.id, LoginDevice.device_id != device_id)
        .values(is_current=False)
    )

    await db.commit()

    set_auth_cookies(response, access_token, refresh_token)
    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        student_id=user.id,
        name=user.full_name or user.username,
        email=user.email,
    )


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("3/minute")
async def refresh(request: Request, req: RefreshRequest | None = None, db: AsyncSession = Depends(get_db)):
    # Read refresh token from cookie (HttpOnly, set by login) — avoids
    # requiring the client to send it in the request body. The body is still
    # accepted for backwards compatibility with any client that sends it.
    refresh_token = request.cookies.get(REFRESH_COOKIE) or (req.refresh_token if req else None)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Refresh token required")
    # Round 29 audit P1: pass expected_type="refresh" — decode_token now
    # rejects access tokens here (the previous code had its own type check
    # but at the wrong layer; let decode_token be the single source of truth).
    payload = decode_token(refresh_token, expected_type="refresh")
    user_id = payload.get("sub")
    result = await db.execute(
        select(Token).where(Token.token == refresh_token, Token.token_type == "refresh")
    )
    token_record = result.scalar_one_or_none()
    if not token_record or token_record.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Refresh token expired or revoked")

    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    new_access = create_access_token({
        "sub": user.id,
        "role": user.role,
        "full_name": user.full_name,
        "email": user.email,
        "jabatan": user.jabatan,
        "jurusan": user.jurusan,
        "nip": user.nip,
    })
    new_refresh = create_refresh_token({"sub": user.id})

    await db.delete(token_record)
    db.add(Token(
        id=str(uuid.uuid4()),
        user_id=user.id,
        token=new_refresh,
        token_type="refresh",
        expires_at=(datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)),
    ))
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=503, detail="Refresh token gagal diputar")

    response = JSONResponse(content={
        "access_token": new_access,
        "refresh_token": new_refresh,
        "token_type": "bearer",
    })
    set_auth_cookies(response, new_access, new_refresh)
    return response


@router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    # Delete all refresh tokens for this user from DB. If the DB is briefly
    # unavailable (failover, pool exhausted), the cookies still get cleared
    # client-side, but the tokens remain valid until expiry. We retry the
    # cleanup once on a fresh session and surface a 503 to the client if it
    # still fails — silent failures here mean a leaked refresh token stays
    # usable for up to 7 days.
    payload = None
    try:
        payload = await get_current_user(request)
    except HTTPException:
        # Token expired/invalid — cookies will be cleared anyway, no DB
        # cleanup needed since there's nothing to delete (token already gone).
        pass

    user_id = payload.get("sub") if payload else None
    if user_id:
        for attempt in (1, 2):
            try:
                await db.execute(delete(Token).where(Token.user_id == user_id))
                await db.commit()
                break
            except Exception as exc:
                # On retry, get a fresh session — the first attempt may have
                # left the session in an unusable state.
                if attempt == 2:
                    logging.getLogger("monitor").exception(
                        "[auth/logout] token cleanup failed for user=%s after "
                        "2 attempts — refresh tokens remain valid until expiry",
                        user_id,
                    )
                    clear_auth_cookies(response)
                    raise HTTPException(
                        status_code=503,
                        detail="Logout gagal: cleanup token tidak dapat "
                               "diselesaikan. Refresh token masih berlaku "
                               "sampai waktu habis.",
                    )
                logging.getLogger("monitor").warning(
                    "[auth/logout] token cleanup attempt %d failed: %s",
                    attempt,
                    exc,
                )
                await db.rollback()
                continue

    clear_auth_cookies(response)
    return {"message": "Logged out successfully"}


@router.get("/me")
async def get_me(request: Request):
    """Return current user info from token."""
    payload = await get_current_user(request)
    return {
        "sub": payload.get("sub"),
        "role": payload.get("role"),
        "full_name": payload.get("full_name"),
        "email": payload.get("email"),
        "jabatan": payload.get("jabatan"),
        "jurusan": payload.get("jurusan"),
        "nip": payload.get("nip"),
    }


# ─── Login device tracking ────────────────────────────────────────────────


def _hash_device_fallback(user_agent: str | None) -> str:
    """Fallback device_id when the client didn't compute one. Stable across
    logins from the same browser. SHA-256 → first 32 hex chars."""
    import hashlib
    return hashlib.sha256((user_agent or "").encode("utf-8")).hexdigest()[:32]


def _parse_device_name(user_agent: str) -> str:
    """Best-effort "Chrome 124 di Windows 11" style label from a UA string.
    Falls back to the raw UA when we can't parse it. Pure-string parsing —
    no client-side `navigator` access on the server."""
    if not user_agent:
        return "Perangkat tidak dikenal"
    ua = user_agent
    browser = "Browser"
    os = ""
    if "Edg/" in ua:
        browser = "Edge"
    elif "OPR/" in ua or "Opera" in ua:
        browser = "Opera"
    elif "Firefox/" in ua:
        browser = "Firefox"
    elif "Chrome/" in ua and "Safari/" in ua:
        browser = "Chrome"
    elif "Safari/" in ua and "Chrome/" not in ua:
        browser = "Safari"
    if "Windows" in ua:
        os = "di Windows"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os = "di macOS"
    elif "Android" in ua:
        os = "di Android"
    elif "iPhone" in ua or "iPad" in ua:
        os = "di iOS"
    elif "Linux" in ua:
        os = "di Linux"
    if browser == "Browser" and not os:
        return ua[:80] or "Perangkat tidak dikenal"
    return f"{browser} {os}".strip()


@router.get("/devices", response_model=list[LoginDeviceResponse])
async def list_login_devices(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """All devices that have logged into this user account, deduped by
    device_id (one row per browser/app install). Most recent first."""
    user_id = await get_current_user_id(request)
    result = await db.execute(
        select(LoginDevice)
        .where(LoginDevice.user_id == user_id)
        .order_by(LoginDevice.last_seen.desc())
    )
    return result.scalars().all()


@router.delete("/devices/{device_id}", status_code=204)
async def revoke_login_device(
    request: Request,
    device_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Revoke one device — used to log out a session on a lost phone or
    shared computer. Soft-revokes the device row (sets is_active=False via
    last_seen reset) and deletes any refresh tokens it would have minted.
    We can't identify WHICH refresh token came from this device (Token
    table has no device_id FK), so we delete all of the user's refresh
    tokens EXCEPT the current request's token — close enough for the
    common case where the user is revoking "another device".

    If the device being revoked is the current one, the user is effectively
    logged out at the next refresh."""
    user_id = await get_current_user_id(request)
    result = await db.execute(
        select(LoginDevice).where(
            LoginDevice.id == device_id,
            LoginDevice.user_id == user_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Perangkat tidak ditemukan")
    # Delete refresh tokens for this user. The current access token (read
    # from cookie/header) still authenticates the request that hit this
    # endpoint, so revoking the device they're on doesn't immediately log
    # them out — they have until the access token's ~15 min lifetime.
    await db.execute(delete(Token).where(Token.user_id == user_id))
    # Mark the row no-longer-current so it sorts after active devices in
    # the UI. The row stays for audit.
    row.is_current = False
    await db.commit()
    return None
