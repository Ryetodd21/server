"""
AI Chat Router — Groq-powered cluster analysis assistant.
Accessible by admin and dosen only.

KEY POOL: server-side 4-key pool, round-robin + per-key cooldown + 429 auto-fallback.
Operator sets GROQ_API_KEY_1/2/3/4 in .env.production. User tidak input key.

LOAD BALANCING (round 16):
- Round-robin across keys, but skip keys currently in cooldown.
- After 429, mark the failing key with a 60s cooldown (Groq free tier rate
  limit window). Round-robin still advances the index so we don't repeat the
  same key, but cooled-down keys are skipped until their TTL expires.
- After a successful request, give the key a 30s "warm" window. Subsequent
  requests prefer warm keys (sticky-first-key) before falling back to cold ones
  — reduces redundant cold-path auth checks if all keys share the same IP/reg.
- Stagger initial round-robin position across workers using monotonic time so
  concurrent processes don't all hammer key #1 at the same instant.

TOKEN BUDGET STRATEGY:
- System prompt: <600 chars (~150 tokens)
- Context: model summary only, top-20 high-distraction students, compact stats
- max_tokens response: 2048 (bukan 4096) — cukup untuk jawaban terstruktur
- temperature: 0.3 (lebih deterministik, kurang hallucination)
"""
import asyncio
import json
import logging
import time
import httpx
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, func, cast
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, AwareDateTime
from app.limiter import limiter
from app.config import get_settings
from app.timezone import WITA
from app.models.model_version import ModelVersion
from app.models.cluster_result import ClusterResult
from app.models.student import Student
from app.models.app_log import AppLog
from app.routers.auth import get_current_user

logger = logging.getLogger("monitor.ai")

router = APIRouter(prefix="/ai", tags=["AI"])


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"] = "user"
    # Round 29 audit P1 #1: bound content length to prevent DoS via
    # a single huge message. Groq's llama-3.3-70b-versatile has a
    # 32K context window, but a 4000-char message is plenty for
    # conversational AI and bounds memory + per-request cost. Larger
    # prompts should be chunked client-side or use a different model.
    content: str = Field(..., max_length=4000)


class ChatRequest(BaseModel):
    # Round 29 audit P1 #1: bound the message list. 50 messages is
    # already a long conversation; more is suspicious.
    messages: list[ChatMessage] = Field(default_factory=list, max_length=50)
    # Model opsional. Server default ke openai/gpt-oss-120b jika tidak diisi.
    # Pool key 3 dipakai round-robin di server; client tidak perlu tau.
    # Round 29 audit P1 #1: bound model string to a reasonable length
    # (matches the actual Groq model ID format).
    model: str = Field(default="openai/gpt-oss-120b", max_length=100)

