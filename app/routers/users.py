import uuid
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
import bcrypt

from app.database import get_db
from app.limiter import limiter
from app.models.user import User, Token
from app.routers.auth import get_current_user, get_current_user_id, get_password_hash, verify_password
from app.schemas.auth import (
    UserCreate, UserUpdate, UserResponse, ProfileUpdate, PasswordChange
)

router = APIRouter(prefix="/users", tags=["Users"])


def user_to_response(user: User, include_avatar: bool = True) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        full_name=user.full_name or "",
        jabatan=user.jabatan or "",
        jurusan=user.jurusan or "",
        nip=user.nip or "",
        phone=user.phone or "",
        avatar_url=user.avatar_url or "" if include_avatar else "",
        bio=user.bio or "",
        role=user.role,
        is_active=user.is_active,
        last_login=user.last_login.isoformat() if user.last_login else None,
        created_at=user.created_at.isoformat() if user.created_at else None,
    )


async def require_admin(request: Request) -> dict:
    """Only admin role can manage users."""
    payload = await get_current_user(request)
    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Hanya admin yang boleh mengelola akun")
    return payload


# ─── Profile endpoints (user edits own profile) ────────────────────────────────

@router.get("/me", response_model=UserResponse)
async def get_my_profile(
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """User ambil profilenya sendiri."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user_to_response(user)


@router.put("/me", response_model=UserResponse)
async def update_my_profile(
    data: ProfileUpdate,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """User edit profilenya sendiri."""
    update_data = {k: v for k, v in data.model_dump().items() if v is not None}
    current = await db.get(User, user_id)
    if current and current.role == "dosen" and "jurusan" in update_data and update_data["jurusan"] != current.jurusan:
        raise HTTPException(403, "Perubahan afiliasi dosen hanya dapat dilakukan admin")
    if not update_data:
        raise HTTPException(status_code=400, detail="Tidak ada field yang diubah")

    # Check email uniqueness if changing
    if "email" in update_data:
        existing = await db.execute(
            select(User).where(User.email == update_data["email"], User.id != user_id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Email sudah dipakai user lain")
        update_data["email"] = update_data["email"].lower()

    result = await db.execute(
        update(User).where(User.id == user_id).values(**update_data)
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="User not found")
    await db.commit()

    refreshed = await db.execute(select(User).where(User.id == user_id))
    user = refreshed.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user_to_response(user)


# Round 27 — FCM device-token registration.
# The Android `FirebaseMessagingService` posts the device's FCM token
# here whenever it's refreshed (token-rotation is a Google-driven
# process, not user-driven). Tokens are stored in `users.fcm_token`
# (one per user, latest wins). When the user signs out, the Android
# client also POSTs an empty token to clear the field — so a returned
# device doesn't keep receiving push for the previous user.
@router.put("/me/fcm-token", response_model=dict)
@limiter.limit("30/hour")
async def register_fcm_token(
    request: Request,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    token = (payload or {}).get("token", "").strip()
    if token and len(token) > 4096:
        raise HTTPException(status_code=400, detail="FCM token terlalu panjang")
    await db.execute(
        update(User).where(User.id == user_id).values(fcm_token=token or None)
    )
    await db.commit()
    return {"ok": True, "registered": bool(token)}


@router.put("/me/password", response_model=dict)
@limiter.limit("5/hour")
async def change_my_password(
    request: Request,
    data: PasswordChange,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """User ganti password sendiri."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not verify_password(data.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Password lama salah")

    new_hash = get_password_hash(data.new_password)
    await db.execute(
        update(User).where(User.id == user_id).values(hashed_password=new_hash)
    )
    # Round 26 audit (P0 #4): revoke all refresh tokens issued before this
    # password change. Without this, a stolen refresh token (up to 7 days
    # lifetime) stays valid even after the user rotated their password —
    # which is exactly the scenario the rotation is meant to defend against.
    await db.execute(delete(Token).where(Token.user_id == user_id))
    await db.commit()
    return {"message": "Password berhasil diubah"}


# ─── Admin endpoints (manage all users) ────────────────────────────────────────

