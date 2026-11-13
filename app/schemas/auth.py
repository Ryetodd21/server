from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


def _validate_password_strength(v: str) -> str:
    """Shared password policy — applies to all password-accepting endpoints.

    Without this on UserCreate/UserUpdate, admin endpoints accepted 1-char
    passwords, bypassing the 8-char minimum enforced on PasswordChange.
    Now every password write — self-service, admin-create, admin-update —
    goes through the same gate.
    """
    if len(v) < 8:
        raise ValueError("Password minimal 8 karakter")
    if len(v) > 128:
        raise ValueError("Password maksimal 128 karakter")
    return v


class TokenResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LoginResponse(BaseModel):
    """Response returned by POST /api/auth/login — aligned with Android LoginResponse DTO."""
    model_config = ConfigDict(from_attributes=True)
    access_token: str
    refresh_token: str
    expires_in: int  # seconds until access_token expiry
    student_id: str  # user's UUID (used as student_id in Android)
    name: str  # user's full_name
    email: str


class LoginRequest(BaseModel):
    email: str | None = None
    password: str
    # Android app sends NIM (student ID number) instead of email.
    # Backend accepts either field — handler tries email first, then username.
    nim: str | None = None
    # Client-computed device fingerprint (sha256 of UA + screen + locale).
    # Optional — if absent, the server still creates a LoginDevice row keyed
    # only by user_agent, but the row won't dedupe across browsers.
    device_id: str | None = None
    # Parsed friendly device name — falls back to user_agent on the server.
    device_name: str | None = None
    # Raw UA, kept so the UI can render "Chrome 124 / Windows 11".
    user_agent: str | None = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Alamat email tidak valid")
        return v

    @field_validator("nim")
    @classmethod
    def validate_nim(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if len(v) < 3:
            raise ValueError("NIM minimal 3 karakter")
        return v


class RefreshRequest(BaseModel):
    # Optional — the server now reads the refresh token from the HttpOnly
    # cookie set at login. The body field is accepted for backwards compat
    # with any client that sends it.
    refresh_token: str | None = None


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    username: str
    email: str
    full_name: str = ""
    jabatan: str = ""
    jurusan: str = ""
    nip: str = ""
    phone: str = ""
    avatar_url: str = ""
    bio: str = ""
    role: str
    is_active: bool
    last_login: str | None = None
    created_at: str | None = None


class UserCreate(BaseModel):
    username: str
    email: str
    password: str
    full_name: str = ""
    jabatan: str = ""
    jurusan: str = ""
    nip: str = ""
    phone: str = ""
    avatar_url: str = ""
    bio: str = ""
    role: str = "mahasiswa"
    is_active: bool = True

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Alamat email tidak valid")
        return v.lower()

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return _validate_password_strength(v)


class UserUpdate(BaseModel):
    username: str | None = None
    email: str | None = None
    password: str | None = None
    full_name: str | None = None
    jabatan: str | None = None
    jurusan: str | None = None
    nip: str | None = None
    phone: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    role: str | None = None
    is_active: bool | None = None

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_password_strength(v)


class ProfileUpdate(BaseModel):
    """User can edit own profile (limited fields)."""
    full_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    # Round 29 audit P1 #3: bound avatar_url. Frontend caps at 1MB
    # base64 (~1.4MB raw data URL), but the server had no upper limit
    # so a user (or attacker with a stolen account) could PUT a 100MB
    # string. base64 inflates ~33%, so 2_000_000 chars ≈ 1.5MB binary —
    # comfortable headroom for the 1MB frontend cap.
    avatar_url: str | None = Field(default=None, max_length=2_000_000)
    bio: str | None = Field(default=None, max_length=1000)
    email: str | None = Field(default=None, max_length=200)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        return _validate_password_strength(v)


class LoginDeviceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    device_id: str
    device_name: str = ""
    user_agent: str = ""
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    is_current: bool = False