# Models that work for free-tier Groq API keys (verified active on Groq).
GROQ_MODELS = [
    "openai/gpt-oss-120b",   # default, best reasoning, free-tier
    "openai/gpt-oss-20b",    # faster, lighter
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"


# ─── Key Pool (round-robin + cooldown + sticky-on-success) ──────────────────
# Operator mengatur 1-4 Groq API key di env. Server round-robin per-request
# sehingga beban tersebar. Pada 429, otomatis coba key berikutnya dalam pool
# sampai habis. 401/timeout/5xx tidak di-rotate (config error / network issue).
#
# Per-key cooldown: setelah 429, key ditandai cooldown selama COOLDOWN_429_SECS
# (60s, default Groq free-tier rate-limit window). Round-robin akan melewati
# key tersebut sampai cooldown habis. Status cooldown dilihat via log + akhir
# log key (last-4 hex). Persisted sebagai dict suffix → expiry_monotonic.

_GROQ_KEYS: list[str] = []
# _key_suffix[i] = last-4 chars of _GROQ_KEYS[i], cached so cooldown map doesn't
# leak raw key bytes into logs. Stable across calls since _GROQ_KEYS is immutable.
_key_suffix: list[str] = []
_key_index: int = 0
_key_index_lock = asyncio.Lock()

# Per-key cooldown / warm windows. Maps suffix (str, 4 chars) → monotonic expiry.
# Cooldown set on 429; warm set on success. Both default-clear via TTL on read.
_cooldown_until: dict[str, float] = {}
_warm_until: dict[str, float] = {}
COOLDOWN_429_SECS = 60.0   # Groq free-tier rate-limit window
WARM_AFTER_SUCCESS_SECS = 30.0


def _load_groq_keys() -> list[str]:
    """Read Groq API keys from environment. Filter out empty strings."""
    settings = get_settings()
    keys = [
        settings.GROQ_API_KEY_1.strip(),
        settings.GROQ_API_KEY_2.strip(),
        settings.GROQ_API_KEY_3.strip(),
        settings.GROQ_API_KEY_4.strip(),
    ]
    return [k for k in keys if k]


def _init_key_pool() -> None:
    """Load keys at module import. Called once per worker process."""
    global _GROQ_KEYS, _key_suffix, _key_index
    _GROQ_KEYS = _load_groq_keys()
    _key_suffix = [k[-4:] if len(k) >= 4 else k for k in _GROQ_KEYS]
    # Stagger starting index across workers so concurrent processes don't all
    # pick the same key on the first request. Uses time.monotonic modulo pool
    # size — non-cryptographic, just a load-spreader.
    if _GROQ_KEYS:
        _key_index = int(time.monotonic() * 1000) % len(_GROQ_KEYS)
        logger.info(
            f"[ai] loaded {len(_GROQ_KEYS)} Groq API key(s) for round-robin pool, "
            f"start_index={_key_index}"
        )
    else:
        logger.warning(
            "[ai] no GROQ_API_KEY_1/2/3/4 set. AI Analisis will return 503 until "
            "operator configures at least one key in .env.production"
        )


_init_key_pool()


def _is_in_cooldown(suffix: str) -> bool:
    """Return True if the key is currently rate-limited."""
    expiry = _cooldown_until.get(suffix)
    if expiry is None:
        return False
    if time.monotonic() >= expiry:
        # Lazily drop expired entry so the dict doesn't grow forever.
        _cooldown_until.pop(suffix, None)
        return False
    return True


def _is_warm(suffix: str) -> bool:
    """Return True if the key was used successfully within the warm window."""
    expiry = _warm_until.get(suffix)
    if expiry is None:
        return False
    if time.monotonic() >= expiry:
        _warm_until.pop(suffix, None)
        return False
    return True


async def _next_key() -> str:
    """Atomically pick next key via round-robin, skipping cooled-down keys.

    Strategy:
    1. Walk forward from current index, N steps (N = pool size).
    2. For each slot, prefer a warm key; else accept any non-cooled key.
    3. If all keys are cooled down, return the round-robin slot anyway —
       better to retry than 503, since cooldown expires within ~60s.
    4. Advance the index unconditionally so cooldown eviction doesn't bias
       the distribution back to key #0.
    """
    global _key_index
    async with _key_index_lock:
        n = len(_GROQ_KEYS)
        if n == 0:
            return ""
        # Two-pass scan: warm key first, then any non-cooled key.
        start = _key_index
        warm_slot = None
        cold_slot = None
        for offset in range(n):
            i = (start + offset) % n
            suf = _key_suffix[i]
            if _is_in_cooldown(suf):
                continue
            if _is_warm(suf) and warm_slot is None:
                warm_slot = i
                break  # Best candidate found, no need to keep scanning.
            if cold_slot is None:
                cold_slot = i
        chosen = warm_slot if warm_slot is not None else cold_slot
        if chosen is None:
            # All cooled down — pick the next slot in round-robin order so we
            # still spread load, and let Groq decide if the limit is reset.
            chosen = start % n
        _key_index = (chosen + 1) % n
        return _GROQ_KEYS[chosen]


def _mark_cooldown(api_key: str) -> None:
    """Mark a key as rate-limited for COOLDOWN_429_SECS."""
    suf = api_key[-4:] if len(api_key) >= 4 else api_key
    _cooldown_until[suf] = time.monotonic() + COOLDOWN_429_SECS
    _warm_until.pop(suf, None)  # a cooled key is no longer warm


def _mark_warm(api_key: str) -> None:
    """Mark a key as recently-successful for WARM_AFTER_SUCCESS_SECS."""
    suf = api_key[-4:] if len(api_key) >= 4 else api_key
    _warm_until[suf] = time.monotonic() + WARM_AFTER_SUCCESS_SECS


class ChatResponse(BaseModel):
    message: str
    model: str
    usage: dict | None = None


async def require_admin_or_dosen(request: Request) -> dict:
    payload = await get_current_user(request)
    role = payload.get("role", "")
    if role not in ("admin", "dosen"):
        raise HTTPException(status_code=403, detail="Akses ditolak. Hanya admin dan dosen.")
    return payload


def _parse_json(value, default=None):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


# ─── Token Budget Constants ────────────────────────────────────────────────────
# Prinsip: kirim data AGRESIF DIKOMPRES ke model.
# Alih-alih kirim 200 student rows → kirim top-10 high + top-5 low distraction saja.
# Model lebih baik menganalisis insight dari ringkasan daripada menghafal data.

TOP_HIGH = 10   # mahasiswa distraction tertinggi
TOP_LOW = 5      # mahasiswa distraction terendah


# ─── Context Builder ─────────────────────────────────────────────────────────

async def _build_cluster_context(db: AsyncSession) -> dict:
    """
    Build COMPACT cluster context for the AI.

    Strategy:
    - Model metadata: FULL (compact field names)
    - Student data: TOP-N only (high + low distraction), sorted
    - Stats: compact
    Result: ~3KB JSON max vs ~20KB kalau kirim semua student rows.
    """
    # ── Active model ──────────────────────────────────────────────────────────
    result = await db.execute(
        select(ModelVersion).where(ModelVersion.is_active == True).limit(1)
    )
    active_model = result.scalar_one_or_none()

    if active_model:
        dist = _parse_json(active_model.cluster_distribution, {})
        lbls = _parse_json(active_model.cluster_labels, {})
        model_info = {
            "v": active_model.version_number,
            "s": active_model.status,
            "k": active_model.kmeans_n_clusters,
            "pca": active_model.pca_n_components,
            "sil": round(active_model.silhouette_score, 3) if active_model.silhouette_score else None,
            "ds": active_model.dataset_size,
            "trained_at": active_model.completed_at.isoformat() if active_model.completed_at else None,
            "inertia": round(active_model.inertia, 3) if active_model.inertia else None,
            "dist": {str(k): v for k, v in dist.items()},
            "lbl": {str(k): v for k, v in lbls.items()},
        }
        active_model_id = active_model.id
    else:
        model_info = {"v": None, "s": "no_model"}
        active_model_id = None

    # ── Student cluster data — top-N only ────────────────────────────────────
    high_distract = []
    low_distract = []
    cluster_summary = {}
    program_summary = {}

    if active_model_id:
        # Cast `max_date` to AwareDateTime so the equality with
        # ClusterResult.result_date (also AwareDateTime) compares tz-aware on
        # both sides. Without this, `func.max()` on a tz-aware column returns
        # a naive label and asyncpg raises `DataError: can't subtract
        # offset-naive and offset-aware datetimes` when PostgreSQL binds
        # both parameters — confirmed in docker logs.
        subq = (
            select(
                ClusterResult.student_id,
                cast(func.max(ClusterResult.result_date), AwareDateTime).label("max_date"),
            )
            .where(ClusterResult.model_version_id == active_model_id)
            .group_by(ClusterResult.student_id)
            .subquery()
        )
        result = await db.execute(
            select(ClusterResult, Student)
            .join(Student, ClusterResult.student_id == Student.id)
            .join(
                subq,
                (ClusterResult.student_id == subq.c.student_id)
                & (ClusterResult.result_date == subq.c.max_date),
            )
            .where(ClusterResult.model_version_id == active_model_id)
            .order_by(ClusterResult.distraction_score.desc())
        )
        rows = result.all()

        for cr, student in rows:
            label = str(cr.cluster_label)
            cluster_summary[label] = cluster_summary.get(label, 0) + 1
            entry = {
                "n": student.nim,
                "m": student.full_name,
                "c": cr.cluster_label,
                "ds": round(cr.distraction_score, 2) if cr.distraction_score else 0,
            }

            if len(high_distract) < TOP_HIGH:
                high_distract.append(entry)
            elif len(low_distract) < TOP_LOW:
                low_distract.append(entry)

            # Program studi summary — count per cluster
            prog = student.program_studi or "(tidak diketahui)"
            if prog not in program_summary:
                program_summary[prog] = {}
            program_summary[prog][label] = program_summary[prog].get(label, 0) + 1

        students_clustered = len(rows)
    else:
        students_clustered = 0

    # ── Week stats (compact) ────────────────────────────────────────────────
    today = datetime.now(WITA).date()
    wita = WITA
    week_ago = today - timedelta(days=7)
    prev_week_ago = today - timedelta(days=14)

    week_start = datetime.combine(week_ago, datetime.min.time(), tzinfo=wita)
    week_end = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=wita)
    prev_week_start = datetime.combine(prev_week_ago, datetime.min.time(), tzinfo=wita)
    prev_week_end = datetime.combine(week_ago + timedelta(days=1), datetime.min.time(), tzinfo=wita)

    log_result = await db.execute(
        select(
            func.avg(AppLog.total_screen_time_minutes).label("avg_sot"),
            func.avg(AppLog.unlock_count).label("avg_unlock"),
            func.avg(AppLog.social_media_time_minutes).label("avg_sosmed"),
            func.avg(AppLog.game_time_minutes).label("avg_game"),
            func.count(AppLog.id).label("total_logs"),
        ).where(
            AppLog.timestamp >= week_start,
            AppLog.timestamp < week_end,
            AppLog.total_screen_time_minutes.isnot(None),
        )
    )
    log_row = log_result.one_or_none()
    week_stats = {
        "sot": round(float(log_row.avg_sot or 0), 1),
        "unlock": round(float(log_row.avg_unlock or 0), 1),
        "sosmed": round(float(log_row.avg_sosmed or 0), 1),
        "game": round(float(log_row.avg_game or 0), 1),
        "logs": log_row.total_logs or 0,
    }

    # Week-over-week delta — compare this week vs prior week
    prev_log_result = await db.execute(
        select(
            func.avg(AppLog.total_screen_time_minutes).label("avg_sot"),
            func.avg(AppLog.social_media_time_minutes).label("avg_sosmed"),
            func.avg(AppLog.game_time_minutes).label("avg_game"),
            func.count(AppLog.id).label("total_logs"),
        ).where(
            AppLog.timestamp >= prev_week_start,
            AppLog.timestamp < prev_week_end,
            AppLog.total_screen_time_minutes.isnot(None),
        )
    )
    prev_row = prev_log_result.one_or_none()
    prev_sot = round(float(prev_row.avg_sot or 0), 1)
    week_stats["prev_sot"] = prev_sot
    week_stats["sot_delta"] = (
        round(week_stats["sot"] - prev_sot, 1) if prev_sot > 0 else None
    )
    week_stats["prev_sosmed"] = round(float(prev_row.avg_sosmed or 0), 1)
    week_stats["sosmed_delta"] = (
        round(week_stats["sosmed"] - week_stats["prev_sosmed"], 1)
        if week_stats["prev_sosmed"] > 0 else None
    )
    week_stats["prev_game"] = round(float(prev_row.avg_game or 0), 1)
    week_stats["game_delta"] = (
        round(week_stats["game"] - week_stats["prev_game"], 1)
        if week_stats["prev_game"] > 0 else None
    )
    week_stats["logs_delta"] = (
        week_stats["logs"] - (prev_row.total_logs or 0)
        if prev_row else None
    )

    total_students_result = await db.execute(select(func.count(Student.id)))
    total_students = total_students_result.scalar() or 0

    return {
        "model": model_info,
        "total": total_students,
        "clustered": students_clustered,
        "week": week_stats,
        # top_high: distraction score tertinggi → perlu perhatian
        # top_low: distraction score terendah → mahasiswa berpotensi baik
        "top_high": high_distract,
        "top_low": low_distract,
        "cluster_summary": cluster_summary,
        "program_summary": program_summary,
    }