@router.post("/register", response_model=UserResponse)
@limiter.limit("3/hour")
async def register_user(
    request: Request,
    data: UserCreate,
    db: AsyncSession = Depends(get_db),
):
    """Public registration endpoint — creates account as pending (inactive) for admin approval."""
    existing = await db.execute(
        select(User).where((User.email == data.email.lower()) | (User.username == data.username))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username atau email sudah dipakai")

    if data.role not in {"mahasiswa", "dosen"}:
        raise HTTPException(status_code=422, detail="Registrasi hanya untuk mahasiswa atau dosen")
    role = data.role

    user = User(
        id=str(uuid.uuid4()),
        username=data.username,
        email=data.email.lower(),
        hashed_password=get_password_hash(data.password),
        full_name=data.full_name,
        jabatan=data.jabatan,
        jurusan=data.jurusan,
        nip=data.nip,
        phone=data.phone,
        avatar_url=data.avatar_url,
        bio=data.bio,
        role=role,
        is_active=False,  # Inactive until admin approves
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user_to_response(user)


@router.get("", response_model=list[UserResponse])
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    """List all users (admin only)."""
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()
    return [user_to_response(u, include_avatar=False) for u in users]


@router.get("/advisors", response_model=list[UserResponse])
async def list_advisors(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    """List dosen/admin users that can be assigned as Dosen PA — any authenticated user.

    Returns users with role=admin or dosen, with id/full_name. The student-create
    flow (admin/dosen) needs this for the "Pilih Dosen PA" dropdown without
    exposing the full user table to the mahasiswa role.
    """
    result = await db.execute(
        select(User)
        .where(User.role.in_(("admin", "dosen")))
        .where(User.is_active == True)
        .order_by(User.full_name.asc())
    )
    users = result.scalars().all()
    return [user_to_response(u, include_avatar=False) for u in users]


@router.post("/create-student", response_model=UserResponse)
async def create_student_account(
    data: UserCreate,
    db: AsyncSession = Depends(get_db),
    _payload: dict = Depends(get_current_user),
):
    """Create a mahasiswa account (admin/dosen only).

    Dosen use this when adding a student via the dashboard — they cannot use
    POST /api/users (admin-only) and POST /api/users/register (public, requires
    approval). This endpoint lets dosen register a mahasiswa with active=true so
    the student can log in immediately. Role is forced to "mahasiswa" server-side;
    any other role submitted by the client is rejected.
    """
    if _payload.get("role") not in ("admin", "dosen"):
        raise HTTPException(status_code=403, detail="Hanya admin/dosen yang boleh menambah akun mahasiswa")
    if data.role != "mahasiswa":
        raise HTTPException(
            status_code=400,
            detail="Endpoint ini hanya untuk akun mahasiswa. Untuk akun dosen/admin gunakan /api/users (admin only).",
        )
    existing = await db.execute(
        select(User).where((User.email == data.email.lower()) | (User.username == data.username))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username atau email sudah dipakai")

    user = User(
        id=str(uuid.uuid4()),
        username=data.username,
        email=data.email.lower(),
        hashed_password=get_password_hash(data.password),
        full_name=data.full_name,
        jabatan=data.jabatan,
        jurusan=data.jurusan,
        nip=data.nip,
        phone=data.phone,
        avatar_url=data.avatar_url,
        bio=data.bio,
        role="mahasiswa",
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user_to_response(user)


@router.post("", response_model=UserResponse)
async def create_user(
    data: UserCreate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    # Check duplicates
    existing = await db.execute(
        select(User).where((User.email == data.email.lower()) | (User.username == data.username))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username atau email sudah dipakai")

    user = User(
        id=str(uuid.uuid4()),
        username=data.username,
        email=data.email.lower(),
        hashed_password=get_password_hash(data.password),
        full_name=data.full_name,
        jabatan=data.jabatan,
        jurusan=data.jurusan,
        nip=data.nip,
        phone=data.phone,
        avatar_url=data.avatar_url,
        bio=data.bio,
        role=data.role,
        is_active=data.is_active,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user_to_response(user)


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    data: UserUpdate,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin),
):
    update_data = {k: v for k, v in data.model_dump().items() if v is not None}
    # Hash password separately if provided
    if "password" in update_data:
        update_data["hashed_password"] = get_password_hash(update_data.pop("password"))
    if "email" in update_data:
        update_data["email"] = update_data["email"].lower()
    if not update_data:
        raise HTTPException(status_code=400, detail="Tidak ada field yang diubah")

    result = await db.execute(update(User).where(User.id == user_id).values(**update_data))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")
    # Round 26 audit (P0 #4): if the admin rotated the target user's password,
    # revoke that user's outstanding refresh tokens. Same rationale as
    # change_my_password above — a compromised session should not survive
    # a password reset.
    if "hashed_password" in update_data:
        await db.execute(delete(Token).where(Token.user_id == user_id))
    await db.commit()

    refreshed = await db.execute(select(User).where(User.id == user_id))
    user = refreshed.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")
    return user_to_response(user)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
):
    # Prevent self-delete to avoid locking out the admin
    if admin.get("sub") == user_id:
        raise HTTPException(status_code=400, detail="Tidak dapat menghapus akun sendiri")
    result = await db.execute(delete(User).where(User.id == user_id))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")
    await db.commit()
    return None