# ─── System Prompt — clear, structured, Indonesian ──────────────────────────
# Target: instruct Groq to give well-structured Markdown answers that cite
# specific data and end with actionable recommendations.

SYSTEM_PROMPT = """Anda adalah asisten AI untuk Monitor UNG (Monitoring Distraksi Belajar — Universitas Negeri Gorontalo).

TUJUAN: Bantu admin/dosen memahami hasil clustering mahasiswa, distribusi distraksi, dan memberikan insight + rekomendasi yang dapat ditindaklanjuti.

ATURAN JAWABAN:
1. WAJIB jawab dalam **Bahasa Indonesia** yang jelas dan mudah dipahami.
2. Gunakan **Markdown formatting** dengan rapi:
   - **Bold** untuk istilah penting, nama mahasiswa, atau nilai kunci
   - Bullet/numbered list untuk poin-poin dan rekomendasi
   - Heading (## / ###) untuk membagi bagian jawaban yang panjang
3. Selalu kutip data spesifik: NIM, nama, distraction_score, program_studi, nama cluster.
4. JANGAN mengarang data. Jika informasi tidak tersedia di context, jawab dengan jujur: "Tidak ada data X dalam konteks saat ini."
5. Akhiri dengan **rekomendasi konkret** yang dapat ditindaklanjuti oleh admin/dosen.
6. Jika pertanyaan tidak terkait data monitoring, jawab singkat dan arahkan kembali ke topik sistem.

FORMAT JAWABAN IDEAL:
- Ringkasan 1-2 kalimat
- Data spesifik yang relevan (tabel atau list)
- Analisis/insight
- Rekomendasi konkret (actionable steps)

DATA TERSEDIA (lihat JSON context di bawah):
- Model ML aktif: versi, silhouette score, dataset size, label cluster
- Distribusi cluster (jumlah mahasiswa per cluster)
- Top mahasiswa dengan distraction tertinggi & terendah
- Distribusi per program studi
- Statistik mingguan: screen time (sot), unlock count, sosmed, game + delta vs minggu lalu
- Riwayat training model (jika dipanggil via extended-context)
- Tren screen time 4 minggu + breakdown kategori (jika dipanggil via extended-context)

WAJIB rujuk field trio (sot, sosmed, game) dalam analisis dan rekomendasi."""


# ─── Routes ──────────────────────────────────────────────────────────────────

@router.post("/chat", response_model=ChatResponse)
@limiter.limit("20/minute")
async def chat(
    body: ChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin_or_dosen),
):
    """
    Chat with Groq AI using compact cluster data as context.

    Server-side 4-key pool (round-robin) with 429 auto-fallback. Client tidak
    mengirim API key, pool ada di server. Context aggressively compressed:
    top-10 + bottom-5 students only.
    """
    if not _GROQ_KEYS:
        raise HTTPException(
            status_code=503,
            detail="Server belum dikonfigurasi dengan Groq API key. "
                   "Hubungi administrator untuk menambahkan GROQ_API_KEY_1/2/3/4 di .env.production.",
        )

    if body.model not in GROQ_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Model '{body.model}' tidak didukung. Pilihan: {', '.join(GROQ_MODELS)}",
        )

    ctx = await _build_cluster_context(db)
    ctx_json = json.dumps(ctx, ensure_ascii=False)

    system_msg = f"""{SYSTEM_PROMPT}

CONTEXT:
```json
{ctx_json}
```

Legenda: v=version, k=clusters, sot=screen-on-time(menit),
sosmed=sosmed(menit), game=game(menit),
ds=distraction_score(0-1, tinggi=buruk),
n=NIM, m=nama, c=cluster_label, clustered=mahasiswa dg hasil clustering.
Delta menunjukkan perubahan vs minggu lalu (positif=naik, negatif=turun).
sot_delta, sosmed_delta, game_delta dalam menit.
"""

    groq_messages = [{"role": "system", "content": system_msg}]
    for msg in body.messages:
        groq_messages.append({"role": msg.role, "content": msg.content})

    # ── Round-robin across key pool, 429 auto-fallback ──────────────────────
    last_429 = False
    for attempt in range(len(_GROQ_KEYS)):
        api_key = await _next_key()
        logger.info(
            f"[ai/chat] attempt {attempt + 1}/{len(_GROQ_KEYS)} using key ending ...{api_key[-4:]}"
        )

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
                groq_response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": body.model,
                        "messages": groq_messages,
                        "temperature": 0.3,
                        "max_tokens": 2048,
                    },
                )
        except httpx.TimeoutException:
            logger.warning("[ai/chat] Groq API timeout")
            raise HTTPException(status_code=504, detail="Groq API timeout.")
        except httpx.RequestError as exc:
            logger.warning(f"[ai/chat] Groq connection error: {exc}")
            raise HTTPException(status_code=502, detail="Tidak dapat terhubung ke Groq.")

        # 200, sukses. Mark key warm so the next request in this window prefers
        # the same key (sticky-success) — but keep round-robin index advancing
        # so we don't get stuck on a key if a different worker takes over.
        if groq_response.status_code == 200:
            _mark_warm(api_key)
            data = groq_response.json()
            return ChatResponse(
                message=data["choices"][0]["message"]["content"],
                model=data.get("model", body.model),
                usage=data.get("usage"),
            )

        # 429, rate limit. Mark key cooldown so subsequent requests skip it
        # for COOLDOWN_429_SECS instead of hammering it again immediately.
        # Fallback ke key berikutnya dalam pool.
        if groq_response.status_code == 429:
            _mark_cooldown(api_key)
            logger.warning(
                f"[ai/chat] key ...{api_key[-4:]} hit rate limit (429), "
                f"cooldown {COOLDOWN_429_SECS:.0f}s, falling back to next key"
            )
            last_429 = True
            continue

        # 401, konfigurasi key salah. Jangan rotate (akan loop 401 terus).
        if groq_response.status_code == 401:
            logger.error(
                f"[ai/chat] Groq API key ...{api_key[-4:]} tidak valid (401). "
                f"Periksa GROQ_API_KEY_1/2/3/4 di .env.production"
            )
            raise HTTPException(
                status_code=502,
                detail="Konfigurasi Groq API key tidak valid. Hubungi administrator.",
            )

        # 5xx atau error lain dari Groq, log dan return apa adanya.
        if groq_response.status_code >= 500:
            # Round 29 audit P1 #2: don't echo Groq's raw error body
            # to the client. Log internally; return a generic message.
            # Round 26 fix #44 covered the same pattern in routers/seed.py,
            # routers/models.py, routers/logs.py, routers/status.py — this
            # router was missed.
            logger.error(
                f"[ai/chat] Groq error {groq_response.status_code} on key ...{api_key[-4:]}: "
                f"{groq_response.text[:300]}"
            )
            raise HTTPException(
                status_code=502,
                detail="Layanan AI tidak dapat merespons saat ini. Coba lagi nanti.",
            )

        # 4xx lain (400/403/404), biasanya model error atau quota habis permanen.
        # Round 29 audit P1 #2: same redaction — log full, return generic.
        logger.error(
            f"[ai/chat] Groq {groq_response.status_code} on key ...{api_key[-4:]}: "
            f"{groq_response.text[:300]}"
        )
        raise HTTPException(
            status_code=400 if groq_response.status_code == 400 else 502,
            detail="Permintaan AI tidak valid atau layanan tidak tersedia.",
        )

    # Semua key di pool kena 429
    if last_429:
        raise HTTPException(
            status_code=429,
            detail="Layanan AI sedang sibuk. Semua Groq API key mencapai rate limit. "
                   "Coba lagi dalam 1 menit.",
        )

    # Fallback unreachable (seharusnya tidak terjadi, tapi defensive)
    raise HTTPException(status_code=502, detail="Tidak dapat terhubung ke Groq.")


@router.get("/models")
async def list_groq_models(
    request: Request,
    _: dict = Depends(require_admin_or_dosen),
):
    """List Groq models available to the application."""
    return {
        "models": GROQ_MODELS,
        "default": DEFAULT_GROQ_MODEL,
    }


@router.get("/pool")
async def get_key_pool_status(
    request: Request,
    _: dict = Depends(require_admin_or_dosen),
):
    """Inspect the Groq key pool: total keys, which are cooled down, which are warm.

    Useful for ops to verify load balancing when a user reports "AI lambat".
    Returns last-4 of each key (suffix) so admins can correlate with .env.
    """
    now = time.monotonic()
    keys = []
    for suf in _key_suffix:
        cooldown_expiry = _cooldown_until.get(suf)
        warm_expiry = _warm_until.get(suf)
        keys.append({
            "suffix": suf,
            "in_cooldown": cooldown_expiry is not None and cooldown_expiry > now,
            "cooldown_remaining_secs": round(cooldown_expiry - now, 1)
                if cooldown_expiry is not None and cooldown_expiry > now else 0,
            "warm": warm_expiry is not None and warm_expiry > now,
            "warm_remaining_secs": round(warm_expiry - now, 1)
                if warm_expiry is not None and warm_expiry > now else 0,
        })
    return {
        "pool_size": len(_GROQ_KEYS),
        "next_index": _key_index,
        "cooldown_429_secs": COOLDOWN_429_SECS,
        "warm_after_success_secs": WARM_AFTER_SUCCESS_SECS,
        "keys": keys,
    }


@router.get("/context")
async def get_cluster_context(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin_or_dosen),
):
    """Get the raw cluster context data (for display/debugging)."""
    return await _build_cluster_context(db)


# ─── Extended Context — Read-only aggregate analytics ────────────────────────
# Data ini hanya dibaca, TIDAK memodifikasi tabel apapun.
# Dipakai oleh AI Analisis untuk menjawab pertanyaan tentang tren, distribusi
# kategori, dan kesehatan model ML.

@router.get("/extended-context")
async def get_extended_context(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin_or_dosen),
):
    """
    Aggregate analytics yang lebih kaya dari /context biasa. Read-only.

    Sections returned:
    - model_history: 5 model training terakhir (versi, tanggal, silhouette, ds)
    - per_program: distribusi cluster per program_studi
    - weekly_trends: 4 minggu terakhir screen time + logs count
    - monthly_trend: perbandingan minggu ini vs bulan lalu
    - category_breakdown: total menit social/game/productive dalam 30 hari
    - unlock_distribution: distribusi unlock_count dalam 30 hari (bucket)
    - active_students: mahasiswa yang punya log dalam 7 hari vs 30 hari
    - model_health: silhouette, inertia, dataset_size, training_date model aktif
    """
    wita = WITA
    today = datetime.now(WITA).date()
    today_dt = datetime.combine(today, datetime.min.time(), tzinfo=wita)

    # ── 1. Model history (last 5 trainings) ────────────────────────────────
    history_q = await db.execute(
        select(ModelVersion)
        .order_by(ModelVersion.version_number.desc())
        .limit(5)
    )
    history_rows = history_q.scalars().all()
    model_history = [
        {
            "v": mv.version_number,
            "s": mv.status,
            "k": mv.kmeans_n_clusters,
            "pca": mv.pca_n_components,
            "sil": round(mv.silhouette_score, 3) if mv.silhouette_score else None,
            "ds": mv.dataset_size,
            "inertia": round(mv.inertia, 3) if mv.inertia else None,
            "trained_at": mv.completed_at.isoformat() if mv.completed_at else None,
            "is_active": mv.is_active,
        }
        for mv in history_rows
    ]

    # ── 2. Per-program cluster distribution ────────────────────────────────
    active_model_q = await db.execute(
        select(ModelVersion).where(ModelVersion.is_active == True).limit(1)
    )
    active_model = active_model_q.scalar_one_or_none()

    per_program = {}
    if active_model:
        # Same subquery pattern as _build_cluster_context — cast max_date to
        # AwareDateTime so equality on ClusterResult.result_date (also aware)
        # doesn't crash on PostgreSQL/asyncpg.
        subq = (
            select(
                ClusterResult.student_id,
                cast(func.max(ClusterResult.result_date), AwareDateTime).label("max_date"),
            )
            .where(ClusterResult.model_version_id == active_model.id)
            .group_by(ClusterResult.student_id)
            .subquery()
        )
        join_q = await db.execute(
            select(ClusterResult.cluster_label, Student.program_studi)
            .join(Student, ClusterResult.student_id == Student.id)
            .join(
                subq,
                (ClusterResult.student_id == subq.c.student_id)
                & (ClusterResult.result_date == subq.c.max_date),
            )
            .where(ClusterResult.model_version_id == active_model.id)
        )
        per_program_rows = join_q.all()
        for cluster_label, prog in per_program_rows:
            prog_key = prog or "(tidak diketahui)"
            if prog_key not in per_program:
                per_program[prog_key] = {}
            label_key = str(cluster_label)
            per_program[prog_key][label_key] = (
                per_program[prog_key].get(label_key, 0) + 1
            )

    # ── 3. Weekly trends — 4 minggu terakhir (screen time & logs) ─────────
    weekly_trends = []
    for weeks_ago in range(3, -1, -1):
        week_start_date = today - timedelta(days=7 * (weeks_ago + 1))
        week_end_date = today - timedelta(days=7 * weeks_ago)
        w_start = datetime.combine(week_start_date, datetime.min.time(), tzinfo=wita)
        w_end = datetime.combine(week_end_date, datetime.min.time(), tzinfo=wita)

        agg_q = await db.execute(
            select(
                func.avg(AppLog.total_screen_time_minutes).label("avg_sot"),
                func.avg(AppLog.unlock_count).label("avg_unlock"),
                func.count(AppLog.id).label("logs"),
            ).where(
                AppLog.timestamp >= w_start,
                AppLog.timestamp < w_end,
                AppLog.total_screen_time_minutes.isnot(None),
            )
        )
        agg_row = agg_q.one_or_none()
        weekly_trends.append({
            "week_start": week_start_date.isoformat(),
            "week_end": week_end_date.isoformat(),
            "avg_sot": round(float(agg_row.avg_sot or 0), 1) if agg_row else 0,
            "avg_unlock": round(float(agg_row.avg_unlock or 0), 1) if agg_row else 0,
            "logs": agg_row.logs or 0,
        })

    # ── 4. Monthly trend (last 30 days vs prior 30 days) ──────────────────
    last_30_start = datetime.combine(today - timedelta(days=30), datetime.min.time(), tzinfo=wita)
    last_30_end = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=wita)
    prior_30_start = datetime.combine(today - timedelta(days=60), datetime.min.time(), tzinfo=wita)
    prior_30_end = last_30_start

    async def _aggregate(start, end):
        # await db.execute(...) returns an async Result; call .one_or_none()
        # on the Result, not on the coroutine. Don't combine await with chained
        # attribute access — Python parses `await db.execute(...).x()` as
        # `await (db.execute(...).x())`, where `.x()` is sync on the coroutine.
        result = await db.execute(
            select(
                func.avg(AppLog.total_screen_time_minutes).label("avg_sot"),
                func.count(AppLog.id).label("logs"),
            ).where(
                AppLog.timestamp >= start,
                AppLog.timestamp < end,
                AppLog.total_screen_time_minutes.isnot(None),
            )
        )
        return result.one_or_none()

    last_30 = await _aggregate(last_30_start, last_30_end)
    prior_30 = await _aggregate(prior_30_start, prior_30_end)
    last_30_sot = round(float(last_30.avg_sot or 0), 1) if last_30 else 0
    prior_30_sot = round(float(prior_30.avg_sot or 0), 1) if prior_30 else 0
    monthly_trend = {
        "last_30_days": {
            "avg_sot": last_30_sot,
            "logs": last_30.logs or 0 if last_30 else 0,
        },
        "prior_30_days": {
            "avg_sot": prior_30_sot,
            "logs": prior_30.logs or 0 if prior_30 else 0,
        },
        "sot_delta": round(last_30_sot - prior_30_sot, 1) if prior_30_sot > 0 else None,
        "logs_delta": (
            (last_30.logs or 0) - (prior_30.logs or 0)
            if last_30 and prior_30 else None
        ),
    }

    # ── 5. Category breakdown (last 30 days) ──────────────────────────────
    cat_q = await db.execute(
        select(
            func.coalesce(func.sum(AppLog.social_media_time_minutes), 0).label("social"),
            func.coalesce(func.sum(AppLog.game_time_minutes), 0).label("game"),
            func.coalesce(func.sum(AppLog.other_app_time_minutes), 0).label("other"),
            func.coalesce(func.sum(AppLog.total_screen_time_minutes), 0).label("total"),
        ).where(
            AppLog.timestamp >= last_30_start,
            AppLog.timestamp < last_30_end,
        )
    )
    cat_row = cat_q.one_or_none()
    total_minutes = float(cat_row.total or 0) if cat_row else 0
    category_breakdown = {
        "social_minutes": int(cat_row.social or 0) if cat_row else 0,
        "game_minutes": int(cat_row.game or 0) if cat_row else 0,
        "other_minutes": int(cat_row.other or 0) if cat_row else 0,
        "total_minutes": int(total_minutes),
    }
    if total_minutes > 0:
        category_breakdown["social_pct"] = round((cat_row.social or 0) / total_minutes * 100, 1)
        category_breakdown["game_pct"] = round((cat_row.game or 0) / total_minutes * 100, 1)
        category_breakdown["other_pct"] = round((cat_row.other or 0) / total_minutes * 100, 1)
    else:
        category_breakdown["social_pct"] = 0
        category_breakdown["game_pct"] = 0
        category_breakdown["other_pct"] = 0

    # ── 6. Unlock count distribution (last 30 days, bucketized) ───────────
    unlock_q = await db.execute(
        select(AppLog.unlock_count).where(
            AppLog.timestamp >= last_30_start,
            AppLog.timestamp < last_30_end,
            AppLog.unlock_count.isnot(None),
        )
    )
    unlock_values = [int(u[0]) for u in unlock_q.all() if u[0] is not None]
    unlock_buckets = {"0-9": 0, "10-24": 0, "25-49": 0, "50-99": 0, "100+": 0}
    for u in unlock_values:
        if u < 10:
            unlock_buckets["0-9"] += 1
        elif u < 25:
            unlock_buckets["10-24"] += 1
        elif u < 50:
            unlock_buckets["25-49"] += 1
        elif u < 100:
            unlock_buckets["50-99"] += 1
        else:
            unlock_buckets["100+"] += 1
    unlock_distribution = {
        "buckets": unlock_buckets,
        "samples": len(unlock_values),
        "avg": round(sum(unlock_values) / len(unlock_values), 1) if unlock_values else 0,
    }

    # ── 7. Active students (logged at least once) ─────────────────────────
    week_start = datetime.combine(today - timedelta(days=7), datetime.min.time(), tzinfo=wita)
    week_end = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=wita)

    active_week_q = await db.execute(
        select(func.count(func.distinct(AppLog.student_id))).where(
            AppLog.timestamp >= week_start,
            AppLog.timestamp < week_end,
        )
    )
    active_month_q = await db.execute(
        select(func.count(func.distinct(AppLog.student_id))).where(
            AppLog.timestamp >= last_30_start,
            AppLog.timestamp < last_30_end,
        )
    )
    total_q = await db.execute(select(func.count(Student.id)))

    active_students = {
        "active_this_week": active_week_q.scalar() or 0,
        "active_last_30_days": active_month_q.scalar() or 0,
        "total_registered": total_q.scalar() or 0,
    }
    if active_students["total_registered"] > 0:
        active_students["weekly_pct"] = round(
            active_students["active_this_week"] / active_students["total_registered"] * 100, 1
        )
        active_students["monthly_pct"] = round(
            active_students["active_last_30_days"] / active_students["total_registered"] * 100, 1
        )
    else:
        active_students["weekly_pct"] = 0
        active_students["monthly_pct"] = 0

    # ── 8. Model health (active model snapshot) ───────────────────────────
    model_health = None
    if active_model:
        model_health = {
            "version": active_model.version_number,
            "status": active_model.status,
            "silhouette_score": (
                round(active_model.silhouette_score, 3)
                if active_model.silhouette_score else None
            ),
            "inertia": (
                round(active_model.inertia, 3)
                if active_model.inertia else None
            ),
            "dataset_size": active_model.dataset_size,
            "train_size": active_model.train_size,
            "test_size": active_model.test_size,
            "k_clusters": active_model.kmeans_n_clusters,
            "pca_components": active_model.pca_n_components,
            "trained_at": (
                active_model.completed_at.isoformat()
                if active_model.completed_at else None
            ),
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_history": model_history,
        "per_program": per_program,
        "weekly_trends": weekly_trends,
        "monthly_trend": monthly_trend,
        "category_breakdown": category_breakdown,
        "unlock_distribution": unlock_distribution,
        "active_students": active_students,
        "model_health": model_health,
    }
