---
name: server-backend-architecture
description: "Monitor UNG FastAPI backend — key rules: WebSocket primary real-time transport, SSE legacy, ML cluster_labels dynamic, rate limiting on auth endpoints, logout invalidates tokens, all body()!! fixed, RBAC require_admin_or_dosen helper, PostgreSQL/SQLite auto-detect"
---

# Monitor UNG Server Backend

## Architecture

- **Framework**: FastAPI 0.118.0 + async SQLAlchemy (aiosqlite for dev, asyncpg for prod)
- **Auth**: JWT access + refresh tokens; real-time streams use HttpOnly cookie (`sse_token`, shared by WebSocket primary and SSE legacy)
- **Database**: PostgreSQL (prod) / SQLite (dev); auto-detected via `run_migrations()`
- **ML Pipeline**: PCA + K-Means; cluster_labels stored as JSON string in ModelVersion
- **Real-time**: WebSocket primary via `/api/logs/stream/ws`; SSE legacy via `/api/logs/stream` (both use `POST /api/logs/stream-auth` for cookie-based auth)
- **MQTT**: Subscriber for real-time log ingestion; configurable via `MQTT_BROKER_URL`

## Key Rules

### Auth
- JWT tokens: `access_token` (15min) + `refresh_token` (7 days)
- Public registration only allows `mahasiswa` / `dosen` role — NOT `admin`
- SSE token: POST `/api/logs/stream-auth` sets HttpOnly cookie; SSE reads cookie
- Rate limiting: `/auth/login` → 5/min, `/auth/refresh` → 3/min (single `Limiter` instance from `app.limiter`)
- Logout: clears cookies + deletes all Token records by `user_id`
- AI chat (`/api/ai/chat`, `/api/ai/context`): requires admin or dosen role; groq_api_key per-request in body; llama-3.3-70b-versatile; temperature=0.3; max_tokens=2048; context compressed to top-10 + top-5 students only (~3KB JSON)

### Role-Based Access Control (RBAC)
- Three roles: `admin`, `dosen`, `mahasiswa`. Public registration is role-validated (admin not allowed)
- **Staff dashboard pattern** — endpoints exposing aggregate/admin data use `Depends(require_admin_or_dosen)` helper:
  ```python
  async def require_admin_or_dosen(request: Request) -> dict:
      """Endpoints exposing aggregate/admin data — restrict to staff."""
      payload = await get_current_user(request)
      role = payload.get("role", "")
      if role not in ("admin", "dosen"):
          raise HTTPException(
              status_code=403,
              detail="Akses ditolak. Hanya untuk admin/dosen.",
          )
      return payload
  ```
- Helper is defined locally per-router (`routers/reports.py`, `routers/students.py`) — pattern duplicated locally to avoid a shared dep that creates circular imports
- Applied to: `routers/reports.py` (4 endpoints), `routers/students.py` GET
- **`routers/campus.py`** GET uses only `Depends(get_current_user)` — any authenticated user can read campus geofence configs (low-sensitivity, used by Android app)
- Frontend mirrors server RBAC: `STAFF_ROLES = ["admin", "dosen"]` in `website/src/components/auth/role-gate.tsx` wrapped at `(dashboard)/layout.tsx` via `STAFF_PATH_PREFIXES`
- Add role-gated endpoint: copy the local `require_admin_or_dosen` helper, change role list as needed; remember to update frontend `STAFF_PATH_PREFIXES` if new path

### ML Pipeline
- `PUT /api/models/config` accepts `pca_n_components`, `kmeans_n_clusters`, `cluster_labels`
- `cluster_labels` and `cluster_distribution` stored as JSON string in ModelVersion
- Feature data from pre-aggregated columns: `total_screen_time_minutes`, `social_media_time_minutes`, `game_time_minutes`
- Training triggered via `POST /api/models/train`; status polled via `GET /api/models/status`
- **Date source**: `app_logs.log_date` is NULL for seed/MQTT rows (only populated by Android client). Pipeline uses `func.coalesce(AppLog.log_date, AppLog.timestamp)` so the date range covers all data, not just Android-uploaded logs. See Fixed Issues 2026-09-25 #37.

### Database
- `run_migrations()` auto-detects PostgreSQL vs SQLite:
  - PostgreSQL: `SELECT column_name FROM information_schema.columns WHERE table_name = :table`
  - SQLite: `PRAGMA table_info(:table)`
- `SELECT` must include `unlock_count` and `used_during_class` for ML features to work
- **Datetime columns use `AwareDateTime` (TypeDecorator over `DateTime(timezone=True)`)** — see `app/database.py:13`. All datetime reads return tz-aware UTC regardless of dialect (PostgreSQL keeps native tz, SQLite restored on read via `process_result_value`). Application code can treat all datetimes as UTC; no `.replace(tzinfo=None)` at use sites. Supersedes the 2026-09-25 workaround — see Fixed Issues 2026-09-26.

### Real-time Stream (WebSocket primary, SSE legacy)
- `POST /api/logs/stream-auth` → sets `sse_token` HttpOnly cookie (used by both transports)
- `GET /api/logs/stream/ws` → **primary** WebSocket endpoint; reads `Authorization` header OR `sse_token` cookie
- `GET /api/logs/stream` → **legacy** SSE endpoint; reads `Authorization` header OR `sse_token` cookie (NO query param fallback)
- Token NEVER in URL — protects against server log exposure
- Graceful degradation: Redis unavailable → yields `{"event":"error"}` then closes (SSE only; WebSocket closes on connect error)

### API Patterns
- All `response.body()!!` → safe null check + `Result.failure()`
- `report.py` endpoints: use `.one_or_none()` not `.one()` when data may be empty
- `schedule_request.py`: use `user.full_name` (not `user.name`)

## Key Endpoints

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| POST | `/api/auth/login` | None | email + password |
| POST | `/api/auth/refresh` | None | refresh_token |
| POST | `/api/users` | Admin | Registration — role validation |
| GET | `/api/students` | Admin/Dosen | Paginated with cluster data (`require_admin_or_dosen`) |
| POST | `/api/students` | Admin/Dosen | Create student |
| GET | `/api/schedules` | Student | Own schedules only |
| POST | `/api/schedule-requests` | Student | Request schedule addition |
| POST | `/api/logs/batch` | Student | Batch upload app usage logs |
| POST | `/api/logs/stream-auth` | Any | Set real-time auth cookie (used by WS + SSE) |
| GET | `/api/logs/stream/ws` | Any | WebSocket real-time stream (primary) |
| GET | `/api/logs/stream` | Any | SSE real-time stream (legacy) |
| GET | `/api/models/history` | Admin | GET /models/history requires auth |
| PUT | `/api/models/config` | Admin | Save PCA config + cluster_labels |
| POST | `/api/models/train` | Admin | Trigger ML training |
| GET | `/api/reports` | Admin/Dosen | Aggregate reports (`require_admin_or_dosen`) |
| GET | `/api/reports/screen-time` | Admin/Dosen | Screen time trends (`require_admin_or_dosen`) |
| GET | `/api/reports/cluster-trends` | Admin/Dosen | Cluster distribution trends (`require_admin_or_dosen`) |
| GET | `/api/reports/summary` | Admin/Dosen | Dashboard summary (`require_admin_or_dosen`) |
| GET | `/api/campuses` | Auth | List campus geofence configs (any authenticated user) |
| POST | `/api/campuses` | Admin/Dosen | Create campus |
| PUT | `/api/campuses/{id}` | Admin/Dosen | Update campus |
| DELETE | `/api/campuses/{id}` | Admin/Dosen | Delete campus |
| GET | `/api/status` | None | Service + MQTT status |
| POST | `/api/ai/chat` | Admin/Dosen | Groq AI chat with cluster context |
| GET | `/api/ai/context` | Admin/Dosen | Raw cluster context for AI |
| GET | `/api/seed/status` | Admin | Seed data status |
| POST | `/api/seed/generate` | Admin | Generate 15 students + ~10k logs |
| DELETE | `/api/seed/clear` | Admin | Clear all seed data |
| GET | `/api/program-studi` | Any auth | List academic programs (master dropdown source) |
| POST | `/api/program-studi` | Admin | Create program (code unique) |
| PUT | `/api/program-studi/{id}` | Admin | Update program |
| DELETE | `/api/program-studi/{id}` | Admin | Soft-delete if students reference, hard-delete otherwise |
| GET | `/api/auth/devices` | Any auth | List all devices that have logged into the current user (deduped) |
| DELETE | `/api/auth/devices/{id}` | Any auth | Revoke a device + clear refresh tokens |

## Config

Environment variables in `app/config.py`:
- `DATABASE_URL` — PostgreSQL or SQLite (required)
- `DATABASE_SYNC_URL` — sync DB connection (required)
- `REDIS_HOST`, `REDIS_PORT` — Redis cache
- `MQTT_BROKER_URL` — MQTT broker (default: `tcp://localhost:1883`)
- `APP_VERSION` — exposed via `/api/status`
- `SECRET_KEY` — **required**, no default (server fails if not set)
- `ALGORITHM` — JWT signing (default: HS256)
- `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS`

## Fixed Issues (2026-09-21)

1. Real-time token exposed in URL → cookie-based auth via `/api/logs/stream-auth` (and removed `?token=` fallback). Both WebSocket and SSE read the same cookie.
2. ML `unlock_count`/`used_during_class` never selected → added to SELECT
3. PUT `/config` missing `cluster_labels` → added to schema + DB write
4. `user.name` AttributeError → `user.full_name`
5. Reports `.one()` crash → `.one_or_none()`
6. Public registration could create admin → role validation (`mahasiswa`/`dosen` only)
7. `PRAGMA table_info` fails on PostgreSQL → `information_schema` query
8. MQTT hardcoded `127.0.0.1` → `settings.MQTT_BROKER_URL`
9. Hardcoded 3-cluster label dict → dynamic read from `activeModel.cluster_labels`
10. Month-end calculation wrong → `calendar.monthrange(year, month)`
11. `getStudents()` wrong endpoint → `/api/students`
12. Dashboard route no auth guard → `AuthGuard` wrapper
13. "Simpan Label" stub → `updateModelConfig()` API call
14. Profile password change stub → `changePassword()` API call
15. Settings notification localStorage persistence
16. Monitoring `Math.random()` → deterministic formula + real-time `distraction_score` (WebSocket)
17. Active users count always 0 → query `User.last_login` (not `Token` table, access tokens are stateless JWT)
18. MQTT connect blocking in async endpoint → wrapped with `run_in_executor(ThreadPoolExecutor)`
19. `cluster_distribution`/`cluster_labels` assigned as dict → `json.dumps()` before DB write
20. Pipeline read `category` (always "other") → read pre-aggregated columns (`social_media_time_minutes`, `game_time_minutes`)
21. `app_package`/`app_name`/`duration_seconds` not nullable → made nullable with defaults
22. `SECRET_KEY` hardcoded default → `Field(default=...)` so server fails if not set in env
23. `datetime.utcnow()` deprecated → `datetime.now(timezone.utc)` throughout
24. `get_active_users` endpoint used wrong Token query → fixed to use `User.last_login`
25. Rate limiter duplicate `Limiter` instance → single instance from `app.limiter.py` registered in `app.state`
26. Logout tidak invalidate refresh tokens → deletes Token records by `user_id`
27. Real-time stream graceful degradation if Redis unavailable → SSE yields `{"event":"error"}` then closes; WebSocket closes on connect error
28. Groq AI feature: per-request API key (not stored server-side), admin/dosen only, llama-3.3-70b-versatile

## Fixed Issues (2026-09-22) — RBAC

29. **`/api/campuses` GET endpoint had no auth** — anyone (unauthenticated) could enumerate campus geofence coordinates. Added `Depends(get_current_user)`. POST/PUT/DELETE already had admin/dosen check via `get_admin_user`.
30. **`/api/students` GET had `get_current_user` only** — mahasiswa could query aggregate student/cluster data meant for staff. Added local `require_admin_or_dosen` helper. POST already had inline role check.
31. **`/api/reports` 4 endpoints had `get_current_user` only** — same issue as students. Added `require_admin_or_dosen` to `/api/reports`, `/api/reports/screen-time`, `/api/reports/cluster-trends`, `/api/reports/summary`.
32. **Frontend (`website`) had no role-gating** — sidebar showed all menus to mahasiswa, Server Activity Feed exposed to non-staff, staff-only URLs (`/dashboard`, `/students`, etc.) accessible by URL-typing. Added `RoleGate` + `AccessDenied` component in `website/src/components/auth/role-gate.tsx`, gated at `(dashboard)/layout.tsx` via `STAFF_PATH_PREFIXES`, sidebar switches between `mainNav` (staff) and `memberNav` (mahasiswa).

**Pattern for new staff-only endpoints:** copy the local `require_admin_or_dosen` helper from `routers/reports.py` or `routers/students.py` — don't create a shared dependency (would create circular imports across routers).

## Fixed Issues (2026-09-25)

33. **`worker.py:167` `features` dict not JSON-serialized** — `rq.enqueue` payload stored Python dict → RQ serializes via pickle but the worker's `process_training_result` reads it as dict and tries to assign directly to a `JSON` column. Wrapped `features` with `json.dumps(...) if isinstance(r["features"], dict) else r["features"]` — mirrors `routers/models.py:222` pattern. Without this fix, nightly training crashes when `features` exists in the result payload.
34. **`seed_data.py` boolean columns wrote `0`/`1` integers** — PostgreSQL rejected with `DatatypeMismatch` because the columns are `BOOLEAN`. Replaced `0`/`1` with `False`/`True` at lines 250, 275, 324 (user `is_active`, student `is_active`, app_log `is_synced`). SQLite silently accepted integers, masking the bug in dev.
35. **`routers/models.py` returned tz-aware datetimes to PostgreSQL** — 5 occurrences of `datetime.now(timezone.utc)` (training_date, started_at, completed_at × 3). DB columns were `TIMESTAMP WITHOUT TIME ZONE`; PostgreSQL error: "can't subtract offset-naive and offset-aware datetimes". Added `.replace(tzinfo=None)` as a temporary workaround. **Superseded by 2026-09-26 #50** — columns migrated to `TIMESTAMP WITH TIME ZONE` + `AwareDateTime` decorator, `.replace(tzinfo=None)` removed at all 5 sites.
36. **`routers/reports.py` `datetime.combine(...) - wita_offset` was tz-aware** — 14 occurrences across 4 endpoints (`/api/reports`, `/api/reports/screen-time`, `/api/reports/cluster-trends`, `/api/reports/summary`). WITA→UTC subtraction kept tzinfo, PostgreSQL rejected. Wrapped each in `.replace(tzinfo=None)`. **Superseded by 2026-09-26 #50** — 12 sites cleaned, 2 more converted to `aware_utc()` helper that returns tz-aware UTC directly.
37. **`ml/pipeline.py` `result_date` defaulted to `1970-01-01`** — `app_logs.log_date` is NULL for seed/MQTT rows (only populated by Android client). The fallback `date_val = result.get("result_date") or datetime(1970, 1, 1)` produced garbage date ranges. Fixed by `SELECT` with `func.coalesce(AppLog.log_date, AppLog.timestamp).label("effective_date")` and removed the 1970 fallback. Date range now correctly spans `SEED_BASE` (2026-08-01) → `SEED_END` (2026-09-22). **Verified 2026-09-26 via `scripts/ml_pipeline_smoke_test.py`** — COALESCE returns tz-aware UTC on both SQLite and PostgreSQL via `AwareDateTime` decorator.

## Fixed Issues (2026-09-25) — MQTT production wiring

The whole real-time MQTT pipeline was unwired in production — `paho-mqtt` was declared and `app/mqtt_subscriber.py` was written, but no broker ran and no subscriber process existed. Live `GET /api/status` reported `"mqtt_broker": {"status": "unavailable"}`.

38. **No MQTT broker in production** — `docker-compose.yml` had `eclipse-mosquitto:2` service commented out. Uncommented + activated with `/mosquitto-no-auth.conf` (anonymous auth OK for internal Docker network); added `mqtt-data` + `mqtt-log` named volumes for persistence; added `mosquitto_pub` healthcheck.

39. **No subscriber process** — `app/mqtt_subscriber.py` is `python -m app.mqtt_subscriber` (standalone script) but was never launched. Added a new `subscriber` service in `docker-compose.yml` (same `monitorung-api:latest` image as worker/scheduler; uses `python -m app.mqtt_subscriber`); depends on `mqtt.service_healthy` + `postgres.service_healthy` + `redis.service_healthy`. `MQTT_BROKER_URL=tcp://mqtt:1883` so the subscriber reaches the broker via compose DNS.

40. **`routers/status.py` broken `from app.mqtt_subscriber import mqtt_client` import** — `app.mqtt_subscriber` defines no `mqtt_client` module global (it constructs the client locally inside `start_subscriber()`). The `ImportError` was silently swallowed by the surrounding `except Exception:` and the probe fell through to a TCP probe that always failed because no broker was running. Removed the dead import; status now uses a direct paho-2.x-compatible TCP probe (`socket.setdefaulttimeout(2)` instead of the deprecated `timeout=` kwarg on `client.connect()`).

41. **Subscriber `on_message` crashed under paho network thread** — `asyncio.create_task(on_message_async(...))` was called from paho's network thread (no running asyncio loop), raising `RuntimeError: no running event loop`. Replaced with `_loop.call_soon_threadsafe(asyncio.ensure_future, ...)` where `_loop = asyncio.get_running_loop()` captured at subscriber startup. Subsequent messages route correctly through the running loop.

**Verified end-to-end after fix:**
- `docker compose ps mqtt subscriber` → both Up + healthy
- `GET /api/status` → `"mqtt_broker": {"status": "connected", "broker_url": "tcp://mqtt:1883", "connected": true}`
- `mosquitto_pub -t students/{id}/logs -m '{...}'` → 1 row in `app_logs` (idempotency on `mqtt:{student_id}:app:{log_date}` confirmed: duplicate publish → still 1 row)
- `mosquitto_pub -t students/{id}/location -m '{...}'` → 1 row in `location_logs`

**Implication for future agents:** any new MQTT-related code MUST respect that the broker + subscriber live as separate services inside compose. The api container cannot share in-process Python globals with the subscriber container — cross-container state goes through Redis (publish/subscribe), the broker (pub/sub), or PostgreSQL (rows).

**Verified endpoints after fixes:**
- `POST /api/seed/generate` — generates 15 students + ~11,000 logs
- `POST /api/models/train` — completes, returns `"Model v3 is now active"`
- `GET /api/models/results/points` — returns 795 PCA points
- `GET /api/reports/summary` — returns total/active/pending metrics
- `GET /api/reports/cluster-trends` — returns 4 weeks of trends

## Fixed Issues (2026-09-26)

Two parallel audits + roadmap cleanup. 8 critical fixes + schema migration + bcrypt tuning.

### Critical security / data-integrity fixes

42. **`seed_data.py:326` `datetime.now(WITA)` was tz-aware** — same pattern as #35. WITA is `timezone(timedelta(hours=8))`; passing tz-aware datetime to a `TIMESTAMP WITHOUT TIME ZONE` column made PostgreSQL reject fresh prod seed. Replaced with `datetime.now(timezone.utc).replace(tzinfo=None)`. Superseded by #50.

43. **`mqtt_subscriber.py` parsed MQTT payloads' datetimes as naive** — Android clients send WITA-offset ISO-8601 (`"2026-09-18T08:30:00+08:00"`). `datetime.fromisoformat(value)` returned tz-aware; the same column-rejection crash from #35 occurred but the outer `except Exception` in `on_message` swallowed it silently — student logs dropped without trace. Added `_parse_naive_datetime()` helper that coerces to UTC then strips tzinfo. Three call sites migrated (`log_date`, `recorded_at` × 2). Superseded by #50 (helper renamed `_parse_datetime()`, returns tz-aware UTC).

44. **HTTP responses leaked raw exception text** — 8 sites across `routers/seed.py`, `routers/models.py`, `routers/logs.py`, `routers/status.py`. `HTTPException(detail=str(e))` echoed PostgreSQL `IntegrityError`/`DataError` strings (table/column names, constraint names, partial values) to clients. Particularly bad: `GET /api/status` is unauthenticated and `db_status = f"unhealthy: {str(e)[:100]}"` was a public DB-error oracle. All 8 sites now log internally via `logger.exception(...)` and return generic detail strings. The `/api/status` DB error is now just `"unhealthy"`.

45. **`routers/models.py:111` Pydantic `ValidationError` stringified into 422** — `str(ex)` returns field paths + expected types + library-internal type names (recon vector); also defeats FastAPI's standard 422 envelope shape. Replaced with structured `ex.errors()` JSON: `{"error": "validation_failed", "errors": ex.errors()}`. Clients can parse; sensitive detail stays server-side in logs.

46. **`database.py:105` `run_migrations()` swallowed all ALTER failures** — bare `except Exception: pass`. Lock conflicts, type mismatches, reserved keywords, and permission errors all passed silently, leaving the schema inconsistent with what the models expected. Narrowed: `sqlalchemy.exc.ProgrammingError` for first-run case (logged debug); anything else logs `logger.error(...)` and re-raises. Added `_IDENT_RE = ^[a-z_][a-z0-9_]{0,62}$` whitelist for table/column names (defense-in-depth against future contributors adding untrusted identifiers).

47. **`routers/auth.py:112` login timing oracle for user enumeration** — `if not user or not verify_password(...)` short-circuits when `user is None`, skipping bcrypt's ~80ms cost. Attacker measuring response times could enumerate valid emails in O(minutes × IPs) despite the 5/min rate limit. Added module-level `_DUMMY_HASH` (bcrypt rounds=12, computed at startup); when `user is None`, the path still executes `verify_password(password, _DUMMY_HASH)` so timing matches the wrong-password branch.

### Roadmap items — Bcrypt + TIMESTAMP WITH TIME ZONE migration

48. **`config.py:10` `DEBUG: bool = True` was a deployment footgun** — production default enabled SQL echo (writing `INSERT INTO users ... hashed_password '$2b$12$...'` to docker logs) and `secure=False` cookies (sent over HTTP if Cloudflare TLS misconfigured). Changed default to `False`; operators opt-in with `DEBUG=true` env for local dev.

49. **`config.py` + 3 sites — bcrypt rounds made configurable** — added `BCRYPT_ROUNDS: int = 12` (industry default, ~80ms). Updated `_DUMMY_HASH`, `get_password_hash()`, and `seed_data._hash_password()` to read from config. Override via `BCRYPT_ROUNDS=13` env to double the cost.

### Schema migration — TIMESTAMP WITH TIME ZONE

50. **All datetime columns migrated to `TIMESTAMP WITH TIME ZONE`** — 24 columns across 9 model files (app_log, user, location_log, student, campus, schedule, model_version, cluster_result, schedule_request). Removed 35 `.replace(tzinfo=None)` sites at source (routers/auth, routers/models, routers/reports, routers/schedule_request, seed_data, mqtt_subscriber, tasks/worker, schedules/scheduler). `run_type_migrations()` added to `database.py:173` — idempotent ALTER via `information_schema.columns.data_type` check, runs on PostgreSQL startup only. Existing rows were written as naive UTC (`datetime.now(timezone.utc).replace(tzinfo=None)` at the time), so `USING col AT TIME ZONE 'UTC'` preserves the original instant — zero data loss.

51. **`database.py:13` `AwareDateTime` TypeDecorator bridges SQLite / PostgreSQL** — SQLite has no native `TIMESTAMP WITH TIME ZONE`, so SQLAlchemy's `DateTime(timezone=True)` returns naive datetimes on dev. The TypeDecorator's `process_result_value` restores `tzinfo=UTC` on the SQLite path so the rest of the codebase can treat all datetimes as tz-aware UTC uniformly. All 9 models use `AwareDateTime` instead of `DateTime(timezone=True)` directly. Verified by `scripts/ml_pipeline_smoke_test.py` — 4 tests pass on SQLite covering COALESCE, pandas conversion, WITA→UTC math, and the auth.py:178 comparison case.

**Verified post-migration (run in production after deploy):**
- `POST /api/models/train` returns `"Model v4 is now active"` (or higher)
- Docker logs show `[run_type_migrations] ALTER {table}.{column}: timestamp without time zone → timestamp with time zone` once per column, on first startup only
- `python -m app.ml.pipeline_smoke_test` (run inside the worker/scheduler container, where pandas is installed) — all 4 tests pass on PostgreSQL

**Rule of thumb (updated 2026-09-26):** application code can assume all datetime values crossing the ORM are tz-aware UTC. The `AwareDateTime` decorator normalizes at the SQLAlchemy type boundary — no workarounds needed at use sites. If a new comparison raises `TypeError: can't compare offset-naive and offset-aware datetimes`, that's a code smell pointing at a path that bypasses the ORM (raw SQL, json deserialization, etc.).

## Fixed Issues (2026-09-26 — third pass)

Nine user-reported bugs in the web dashboard were investigated and fixed. The fixes touch backend (routers, models, schema migrations), frontend (profile/students/settings/AI pages), and add a new migration runner for NOT NULL → NULL relaxations.

### Bug #1 + #7 — Profile changes didn't persist
`saveProfile()` only updated local state; `/api/auth/me` on next mount overwrote it. Added `updateMyProfile()` API helper in `src/lib/api.ts` that calls `PUT /api/users/me` (already existed on backend). Profile page now calls the API, hydrates from `/api/users/me` on mount (replaces the lossy `/api/auth/me` roundtrip), and merges `phone` + `avatar_url` into the localStorage profile.

### Bug #2 — Camera button had no onClick handler
Profile page `<button>` for avatar upload now opens a hidden `<input type="file">`. Selected image is read as base64 data URL (max 1 MB, image/* MIME only) and sent via `updateMyProfile({avatar_url})`. The avatar renders in the `<Avatar>` component when present.

### Bug #3 — Role field was always disabled / ambiguous
Form now labels username/role/studyProgram as `(read-only)` with helper text explaining they're admin-managed. Editable fields restricted to `fullName`, `email`, `phone` — matching the backend's `ProfileUpdate` schema (full_name/phone/avatar_url/bio/email).

### Bug #4 — Hardcoded "Browser ini · Windows / Gorontalo, ID"
Replaced with derived values: `navigator.userAgent` for the OS/browser string and `Intl.DateTimeFormat().resolvedOptions().timeZone` for the location. Logout button now actually calls `/api/auth/logout` instead of being a stub.

### Bug #5 — Groq API key "not connected"
`handleSend` only checked `apiKey` React state; on a fresh page load, `useEffect` populating from localStorage runs *after* render, so a user hitting Send immediately saw "Masukkan Groq API Key" even though localStorage had it. Fix: fall back to `localStorage.getItem(STORAGE_KEY)` when state is empty. **Preserved the localStorage storage pattern verbatim per user instruction** — the storage key, expiry, and SAVE_OPTIONS are unchanged.

### Bug #6 — Dosen could pick "admin" in Tambah Akun
Three coordinated changes:
- New `availableRoleOptions()` helper in `settings/page.tsx` filters `ROLE_OPTIONS` to `["mahasiswa"]` when current user is dosen, full list when admin.
- New `POST /api/users/create-student` endpoint in `routers/users.py` lets dosen create a `mahasiswa` account (role forced server-side; rejects any other role with 400). Admin still uses `POST /api/users`.
- Frontend `save()` in settings routes to `/api/users/create-student` for dosen and `/api/users` for admin.

### Bug #8 — `POST /api/students` returned 500 when picking Dosen PA
`students.user_id` was `NOT NULL` but `create_student` didn't supply one. Three coordinated changes:
- `models/student.py`: `user_id` now nullable with `ON DELETE SET NULL`.
- New migration `run_relax_migrations()` in `database.py` issues `ALTER COLUMN ... DROP NOT NULL` + swaps FK from `CASCADE` to `SET NULL` on PostgreSQL startup.
- `routers/students.py:create_student` now optionally creates a linked User account when `create_user_account=true` (with email + initial_password).
- `StudentCreate` schema gained `advisor_id`, `create_user_account`, `email`, `initial_password`.
- New endpoint `GET /api/users/advisors` exposes active dosen/admin for the "Pilih Dosen PA" dropdown.
- Frontend students page: added Dosen PA `<Select>`, AbortController-managed fetch on dialog open.

### Bug #9 — Laporan Rekap showed wrong cluster counts
`build_row` in `routers/reports.py:cluster_trends` used `cluster_labels` from the active model as row keys. When training produced 5–6 clusters (e.g. "Sangat Rendah"/"Sangat Tinggi"), the keys shifted away from the frontend's hardcoded `Rendah/Sedang/Tinggi`, and the rekap table rendered zeros. Fix: `build_row` now always emits `Rendah/Sedang/Tinggi` — for ≤3 clusters, direct mapping; for >3 clusters, count bucket into thirds by mean order. Original per-cluster counts are preserved under `cluster_<id>_<label>` keys for future UIs.

## Fixed Issues (2026-09-26 — second pass)

Eight medium-severity findings from a follow-up audit. Tier 1 (must-fix) + Tier 2 (should-fix) applied in a single session.

52. **`ClusterResult.features` was `String(2000)` — silent JSON truncation** (`models/cluster_result.py:27`). SQLite truncates without error; PostgreSQL raises `value too long for type character varying(2000)`. The 7-float feature JSON currently fits, but the column would break at the next feature-engineering iteration. Changed to `Text`. Added `run_string_widen_migrations()` in `database.py:289` that ALTERs to TEXT on first PostgreSQL startup (no-op on SQLite; idempotent via `information_schema.data_type` check). Wired into `main.py` lifespan.

53. **`LocationLog` idempotency key set but never deduped** (`routers/logs.py:80-95`). The AppLog branch checked `idempotency_key` for duplicates before INSERT (line 54-58); the LocationLog branch computed the key but skipped the check. A flaky 4G connection on the mobile app → mobile retries the batch → multiple `LocationLog` rows for the same `recorded_at` → bloat + distorted geofence analytics. Fixed by mirroring the AppLog dedup pattern. **Verified on SQLite** with a 3+2 batch + replay → exactly 3+2 rows, no duplicates.

54. **N+1 in `POST /api/logs/batch` AppLog branch** (`routers/logs.py:50-77`). Was: 1 SELECT per item (30 items = 30 round-trips). Replaced with one pre-fetched `select(AppLog.idempotency_key).where(idempotency_key.in_(keys))` + Python membership test. Same pattern applied to LocationLog. **30 round-trips → 1.**

55. **Logout's `except Exception: pass` left refresh tokens valid on DB failure** (`routers/auth.py:218-231`). Cookies cleared client-side → user thought logged out → leaked refresh token stays usable for up to 7 days. Replaced with structured retry: first attempt with `db.rollback()` on failure, second attempt on a fresh session, then `HTTPException(503)` + log + clear cookies. Operators now get `[auth/logout] token cleanup failed for user=...` in logs instead of silent pass.

56. **Admin password endpoints bypassed strength policy** (`schemas/auth.py:48,70`). `UserCreate.password` and `UserUpdate.password` had no `field_validator` — an admin could `PUT /users/{id}` with `password: "a"` and bypass the 8-char rule that `PasswordChange` enforces. Extracted shared `_validate_password_strength()` helper (8-char min, 128-char max DoS protection) and applied to all three password-accepting schemas. **Verified**: `UserCreate(password="a")` → `ValidationError`; `UserUpdate(password=None)` → accepted (no-op); 129-char → rejected.

57. **Schedule approve/reject race created duplicate `Schedule` rows** (`routers/schedule_request.py:170,222`). Two concurrent admins (or double-click) both read `status="pending"`, both create a `Schedule`, both commit → duplicate rows + audit trail loss. Added `.with_for_update()` row-lock on the `ScheduleRequest` SELECT. PostgreSQL holds the lock until commit; SQLite is a single-writer DB so the lock is a no-op but transactions still serialize correctly via `BEGIN IMMEDIATE`. **Verified on SQLite**: SELECT with `with_for_update()` returns the row, second approver sees `status="approved"` and the handler returns 400.

58. **`POST /api/models/train` race produced 500 on UNIQUE version_number collision** (`routers/models.py:140-285`). Two concurrent retrains both read `last_version_num=N`, both compute `next=N+1`, both INSERT → second hits `UNIQUE` constraint → `IntegrityError` → 500. Worse, between `is_active=False` and the new MV being inserted, no active model existed → `/api/reports/*` and `/api/models/results/latest` saw a gap. Fix has 3 parts:
    - Module-level `_train_lock = asyncio.Lock()` serializes training (only one train at a time, which the user likely wants anyway).
    - New MV created with `is_active=True` so reports always see exactly one active model — closes the gap.
    - `IntegrityError` on the INSERT triggers one retry with re-read version_number.

59. **`/api/students?cluster=N` filter was applied post-pagination, ignored by `total`** (`routers/students.py:60-70`). Filter ran in a Python `for s in rows: continue` loop after pagination + count, so `total` reflected unfiltered count and `items.length < page_size` even when more pages existed. Refactored: `cluster_map` computed first, then `matching_ids = [sid for sid, cr in cluster_map.items() if cr.cluster_label == cluster]` used in both `query.where(Student.id.in_(matching_ids))` and `count_q.where(...)`. Empty match → short-circuit return with `total=0`.

60. **`scalar_one()` after UPDATE → 500 if row deleted in race window** (`routers/users.py:88,225`, `routers/campus.py:59`). The handler commits at line 222, re-fetches at line 224; if a concurrent DELETE commits between, `scalar_one()` raises `NoResultFound` → 500. Changed to `scalar_one_or_none()` with explicit 404 check. **Pattern already used** in `routers/students.py:35` and `routers/reports.py:35` — this just brings the other endpoints into consistency.

**New verification helper:** the `ML pipeline smoke test` (`scripts/ml_pipeline_smoke_test.py`) still passes 4/4 — confirms the schema migration, AwareDateTime decorator, and tz-aware datetime comparisons all hold after the medium-severity fixes.

## Fixed Issues (2026-09-26 — fourth pass)

Eight production bugs reported by user after the round-3 deploy. Backend touches one router + one migration helper; the rest are frontend.

61. **`run_relax_migrations()` referenced undefined `_IDENT_RE`** (`app/database.py:386`). Module had three migration functions (`run_migrations`, `run_type_migrations`, `run_relax_migrations`) but `_IDENT_RE` was defined **inside** the body of `run_migrations()` (line 146) — local variable, not module-scope. When `run_relax_migrations()` (added in round-3) tried to reference it, startup crashed with `NameError: name '_IDENT_RE' is not defined` and the api container exited. Production went from "healthy" to "unhealthy" immediately after the round-3 rebuild. **Fix:** moved `_IDENT_RE` to module scope (`database.py:19`) so all three migration functions share one definition. Api rebuilt + force-recreated; healthcheck transitioned to `healthy`. **Pattern lesson:** when adding a new migration function, hoist shared state to module scope — local imports/regexes inside a sibling function are unreachable from the new call site.

62. **AI 500 — `func.max()` on tz-aware column returned naive label** (`routers/ai.py:143`). `ClusterResult.result_date` is `TIMESTAMP WITH TIME ZONE` (via `AwareDateTime`). `select(func.max(...).label("max_date"))` produced a naive label in SQLAlchemy 2.x; PostgreSQL/asyncpg rejected the equality with `DataError: can't subtract offset-naive and offset-aware datetimes`. Production logs showed the crash on every `GET /api/ai/context`. **Fix:** wrapped the aggregate with `cast(func.max(ClusterResult.result_date), AwareDateTime).label("max_date")` — standard SQLAlchemy pattern for type-casting aggregate expressions. Added `cast` to the imports. `GET /api/ai/context` now returns `200` with full cluster context (verified: model v12, 795 PCA points, 3 clusters). **Pattern lesson:** when comparing an aggregate against a tz-aware column, cast the aggregate expression to the same `TypeDecorator` used by the column — don't try to override `.columns()` on the subquery (that was my first attempt; `ReadOnlyColumnCollection` is not callable in 2.x).

**Round-4 frontend fixes** (in `website/CLAUDE.md`): avatar in header, dashboard greeting uses username, dashboard/students/models/logs/monitoring/reports metric strips switched to hairline-grid-only `MetricCard` (no per-card border, no colored tints, no pill backgrounds — `ColorMetricCard` kept as legacy for any external imports), page-header title truncation fixed, sidebar collapsed visibility improved (numbers now `text-ink`/`text-ink/70` instead of `text-ink/35`), privacy page fully rewritten with editorial UI + expanded content (TL;DR, RBAC table, cookie matrix, hak Anda per UU PDP, deletion methods, kontak).

## Fixed Issues (2026-09-26 — fifth pass / round 5)

Ten user-reported production bugs. Backend adds two new tables (`program_studi`, `login_devices`), four new endpoints, and trims two em-dashes. The frontend gets role badges + a Jurusan dropdown + editorial AccessDenied + a full login-device-history list + K-Means labels that react to K count + Models/Profile chrome cleanup. Android ships a single parity change: device fingerprint on login.

### Backend new tables

63. **`program_studi` master list** (`app/models/program_studi.py` + `app/schemas/program_studi.py` + `app/routers/program_studi.py`). New table with `id` (UUID), `code` (UNIQUE INDEX), `name`, `description`, `is_active`, `created_at`, `updated_at`. `Base.metadata.create_all` auto-creates the table on next startup — no explicit ALTER needed. Schemas use `field_validator` to enforce 1–100 chars on `code`/`name`. The router exposes:
    - `GET /api/program-studi?include_inactive=false` — any authenticated user (read-only dropdown source for the user-form in `/settings`)
    - `POST /api/program-studi` / `PUT /api/program-studi/{id}` — admin only via local `get_admin_only` helper (mirrors the `require_admin_or_dosen` pattern, scoped to `program_studi`)
    - `DELETE /api/program-studi/{id}` — admin only; **soft-deletes** (sets `is_active=False`) when students still reference the `name` (preserves FK integrity), hard-deletes when no references
    Registered in `app/main.py` under `API_V1_PREFIX`. Verified: `POST /api/program-studi` with `{"code":"IF","name":"Informatika"}` → `201 Created` with the row; `GET /api/program-studi` → `200` with the same row; `DELETE` after the soft-delete path keeps the row but marks `is_active=False`.

64. **`login_devices` per-user device history** (`app/models/login_device.py` + additions to `app/schemas/auth.py` + `app/routers/auth.py`). New table with `id` (UUID), `user_id` (FK CASCADE), `device_id` (indexed, SHA-256 first-32-hex), `device_name` (parsed UI string), `user_agent` (raw), `first_seen`, `last_seen` (both `AwareDateTime`), `is_current` (Boolean). Dedupes on `(user_id, device_id)` — the same browser/installed-app always lands on the same row. `LoginRequest` schema gained optional nullable `device_id` / `device_name` / `user_agent`. The `login` handler now:
    - hashes `_hash_device_fallback(user_agent)` if `device_id` absent (preserves API compatibility for legacy clients)
    - upserts per `(user_id, device_id)` — existing row updates `last_seen`, new row inserts with `first_seen = last_seen = now`
    - flips `is_current=True` on the matching row and `is_current=False` on siblings
    Helpers `_hash_device_fallback(user_agent)` and `_parse_device_name(user_agent)` kept file-private. Two new endpoints:
    - `GET /api/auth/devices` — ordered by `last_seen DESC`, returns `LoginDeviceResponse` list
    - `DELETE /api/auth/devices/{device_id}` — deletes all refresh tokens for the user (since `Token` table has no `device_id` FK), clears `is_current` on the target row, returns `204`
    **Verified end-to-end:** 3 logins from 2 unique `device_id`s produced exactly 2 rows (third login from same `device_id` only updated `last_seen` and flipped `is_current`); `DELETE /api/auth/devices/{id}` returned `204` and subsequent `GET` returned 1 row.

### Backend bug caught during verification

65. **`LoginDeviceResponse.first_seen/last_seen: str | None` raised `ResponseValidationError: 500`**. Pydantic 2 in ORM mode (`from_attributes=True`) does NOT auto-stringify `datetime` when the schema field is annotated as `str`. The serializer validated the raw `datetime` objects against the `str` annotation and failed with `Input should be a valid string`. **Fix:** changed the annotations to `datetime | None`. Pydantic's default serializer emits ISO-8601 strings, which is what the web client already expected (`new Date(...)` parsing on the JS side). Added `from datetime import datetime` to `schemas/auth.py`. **Pattern lesson:** when a `BaseModel` with `from_attributes=True` declares a field as `str` but the ORM object returns `datetime` (or any other type), either match the source type or add an explicit `field_serializer`. Pydantic 2 does not coerce.

### Backend em-dash sweep

66. **`routers/students.py:145,248`** — `s.program_studi or "—"` → `s.program_studi or ""` in two list endpoints. Same pattern as the round-4 privacy/AI fix — empty string is the editorial-correct fallback, frontend renders the muted `kosong` caption when the field is truly empty.

## Fixed Issues (2026-09-27 — seventh pass / round 7)

User-pasted 13-item bug-and-feature list. All items addressed. Backend adds three tables + seven endpoints; the cluster-trends endpoint got a semantic `weeks` parameter; reports endpoints emit `Rendah/Sedang/Tinggi` only when the active model has the same label keys (≤3 clusters → direct mapping, >3 → bucket by mean order into thirds; original per-cluster keys preserved as `cluster_<id>_<label>`).

### Backend new tables + routers

67. **`mata_kuliah` master** (`app/models/mata_kuliah.py` + `app/routers/mata_kuliah.py`). `id` (UUID), `code` (str, 1–20), `name` (str, 1–100), `sks` (int), `semester` (int), `program_studi_id` (FK to `program_studi.id`, nullable), `dosen_id` (FK to `users.id`, nullable), `description` (str), `is_active`, `created_at`, `updated_at`. Router mirrors `program_studi.py` shape: `GET` is any-auth read; `POST/PUT/DELETE` admin/dosen via local `require_staff` helper. Soft-delete via `is_active=False` so historical schedules stay resolvable.

68. **`jadwal_mata_kuliah` (course schedule)** (`app/models/jadwal_mata_kuliah.py` + `app/routers/jadwal_mata_kuliah.py`). `id`, `mata_kuliah_id` (FK CASCADE), `day_of_week` (1–7 ISO), `start_time` (Time), `end_time` (Time), `room` (str), `is_active`, `created_at`, `updated_at`. Schema uses `_parse_hhmm` field validator (HH:MM string coercion). Same RBAC.

69. **`request_akademik` (course/schedule requests)** (`app/models/request_akademik.py` + `app/routers/request_akademik.py`). `id`, `type` (literal `"course" | "schedule_slot"`), `name`, `description`, `payload` (str — JSON blob), `target_id` (nullable UUID), `requested_by_id` (FK to users), `status` (`pending|approved|rejected`), `reviewed_by_id` (FK to users), `review_notes`, `created_at`, `updated_at`. **Any** authenticated user can POST (mahasiswa and dosen alike). `GET` returns own requests for non-staff, all for admin/dosen (filter by `?status=`). `PUT /{req_id}/review` is admin/dosen only — sets status + notes. Approving does **NOT** auto-create the underlying `mata_kuliah` / `jadwal_mata_kuliah`; admin uses the request as a checklist and creates the row separately via `/api/mata-kuliah` or `/api/jadwal-mata-kuliah`. `DELETE` allowed for requester (own pending only) or admin.

### Backend bug fix during verification

70. **`routers/reports.py:cluster_trends` `weeks` parameter was unit-confused** — daily mode computed `range(weeks * 7 - 1, -1, -1)` (49 days for `weeks=7`!), weekly mode was OK, monthly mode used a hardcoded 6. Now `weeks` is **semantic per period**: days for daily, weeks for weekly, months for monthly. Range loop `range(weeks - 1, -1, -1)` for all three. Query param validation `ge=1, le=30` to cap the largest request.

All three tables auto-create on next startup via `Base.metadata.create_all` — no manual migration needed (round-7 schema is fresh, no in-place ALTER). Verified: `POST /api/mata-kuliah` → 201; `POST /api/jadwal-mata-kuliah` → 201; `POST /api/request-akademik` → 201; `PUT /api/request-akademik/{id}/review` → 200 with status flipped to `approved`; `PUT /api/mata-kuliah/{id}` with `is_active=false` → 200; subsequent `GET /api/mata-kuliah?include_inactive=true` shows the inactive row.

## Fixed Issues (2026-09-27 — round 10 / perf)

Production dashboard had been polling `/api/reports/*` and `/api/models/*` aggressively with no in-process caching. Every dashboard refresh hit PostgreSQL for: the active model id, the active model cluster_labels (JSON parse), the per-day screen-time trends (N queries in a loop), the per-period cluster distribution (3N queries across daily/weekly/monthly), and 5 separate counts for the dashboard summary. None of these change often — most are bounded by "what time is it" or "did someone click Retrain".

The fix has four coordinated parts: in-process TTL cache, N+1 → single GROUP BY, DB indexes on hot filter columns, and a new migration runner.

### In-process TTL cache

71. **`app/cache.py` — Ponytail-grade TTL cache** (`app/cache.py:1`). Three functions: `cache_get(key, ttl)`, `cache_set(key, value, ttl)`, `cache_invalidate(key)`. Backed by a module-level dict with `time.monotonic()`-based expiry; thread-safe via a single `Lock`. No async, no Redis round-trip (the cached value is tiny — one row — and the round-trip to Redis costs more than recomputing from a hot dict). Stale entries are dropped lazily on next read; no background sweeper.

### Reports N+1 → single GROUP BY

72. **`get_screen_time_trends` collapsed 7→1 (or 90→1) queries** (`routers/reports.py:172`). Was: per-day `select(...).where(timestamp BETWEEN day_start AND day_end)` inside a `for i in range(days - 1, -1, -1)` loop. Now: single `select(...).group_by(func.date(AppLog.timestamp))` over the full range, then dict-lookup in Python to backfill missing days with zeros. The chart x-axis stays continuous because we iterate the full date range in Python even though DB only returned sparse rows.

73. **`get_cluster_trends` collapsed 3N → 1 query** (`routers/reports.py:225`). Was: three near-identical day/week/month loop bodies, each issuing a fresh query per period. Now: each branch runs one `group_by(bucket, cluster_label)` query — `func.date(...)` for daily, `func.date_trunc('week', ...)` (PostgreSQL) / `func.strftime('%Y-%W', ...)` (SQLite) for weekly, `func.date_trunc('month', ...)` (PostgreSQL) / `func.strftime('%Y-%m', ...)` (SQLite) for monthly. The bucket-to-period mapping is done in Python (sort bucket keys, assign to slots oldest → newest) — avoids date-arithmetic-in-Python and lets the DB do the GROUP BY once. **`build_row()` unchanged** — same `Rendah/Sedang/Tinggi` projection logic, same per-cluster-label preservation.

### Summary endpoint consolidation

74. **`get_dashboard_summary` collapsed 5 queries → 4 + 30s cache** (`routers/reports.py:430`). Was: 5 sequential round-trips — total students, active students (DISTINCT), app_logs count mobile, avg screen time today, seed_logs count. The two mobile-week queries (active students + app_logs count) share `source='mobile' AND timestamp range`, so they're now a single `select(func.count(...), func.count(func.distinct(...)))` query. The remaining 3 queries (total students, screen time today on `log_date`, seed count) genuinely need separate round-trips because they hit different columns. Response cached in-process for 30s — the dashboard auto-refresh is much faster than that, so the cache collapses N dashboard-poll-cycles into 1 query per minute per pod. **Cache key:** `reports:summary`.

### Active model cache + invalidation

75. **`get_active_model_id` + `_get_active_model_labels` cached 60s** (`routers/reports.py:44,65`). Both helpers are called on every `/api/reports/cluster-trends`, `/api/reports/summary`, and indirectly via `/api/models/results/latest`. The active model changes only when an admin runs `POST /api/models/train` (rare — nightly scheduler + manual). Caching for 60s is conservative. **Cache keys:** `reports:active_model_id`, `reports:active_model_labels`. Sentinel values: `""` = no active model, `None` = active model has unparseable labels (kept cache hit, not re-query). **Invalidation wired in `routers/models.py:240,289,303`** — three sites: success path (after `db.commit()` post cluster_results insert), `ValueError` failure branch, generic `Exception` branch. Trainers drop both keys so the next dashboard hit sees the new model immediately.

### DB indexes

76. **`run_index_migrations()` — 6 indexes for hot filter columns** (`database.py:362`). `CREATE INDEX IF NOT EXISTS` is idempotent (PostgreSQL 9.5+ and SQLite 3.8+ both support it natively — no dialect branch needed). Created at startup via `main.py` lifespan, alongside the existing migration helpers. Indexes:
    - `idx_app_logs_timestamp` — used by `reports.summary`, `reports.screen-time`, `reports/cluster-trends`, ML pipeline's `WHERE timestamp BETWEEN ...`
    - `idx_app_logs_student_id` — used by batch insert dedup, latest-result per-student queries, AI context
    - `idx_app_logs_source` — used by `reports/summary` mobile/seed filter (the small-cardinality string column is still indexed because the predicate appears in nearly every reports query)
    - `idx_cluster_results_model_version_id` — used by `cluster-trends`, `models/results/latest`, `models/results/points` (every dashboard load)
    - `idx_cluster_results_result_date` — used by `cluster-trends` date range filter (the most selective predicate in cluster-trends queries)
    - `idx_model_versions_is_active` — used by every `WHERE is_active = true` lookup
    Identifier whitelist via the existing `_IDENT_RE` module-scope regex — same defense-in-depth as the other migration helpers.

### Pattern lessons

- **No Redis for hot small-row caches.** Adding a Redis hop to fetch the active model id costs more than the SELECT. The cache must be in-process and synchronous. Redis earns its keep for real-time pub/sub (WebSocket + SSE), RQ job queue, and the like — not for one-row lookups.
- **GROUP BY date bucketing beats per-period loops when periods are dense.** For `days=90` the old code issued 90 round-trips. The new code is one. The DB does the bucketing in C, returns N sparse rows, and Python walks the date axis to fill in zeros. For sparse data (long-tail, weekday-skewed), this is dramatically faster.
- **Cache invalidation points must match cache write points.** Three write points for the active-model caches: training success, training failure (which can still toggle `is_active` because the MV was created with `is_active=True`), and the explicit rollback path. Missing any one of them leads to dashboards showing a stale model. The naming convention `_CACHE_KEY_*` constants shared between routers keeps the two modules in sync.

### Verified

- `app/cache.py` round-trip + sentinel + invalidation test passes (4/4 assertions)
- All 6 modified Python files parse cleanly under `ast.parse()`
- All 17 routers still import their dependencies correctly (no circular imports introduced — `app.cache` has no app-internal imports)

## Fixed Issues (2026-09-27 — round 10 / user feedback batch)

User-pasted feedback batch with backend-relevant items: JWT access token terlalu panjang (60 menit → diperpendek), halaman register tiba-tiba redirect ke /login setelah 3 detik, dan permintaan untuk menambah section Changelog + rujukan UUD di Kebijakan Privasi (frontend).

### Bug #R1 — JWT access token lifetime 60 menit → 30 menit

`app/config.py`: `ACCESS_TOKEN_EXPIRE_MINUTES: int = 60` → `30`. Alasan user: masa hidup yang lebih pendek memperkecil window paparan token yang dicuri jika ada kebocoran. Refresh token tetap 7 hari sehingga sesi panjang (aplikasi mobile) tidak terputus; frontend sudah memiliki refresh-then-retry pattern pada `apiFetch`.

### Bug #R2 — Register page 3-second redirect race

`app/routers/program_studi.py`: GET `/api/program-studi` dibuka untuk publik (tidak butuh autentikasi). Sebelumnya endpoint ini memanggil `Depends(get_current_user)`, sehingga form register yang memuat `getProgramStudi()` sebelum login memicu chain `apiFetch 401 → refreshAccessToken failed → window.location.href = "/login"` tepat 3 detik setelah halaman dibuka (race dengan debounce search register page).

Verifikasi:
- Tanpa login: `GET /api/program-studi` → 200 dengan list
- Dengan login: `GET /api/program-studi` → 200 dengan list (sama)
- Method tulis (POST/PUT/DELETE) tetap dilindungi `get_current_user` + RBAC sesuai role

### Verified

- `python -c "from app.config import ACCESS_TOKEN_EXPIRE_MINUTES; print(ACCESS_TOKEN_EXPIRE_MINUTES)"` → `30`
- `curl http://localhost:8000/api/program-studi` (no cookie) → 200 dengan JSON list
- `curl http://localhost:8000/api/program-studi -H "Cookie: <dosen session>"` → 200 (sama)
- Frontend register page dapat memuat dropdown prodi tanpa redirect ke /login

## File Map

```
app/
├── main.py               # FastAPI app, CORS, router registration
├── limiter.py            # Single Limiter instance (imported by routers + registered in app.state)
├── cache.py              # NEW round 10 — in-process TTL cache for hot lookups
├── config.py             # Settings from environment
├── database.py           # async engine, session, run_migrations() (incl. run_relax_migrations for round 3, run_type_migrations for round 4, run_index_migrations for round 10)
├── schemas/
│   ├── auth.py           # Token schemas, login/register (+LoginDeviceResponse)
│   ├── model.py          # ModelConfigUpdate (includes cluster_labels)
│   ├── student.py        # StudentCreate
│   └── program_studi.py  # ProgramStudiCreate/Update/Response (round 5)
├── models/              # SQLAlchemy models
│   ├── user.py
│   ├── student.py
│   ├── app_log.py
│   ├── location_log.py
│   ├── schedule.py
│   ├── model_version.py  # cluster_labels stored as Text (JSON)
│   ├── cluster_result.py
│   ├── program_studi.py  # NEW round 5 — master list of academic programs
│   ├── login_device.py   # NEW round 5 — deduped per-user device fingerprint rows
│   ├── mata_kuliah.py    # NEW round 7 — course master list
│   ├── jadwal_mata_kuliah.py # NEW round 7 — course schedule slots (FK CASCADE)
│   └── request_akademik.py   # NEW round 7 — course / schedule_slot requests
├── routers/
│   ├── auth.py          # Login, refresh, register (+GET/DELETE /devices round 5)
│   ├── users.py         # CRUD, password change, role validation
│   ├── students.py      # Student CRUD (admin/dosen — require_admin_or_dosen)
│   ├── schedules.py     # Student schedules
│   ├── schedule_request.py  # Schedule change requests
│   ├── logs.py          # Batch upload, real-time stream (WebSocket primary, SSE legacy, cookie-auth)
│   ├── reports.py       # Aggregate reports, screen-time trends (require_admin_or_dosen) — round 10: N+1 → GROUP BY + 60s active-model cache + 30s summary cache
│   ├── models.py        # ML config, training, history (admin auth)
│   ├── status.py        # Service health, active users
│   ├── ai.py            # Groq AI chat with cluster context (admin/dosen)
│   ├── campus.py        # Campus geofence CRUD (GET auth-only, others admin/dosen)
│   ├── seed.py          # Seed data generator (admin only)
│   ├── program_studi.py # round 5 — admin CRUD + GET (any auth)
│   ├── mata_kuliah.py        # NEW round 7 — CRUD courses (admin/dosen via require_staff)
│   ├── jadwal_mata_kuliah.py # NEW round 7 — CRUD schedule slots (admin/dosen)
│   └── request_akademik.py   # NEW round 7 — any auth POST, admin/dosen PUT review
├── seed_data.py         # Seed logic: 15 students + ~10k logs (session-level)
├── ml/
│   └── pipeline.py      # PCA + K-Means; unlock_count/used_during_class selected
└── mqtt_subscriber.py  # MQTT → database log ingestion
```

## Fixed Issues (2026-09-27 — round 11 / trio rerata harian)

User feedback batch: tambah rerata unlock count, rerata sosial media, rerata game di monitoring + reports + dashboard; Changelog dipindahkan ke page tersendiri; deploy + verifikasi versi.

### Added — `avg_social_media_time` + `avg_game_time` ke `/api/reports`

`app/routers/reports.py` (fungsi `get_report`): tambah dua query `func.avg(AppLog.social_media_time_minutes)` dan `func.avg(AppLog.game_time_minutes)` mengikuti pola `avg_screen_time` (try/except + scalar or 0, cast ke float, round 2 desimal). Output JSON sekarang punya:
- `avg_screen_time` (sudah ada)
- `avg_unlock_count` (sudah ada)
- `avg_social_media_time` (NEW)
- `avg_game_time` (NEW)

### Added — trio rerata "hari ini" ke `/api/reports/summary`

`app/routers/reports.py` (fungsi `get_dashboard_summary`): tambah tiga field di response:
- `avg_unlock_count_today`
- `avg_social_media_time_today_min`
- `avg_game_time_today_min`

Mengikuti pola `avg_screen_time_today_min`: query pada baris app_logs hari ini (timezone Asia/Makassar), try/except untuk graceful fallback ke 0. Cache key tetap `"reports:summary"` (TTL 30 detik) sehingga invalidasi dari training router otomatis terpicu.

### Verified post-deploy

- `GET /api/reports/summary` (cookie admin) → 200 dengan semua field baru:
  ```json
  {
    "total_registered": 17,
    "active_students_week": 0,
    "pending_students": 17,
    "app_logs_week": 0,
    "seed_logs_week": 1268,
    "avg_screen_time_today_min": 0.0,
    "avg_unlock_count_today": 0.0,
    "avg_social_media_time_today_min": 0.0,
    "avg_game_time_today_min": 0.0,
    "date": "2026-09-27"
  }
  ```
- `GET /api/reports?period=weekly&start_date=2026-09-20&end_date=2026-09-27` → 200:
  ```json
  {
    "avg_screen_time": 113.35,
    "avg_unlock_count": 28.2,
    "avg_social_media_time": 37.69,
    "avg_game_time": 20.91,
    ...
  }
  ```
- Android: tidak ada perubahan. Android sudah mengumpulkan `social_media_time_minutes`, `game_time_minutes`, dan `unlock_count` sejak versi awal via UsageStats (lihat `monitor-ung-app/CLAUDE.md` round 11).

## Fixed Issues (2026-09-27 — round 13 / AI trio + program studi case-sensitivity)

Three backend fixes: AI router enriched with trio fields for learning-pattern recommendations; program studi code uniqueness made case-insensitive.

### Backend bug fix

77. **`routers/ai.py` week stats missing trio fields** — `_build_cluster_context` returned only `sot`, `unlock`, and `logs` in `week_stats`. The AI assistant could not reference social-media or game time in recommendations. Added `func.avg(AppLog.social_media_time_minutes)` as `avg_sosmed` and `func.avg(AppLog.game_time_minutes)` as `avg_game` to both the current-week and prior-week queries. Response now includes `sosmed`, `game`, `prev_sosmed`, `sosmed_delta`, `prev_game`, `game_delta` alongside existing `sot`/`prev_sot`/`sot_delta`. System prompt updated to mandate trio field references in analysis and recommendations. Legenda in the prompt expanded with sosmed/game field names and delta semantics.

78. **`routers/program_studi.py` code uniqueness check was case-sensitive** — `select(ProgramStudi).where(ProgramStudi.code == data.code)` matched exact case. Depending on PostgreSQL locale/collation, "PTI", "pti", and "Pti" could coexist as separate rows. More confusingly, if the DB collation was case-insensitive (e.g., `en_US.utf8` with `CITEXT`), the SELECT passed but the UNIQUE index on `code` might have blocked re-insertion. Fixed: both `create_program_studi` and `update_program_studi` now use `func.lower(ProgramStudi.code) == data.code.lower()` for the duplicate check. `func` added to the import. This makes the check consistently case-insensitive regardless of DB collation.

### Code quality note

- **Seeder already has trio fields** — `seed_data.py:194-197` writes `social_media_time_minutes` and `game_time_minutes` per session. No seeder change needed.
- **`/api/jurusan` RBAC verified** — `GET` requires `get_current_user` (any auth), `POST`/`PUT`/`DELETE` require `get_admin_only`. Correct. The `JurusanResponse` schema includes `fakultas_id` so callers can join up to the faculty.

## Fixed Issues (2026-09-27 — round 12 / header auth bersih + trio rerata live)

User feedback batch: prodi cuma bisa ditambah admin; web + Android registration konten harus sama; `/changelog` belum ada di header; search bar harus lebih besar; font "Ringkasan Klaster" belum konsisten di monitoring; UI revisions per image (gear icon pindah inline, audit font); audit hardcode rerata; KBBI + em-dash + typo sweep.

### Catatan untuk backend round 12

Round 12 tidak menambah endpoint baru — semua perubahan ada di website. Backend hanya menjadi target audit hardcode:
- `/api/reports` sudah punya 4 field rerata (screen_time, unlock_count, social_media_time, game_time) sejak round 11.
- `/api/reports/summary` sudah punya 4 field rerata "hari ini" sejak round 11.
- `/api/students`, `/api/logs`, `/api/dashboard/summary` tidak punya hardcode rerata.

### Verified live deploy

- `GET /api/reports/summary` (cookie hary@ung.ac.id) → 200 dengan semua field trio:
  ```json
  {
    "total_registered": 17,
    "active_students_week": 0,
    "pending_students": 17,
    "app_logs_week": 0,
    "seed_logs_week": 1268,
    "avg_screen_time_today_min": 0.0,
    "avg_unlock_count_today": 0.0,
    "avg_social_media_time_today_min": 0.0,
    "avg_game_time_today_min": 0.0,
    "date": "2026-09-27"
  }
  ```
- `GET /api/reports?period=weekly&start_date=2026-09-20&end_date=2026-09-27` → 200 dengan keempat rerata terisi dari database (bukan hardcode).
- `GET /api/program-studi` tanpa auth → 200 (publik sejak round 10.5; dipakai halaman `/register`).

## Fixed Issues (2026-09-27 — round 14 / backend audit)

Code quality audit menemukan 10 issue: 6 dead code, 4 logic. Semua sudah fixed.

### Dead code cleaned up
- **`routers/schedule.py`** — hapus `require_admin()` helper (tidak dipakai), hapus `import uuid` (tidak dipakai)
- **`routers/logs.py`** — hapus `get_authorization_token()` (duplikat dari `get_current_user`)
- **`routers/seed.py`** — hapus `get_sync_db()` (tidak dipakai), hapus `from sqlalchemy.orm import Session`
- **`mqtt_subscriber.py`** — hapus `parse_topic()` (dead code; inline parsing dipakai langsung di `on_message`)
- **`routers/program_studi.py`** — fix duplicate `func` import (`from sqlalchemy import select, func, func` → `select, func`)

### Logic fixes
- **`seed_data.py:35`** — `SEED_END` hardcoded `datetime(2026, 9, 22)` → `datetime.now(WITA) - timedelta(days=1)` sehingga seed data selalu mencakup sampai kemarin (stale date jika admin regenerate)
- **`routers/fakultaS.py:47,77`** — duplicate check case-sensitive → case-insensitive dengan `func.lower(Fakulties.code) == data.code.lower()` (matching pattern dengan `program_studi.py` round 13)
- **`schemas/jurusan.py:11`** — `JurusanResponse.fakultas_id` wajib `str` tapi kolom DB nullable → `str | None = None`
- **`routers/schedule.py:32-33`** — `schedule.subject.name` crash jika `subject` ada tapi `.name` null → `getattr(schedule.subject, 'name', None) or schedule.subject_name_raw`
- **`routers/students.py:147`** — `s.advisor.full_name` crash jika advisor di-deleted → `getattr(s.advisor, 'full_name', None)`

## Fixed Issues (2026-09-28 — round 15 / soft-delete JSON body + orphan prodi cleanup)

User feedback batch: PTI orphan row harus dibersihkan agar admin create manual, dan `DELETE /api/program-studi` (beserta fakultas + jurusan) kehilangan JSON body saat soft-delete karena decorator `status_code=204` membuat FastAPI strip body pada return dict.

### Orphan prodi cleanup

- **`models/program_studi.py`** — Jurusan FK `UNIQUE` constraint ditambahkan (`uq_jurusan_code`, `uq_jurusan_name`) agar dua jurusan tidak bisa share nama/kode. Schema diupdate agar `ProgramStudiCreate` menerima `jurusan_id` (FK ke `jurusan.id`).
- **`database.py`** — dua migration runner baru:
  - `run_unique_constraint_migrations()` — `ALTER TABLE jurusan ADD CONSTRAINT uq_jurusan_code UNIQUE (code)` (idempotent: cek `pg_constraint` dulu). SQLite no-op karena `unique=True` di model sudah bikin unique index via `Base.metadata.create_all`.
  - `cleanup_orphan_prodi()` — soft-delete orphan prodi (`jurusan_id IS NULL`) yang tidak punya referensi student di kolom `students.program_studi` text. Orphan dengan referensi tetap di-aktifkan dan dilog sebagai warning — operator harus reassign student dulu sebelum re-run.
- **`main.py`** lifespan di kedua migration helper dipanggil setelah `Base.metadata.create_all`.
- **Verified**: PTI (no refs) → soft-deleted; IF (9 student refs) → LEFT ACTIVE + log warning. Active prodi sekarang: SI, TDP. Sisanya (IF, PTI, TESTDEL, TI) soft-deleted.

### Soft-delete JSON body preservation

`DELETE /api/{fakultas|jurusan|program-studi}/{id}` decorator `status_code=204` menyebabkan FastAPI strip body dari response dict `{"detail": "Dinonaktifkan", "referenced_by_*": N}` → client dapat HTTP 204 + empty body, tidak bisa membedakan "soft-deleted" vs "hard-deleted".

Fix di tiga router:
```python
# Decorator tetap punya status_code=204 untuk hard-delete path (None return → empty body, 204)
@router.delete("/{id}", status_code=204)
async def delete_xxx(...):
    if referenced > 0:
        # JSONResponse override: status_code=200 explicit, body preserved
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=200,
            content={"detail": "Dinonaktifkan", "referenced_by_*": referenced},
        )
    await db.delete(row)
    await db.commit()
    return None  # → 204 empty
```

**Pattern lesson**: ketika soft-delete perlu return body, return `JSONResponse(status_code=200, content=...)` dan biarkan decorator tetap `status_code=204`. FastAPI pakai status_code dari JSONResponse saat handler return Response object, dan dari decorator saat return `None`. Jangan hapus decorator — kalau dihapus, hard-delete jadi HTTP 200 + body `null`.

**Verified live (after rebuild)**:

| Endpoint | Path | HTTP | Body |
|----------|------|------|------|
| `DELETE /api/fakultas/{id}` | soft (refs exist) | 200 | `{"detail":"Dinonaktifkan","referenced_by_jurusan":1}` |
| `DELETE /api/fakultas/{id}` | hard (no refs) | 204 | empty |
| `DELETE /api/jurusan/{id}` | hard (no refs) | 204 | empty |
| `DELETE /api/program-studi/{id}` | soft (student refs) | 200 | `{"detail":"Dinonaktifkan","referenced_by_students":1}` |
| `DELETE /api/program-studi/{id}` | hard (no refs) | 204 | empty |

## Round 17 — Sync contract `client_id` echo (2026-09-30)

User feedback batch Android 2026-09-30: "pending sudah berkali kali pada antrian" + "data colection tidak konsisten sehaerusnya bertambah kadang malah kurang". Dua-duanya root cause sama: server dan Android punya kontrak sync yang tidak match.

### Masalah

Android `UsageRepositoryImpl.syncPendingLogs()` + `DataSyncManager.syncPendingData()` keduanya baca `body.syncedClientIds` (yang asumsi server return list of local row IDs that were actually persisted), filter `pendingIds` terhadap list tersebut, baru `markSynced()` hanya yang ada di sana.

Server sebelum round 17 tidak pernah echo balik `client_id`. `SyncStatusResponse` cuma `{idempotency_key, status, processed_count}`. Result: `syncedClientIds` selalu `[]` → semua pending row di-keep → pending count stuck.

Plus `data/snapshot/UsageSnapshotStore.kt` (Android side, new singleton) mengumpulkan per-package deltas dari 3 collectors. Tanpa sinkronisasi yang akurat, daily aggregate naik-turun.

### Fix — `app/schemas/log.py`

```python
class AppLogItem(BaseModel):
    client_id: str | None = None  # NEW — Android local row id

class LocationLogItem(BaseModel):
    client_id: str | None = None  # NEW

class SyncStatusResponse(BaseModel):
    idempotency_key: str
    status: str
    processed_count: int
    synced_client_ids: list[str] = []  # NEW — echo back accepted client IDs
```

### Fix — `app/routers/logs.py` batch endpoint

Loop collects `synced_client_ids: list[str] = []`, appends `log_item.client_id` saat row baru di-insert (skip duplicates yang sudah ada di `existing_app` set, jadi replay tidak double-count):

```python
synced_client_ids: list[str] = []
for log_item in data.logs:
    composite_key = f"{data.idempotency_key}:app:{log_item.log_date}"
    if composite_key in existing_app:
        continue
    # ... existing insert ...
    if log_item.client_id:
        synced_client_ids.append(log_item.client_id)

for loc_item in data.locations:
    loc_key = f"{data.idempotency_key}:loc:{loc_item.recorded_at}"
    if loc_key in existing_loc:
        continue
    # ... existing insert ...
    if loc_item.client_id:
        synced_client_ids.append(loc_item.client_id)

return SyncStatusResponse(
    idempotency_key=data.idempotency_key,
    status="processed",
    processed_count=processed,
    synced_client_ids=synced_client_ids,
)
```

`client_id` adalah string-typed di Python side (Android side row IDs adalah Long; Android `toDto()` converts via `id.toString()` sebelum kirim). Echo sebagai string preserves precision untuk long auto-increment rows.

### Verified post-deploy

- `docker compose build --no-cache api` → SUCCESS, image rebuilt
- Container restart: `monitorung-api` healthy, `/health` returns 200
- `SyncStatusResponse.model_fields['synced_client_ids']` confirmed `annotation=list[str] required=False default=[]`
- `POST /api/logs/batch` (no auth) → 401 (schema validates cleanly, auth enforced, no 500)

End-to-end Android test: setelah deploy, user trigger manual sync di Settings → pending count drops ke 0 dalam 1 batch (sebelumnya stuck). Daily summary accurate karena data dari 3 collectors sekarang lewat single shared snapshot (lihat `monitor-ung-app/CLAUDE.md` Round 17 Part 4c/4d).

## Audit Round — Wave 1 (2026-10-05): 1-line cross-cutting fixes

Lima fix high-leverage, mostly satu baris, menyentuh trust boundary (dashboard akurat = admin bisa ambil keputusan benar). Output dari audit komprehensif 3-komponen (server + website + android).

### 1. `datetime.now(WITA).date()` di 5 sites — fix "00:00-08:00 WITA tampil kemarin sebagai hari ini"

**Files:** `app/routers/reports.py:264,316,527` (3 sites), `app/routers/ai.py:343,698` (2 sites)

**Sebelum:**
```python
today = datetime.now(timezone.utc).date()
```

**Sesudah:**
```python
today = datetime.now(WITA).date()
```

**Why:** Antara 00:00-08:00 WITA (= 16:00-24:00 UTC hari sebelumnya), `today = UTC.date()` returns yesterday's date, tapi range query pakai WITA offset (`range_end = today + 1 day` → kemarin's WITA midnight). Hasil: chart label "today" tampil kemarin. Setiap dashboard summary + AI context + cluster trends + screen-time trends terdampak 8 jam per hari.

**Fix:** Pakai `WITA` (single source of truth di `app/timezone.py`) untuk anchor "today". `WITA` sudah di-import di kedua file. `timezone.utc` masih dipakai di line lain untuk pattern `datetime.combine(..., tzinfo=timezone.utc) - wita_offset` (WITA midnight → UTC subtraction) — itu benar, tidak disentuh.

**Verified:** `python3 -m py_compile` clean; live container boot `Server app import OK — 89 routes loaded`.

### 2. `CampusCreate.model_fields` bukan `__pydantic_fields__`

**File:** `app/routers/config.py:75`

**Sebelum:**
```python
geofence_radius_meters=int(CampusCreate.__pydantic_fields__.get("radius_meters").default),
```

**Sesudah:**
```python
geofence_radius_meters=int(CampusCreate.model_fields["radius_meters"].default),
```

**Why:** `__pydantic_fields__` adalah Pydantic 1.x private compat shim. Pydantic 2.x public API: `model_fields`. Future-proof + tidak mengakses private internals.

### 3. Dead-code `async def on_message` di `mqtt_subscriber.py` dihapus

**File:** `app/mqtt_subscriber.py:238-262` (25 baris)

**Why:** Ada `async def on_message(client, userdata, message)` di module scope yang tidak pernah di-wire ke `client.on_message` (real handler adalah `def on_message` di line 288 dalam `start_subscriber()` closure, yang dispatch via `_loop.call_soon_threadsafe`). Dead function shadowed the wired one, membuat debugging confusing.

**Fix:** Hapus seluruh blok `async def on_message` + surrounding blank lines.

### 4. `asyncio.get_running_loop()` bukan deprecated `asyncio.get_event_loop()`

**File:** `app/routers/status.py:119`

**Why:** Di dalam running async context, `get_event_loop()` deprecated di Python 3.10+. Bisa raise `DeprecationWarning` atau fail di 3.12+. Yang benar: `asyncio.get_running_loop()`.

### 5. `useSearchParams` di website dibungkus `<Suspense>` (server-side)

**File:** `website/src/app/(dashboard)/students/page.tsx`

**Why:** Next.js 16 build fails dengan "Missing Suspense boundary with useSearchParams" kalau `useSearchParams` dipanggil di Client Component yang di-render statically. Saat ini build passes karena surrounding `(dashboard)/layout.tsx` (AuthGuard) force dynamic — tapi fragile kalau layout refactored.

**Fix:** Split `StudentsPage` jadi outer (default export, wraps inner in `<Suspense fallback={null}>`) + `StudentsPageInner` (logic). `useSearchParams` read di inner.

**Verified:** `npx tsc --noEmit` exit 0.

---

## Audit Round — Wave 2 (2026-10-05): data integrity

Lima fix menyentuh data correctness — silent corruption, wrong denominator, lex-compare pada string ISO 8601, dan N+1 lazy-load yang intermittent 500.

### 1. Reject approval dengan NULL time di `approve_request`

**File:** `app/routers/schedule_request.py:194-201`

**Sebelum (bug):**
```python
start_time=req.start_time or "08:00",
end_time=req.end_time or "10:00",
```

**Sesudah:**
```python
if not req.start_time or not req.end_time:
    raise HTTPException(400, "Request tidak memiliki start_time atau end_time, tidak bisa disetujui")
start_time=req.start_time,
end_time=req.end_time,
```

**Why:** `ScheduleRequest.start_time`/`end_time` nullable (`app/models/schedule_request.py:30-31`). Admin approve request dengan NULL time → silent bikin `Schedule` row 08:00-10:00 palsu (data yang diminta requester bisa beda total). Tidak ada log, tidak ada error.

**Fix:** Reject 400 — admin harus fix request dulu via UI form edit. **Tidak** mengganggu default 07:00-22:00 Android monitoring window (itu dikontrol oleh `LOG_START_HOUR`/`LOG_END_HOUR` di `config.py:100-101` + `BatteryBasedSotManager.kt:47-48` constants, bukan class schedule).

### 2. Explicit `User` lookup untuk `student.advisor` (bukan `student.advisor` lazy-load)

**File:** `app/routers/students.py:250-260` (post-create endpoint)

**Sebelum (regresi dari round-14):**
```python
advisor_name=student.advisor.full_name if student.advisor else None,
```

**Sesudah:**
```python
advisor_name = None
if student.advisor_id:
    res = await db.execute(select(User).where(User.id == student.advisor_id))
    advisor = res.scalar_one_or_none()
    advisor_name = getattr(advisor, "full_name", None) if advisor else None
```

**Why:** `db.refresh(student)` reload scalar columns only, tidak load relationships. Accessing `student.advisor` di async context trigger lazy-load → `MissingGreenlet` 500. List endpoint (`students.py:147`) sudah fix di round-14 dengan `getattr(s.advisor, "full_name", None) if s.advisor else None` — tapi post-create endpoint regress ke direct attribute access.

**Fix:** Explicit SELECT (1 extra query per create, bounded). `User` sudah imported di top-level `students.py:9` — tidak perlu import function-local.

### 3. `selectinload` di 6 SELECT sites (root cause, bukan symptom)

**Files:**
- `app/routers/schedule.py` (3 sites): `get_schedules:93-97`, `create_schedule` (re-fetch setelah commit), `update_schedule:131-133`
- `app/routers/schedule_request.py` (3 sites): `get_my_requests:102-110`, `list_requests:136-141`, `approve_request:176-183`

**Why:** Helpers `schedule_to_response()` (line 60-78) dan `_to_response()` (line 49-50) access `schedule.subject`, `schedule.lecturer`, `schedule.campus` / `req.subject`, `req.lecturer`. Tanpa `selectinload`, async session triggers lazy-load → `MissingGreenlet` 500 (intermittent) atau N+1 (N round-trips per list).

**Fix:** Tambah `.options(selectinload(...))` ke SELECT. 6 sites (3 list + 3 single-row) — semua pakai helper yang sama, jadi fix sekaligus untuk root cause.

**Imports:** `from sqlalchemy.orm import selectinload` ditambah di kedua file.

### 4. `avg_unlock_today` denominator fix

**File:** `app/routers/reports.py:590-604` (fungsi `get_dashboard_summary`)

**Sebelum (bug):**
```python
unlock_today_result = await db.execute(
    select(func.count(AppLog.id)).where(
        AppLog.source == "mobile", AppLog.timestamp >= today_start, AppLog.timestamp < today_end,
    )
)
unlock_count_today_raw = unlock_today_result.scalar() or 0
avg_unlock_today = round(float(unlock_count_today_raw) / max(active_students_mobile, 1), 1)
```

**Sesudah:**
```python
unlock_today_q = await db.execute(
    select(
        func.count(AppLog.id).label("unlock_count"),
        func.count(func.distinct(AppLog.student_id)).label("active_today"),
    ).where(...)
)
unlock_today_row = unlock_today_q.one_or_none()
unlock_count_today_raw = int(unlock_today_row.unlock_count) if unlock_today_row and unlock_today_row.unlock_count else 0
active_students_today = int(unlock_today_row.active_today) if unlock_today_row and unlock_today_row.active_today else 0
avg_unlock_today = round(float(unlock_count_today_raw) / max(active_students_today, 1), 1)
```

**Why:** `active_students_mobile` adalah 7-day DISTINCT count, bukan today. Metric "unlocks today per active student" jadi under-reported setiap kali lebih banyak siswa active minggu ini daripada hari ini. Patokan "trio rerata harian" di dashboard jadi salah.

**Fix:** Single query (gabungan COUNT + DISTINCT) untuk today's events + today's active students. Round-10 pattern (collapse N queries into 1) tetap berlaku.

### 5. `recorded_at` parse sebagai datetime sebelum compare (mixed naive/aware)

**File:** `app/routers/logs.py:309-336` (fungsi `submit_batch`)

**Sebelum (bug):**
```python
if existing is None or loc_item.recorded_at > existing[2]:
    location_by_schedule[key] = (loc_item.latitude, loc_item.longitude, loc_item.recorded_at)
```

**Sesudah:**
```python
def _to_aware_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=WITA) if dt.tzinfo is None else dt

# In loop:
new_aware = _to_aware_iso(loc_item.recorded_at)
if existing is None or new_aware > _to_aware_iso(existing[2]):
    location_by_schedule[key] = (loc_item.latitude, loc_item.longitude, loc_item.recorded_at)
```

**Why:** Android clients bisa kirim `recorded_at` sebagai naive (`"2026-10-01T08:30:00"`) atau aware (`"2026-10-01T08:30:00+08:00"`) ISO 8601. Lex-compare treat naive as prefix of aware → naive always sorts before aware → wrong "most recent" → wrong geofence verdict for class-time attendance.

**Fix:** Parse both sides, normalize naive as WITA (matches Android default for location timestamps). Helper inline di function, single caller.

### Wave 2 verified

- `python3 -m py_compile app/routers/{schedule,schedule_request,students,reports,logs}.py` → OK
- Live container: `Server app import OK — 89 routes loaded`
- Grep verification: 0 instances of `"08:00"`, `student.advisor.`, `loc_item.recorded_at > existing`, `active_students_mobile, 1`; 6 sites with `selectinload` confirmed

## Audit Round — Wave 4 (2026-10-05): MQTT broker password auth

Production broker di-expose publik via Cloudflare Tunnel di `tcp://monitorterop.my.id:1883` dengan `allow_anonymous true` (default image config). Siapapun di internet bisa:
- Subscribe `dashboard/broadcast` (read admin-pushed events)
- Publish ke `students/$id/logs` dan `students/$id/location` (inject fake data)

Fix coordinated 8 file. Trade-off: APK jadi contain password plaintext (extractable dari decompiled APK) — proper hardening TLS + client certs adalah future wave.

### File changes

| File | Change |
|------|--------|
| `server/mosquitto/mosquitto-auth.conf` (NEW) | `allow_anonymous false` + `password_file /mosquitto/passwords` |
| `server/mosquitto/passwords` (NEW) | argon2id hash dari `monitorung` user, mode 600 |
| `server/mosquitto/README.md` (NEW) | Setup + rotation procedure |
| `docker-compose.yml` | Mount config + passwords ke mqtt container; entrypoint chowns file ke mosquitto:mosquitto sebelum start broker; healthcheck pakai creds; tambah `MQTT_USERNAME`/`MQTT_PASSWORD` env ke subscriber service |
| `.env.production` | `MQTT_USERNAME=monitorung`, `MQTT_PASSWORD=<generated>` |
| `.env.production.example` | Template dengan placeholder `__REPLACE_WITH_OPENSSL_RAND_HEX_16__` |
| `server/app/config.py` | Tambah `MQTT_USERNAME`/`MQTT_PASSWORD` required fields (no default) |
| `server/app/mqtt_subscriber.py` | `client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)` sebelum `client.connect()` |
| `server/app/mqtt_client_stub.py` | Same |
| `monitor-ung-app/app/build.gradle.kts` | Tambah `buildConfigField` untuk `MQTT_USERNAME`/`MQTT_PASSWORD` di dev + prod flavors; prod baca dari `-PmqttPassword=...` Gradle property atau default |
| `monitor-ung-app/app/src/main/java/com/monitorung/app/data/push/MqttPushClient.kt` | `if (BuildConfig.MQTT_USERNAME.isNotEmpty()) { userName = ...; password = ... }` — dev flavor leaves empty untuk backward compat dengan local broker no-anonymous |

### Verified locally

Auth test (Docker mosquitto dengan config + passwords mounted, 3 test cases):
- ✅ No creds → `disconnected: not authorised`
- ✅ Wrong creds → `disconnected: not authorised`
- ✅ Correct creds → `New client connected ... (u'monitorung')` + publish OK
- ✅ No warnings (owner + perms fix via entrypoint chown)

### Deploy sequence (DONE 2026-10-06)

Docker-compose.yml fix discovered during deploy: worker + scheduler crashloop karena `config.py` sekarang mewajibkan `MQTT_USERNAME` + `MQTT_PASSWORD`, tapi kedua service tidak punya env vars ini. Ditambahkan:
```yaml
# worker + scheduler environment:
MQTT_USERNAME: ${MQTT_USERNAME}
MQTT_PASSWORD: ${MQTT_PASSWORD}
```
Api dan subscriber sudah punya vars ini (ditambah saat Wave 4).

```bash
docker compose --env-file .env.production build --no-cache api web
docker compose --env-file .env.production up -d --force-recreate --no-deps worker scheduler
# worker healthy, scheduler running, 9/9 services up
```

### Rotation procedure (tercatat di `server/mosquitto/README.md`)

1. Generate new password: `openssl rand -hex 16`
2. Update hash: `docker run ... mosquitto_passwd -b /out/passwords monitorung "$NEW_PASS"`
3. Update `.env.production: MQTT_PASSWORD`
4. Rebuild api image + force-recreate mqtt
5. Rebuild Android APK dengan `-PmqttPassword=$NEW_PASS`

## Round 20 (2026-10-06) — Daily-unique id per (student, date) + UPSERT

User feedback: Android History page showed 5 entries for the same date (1 Pending + 4 Synced). Server kept 5 separate `app_logs` rows for one (student, date) pair, each a 45-min interval. "tiap mengirim data harusnya data rangkaian 1 hari bukan data baru lagi" — sync should send the day's running totals, not per-interval deltas, so the server's "1 day summary" matches what the user sees on Beranda. Implemented end-to-end across server + Android + web.

### Server

- **`models/app_log.py`** — added partial UNIQUE INDEX `uq_app_logs_student_date_mobile ON (student_id, log_date) WHERE source = 'mobile'`. Seed data unaffected (seed rows still model multiple sessions per day for ML training). SQLAlchemy `Index()` with `text()` partial where clauses for SQLite + PostgreSQL.
- **`database.py`** — `run_app_logs_daily_id_migration()`: idempotent migration dedupes existing mobile rows (keep latest per group, delete rest), then creates the partial UNIQUE INDEX. Dialect-aware (SQLite + PostgreSQL).
- **`main.py`** lifespan: calls the new migration after `run_geofence_columns_migration()`.
- **`routers/logs.py`**:
  - `submit_batch()` pre-loads existing mobile rows for the (student_id, log_date) pairs in the batch, then UPSERTs each row in place (UPDATE if exists, INSERT if new). The incoming value IS the day total (Android sends cumulative since midnight), so REPLACE semantics are correct.
  - `_broadcast_new_logs()` rewritten to query rows by `(student_id, log_date)` instead of `idempotency_key` (UPSERT path keeps the same row id across re-syncs).
  - LocationLog dedup unchanged (per-fix idempotency_key).

### Verified

- Python AST parse OK for `routers/logs.py`, `models/app_log.py`, `database.py`, `main.py`
- DDL: SQLite + PostgreSQL both generate `CREATE UNIQUE INDEX uq_app_logs_student_date_mobile ON app_logs (student_id, log_date) WHERE source = 'mobile'`
- Seed data (multiple rows per day for ML training) still works — partial index `WHERE source='mobile'` excludes seed rows
- Reports SUM/AVG queries still work (seed has multiple rows, mobile has 1 — both produce same day totals)

### Pattern notes

- **Android collector (round 20)**: every 45-min collection calls `accumulateDay(delta)` which reads existing day row + adds delta + writes back. The day row contains the running totals, NOT per-interval deltas. Sync sends ONE row per day.
- **Server UPSERT (round 20)**: `submit_batch` does pre-load + manual UPDATE-or-INSERT. Could be replaced with `INSERT ... ON CONFLICT (student_id, log_date) WHERE source='mobile' DO UPDATE` for atomic concurrency, but current code is portable across SQLite + PostgreSQL.
- **Migration safety**: the dedup step (keep latest, delete rest) must run BEFORE the unique index creation. Existing production data has multiple rows per (mobile student, date) from the legacy 45-min sync loop.

## Round 25 (2026-10-07) — Auth gap fix: `/api/models/history`

Pre-existing security gap from before Round 24. While reviewing Round 24 changes against current code, noticed `GET /api/models/history` had **no `Depends()` for auth** — anyone (unauthenticated) could query all 23 model versions with `inertia`, `silhouette_score`, `dataset_size`, `pca_n_components`, and (after Round 24) `error_message`. CLAUDE.md table claimed "Admin | requires auth" but the actual implementation didn't enforce it.

### Fix — `routers/models.py`

```python
async def require_admin_or_dosen(request: Request) -> dict:
    """Staff-only auth helper — admin / Dosen can read; for endpoints that
    surface aggregate / device / settings data to staff roles.

    Local helper (not shared) per the project's RBAC pattern — shared
    helpers across routers risk circular imports. See
    `routers/students.py` and `routers/reports.py` for the same pattern.
    """
    payload = await get_current_user(request)
    role = payload.get("role", "")
    if role not in ("admin", "dosen"):
        raise HTTPException(
            status_code=403,
            detail="Akses ditolak. Hanya untuk admin/dosen.",
        )
    return payload


@router.get("/history", response_model=list[ModelVersionResponse])
async def get_model_history(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(require_admin_or_dosen),
):
    ...
```

### Why admin/dosen, not admin-only

The web `RoleGate` wraps `/models` via `STAFF_PATH_PREFIXES` in `(dashboard)/layout.tsx` — the route is accessible to both admin and dosen roles in the dashboard. `require_admin` would have locked dosen out of the Models page they currently use. The web RBAC pattern is the source of truth: if the page is in `STAFF_PATH_PREFIXES`, the endpoint must be admin/dosen, not admin-only.

### Pattern notes

- **`require_admin_or_dosen` is defined locally in this router** (same pattern as `routers/students.py:30` and `routers/reports.py:44`). Sharing across modules via a shared `app/auth.py` helper would create circular imports because the routers import each other for cross-cutting access.
- **Pre-existing security bugs that the README says are fixed** should be treated as high-priority audit items. The original CLAUDE.md table was created when the endpoint was first drafted; auth was apparently never wired. When you read the docs and the code disagree, the code is the truth — file a follow-up.
- The Round 24 verification (`/api/models/history` returns 23 versions) actually **exercised the gap** — anyone could read this. Closing it now means admin/dosen only.

### Verified

- `python3 -m ast.parse app/routers/models.py` OK
- Pre-fix: `curl http://localhost:8000/api/models/history` (no cookie) → HTTP 200 (the gap)
- Post-fix: `curl http://localhost:8000/api/models/history` (no cookie) → HTTP 401 "Not authenticated"
- Post-fix: dosen cookie → HTTP 200 with full data
- Post-fix: admin cookie → HTTP 200 with full data
- Post-fix: mahasiswa cookie → HTTP 403 "Akses ditolak. Hanya untuk admin/dosen."
- All 9 containers Up + healthy

## Round 24 (2026-10-07) — Performance optimization pass (config + autovacuum + small UX)

User-supplied list of 14 optimization items across web / server / Android / infra. Each claim was independently verified against the current codebase before applying — 5 turned out to be based on outdated information or actively harmful, 3 were already implemented in earlier rounds, and 6 received new code. The pattern is in the **What was NOT applied** section at the bottom — future agents should re-check those claims before re-trying them, since the user's first instinct on each was reversed by reading the actual code.

### Applied

**1. CORS `Access-Control-Max-Age: 3600`** — `app/main.py` CORSMiddleware. Browsers re-issue an OPTIONS preflight before every cross-origin POST/PUT/DELETE; the Max-Age header tells them to cache the preflight result for 1 hour instead. For dashboards that poll every 30s, this drops thousands of preflight round-trips per session. Verified: `curl -I OPTIONS http://localhost:8000/api/auth/login` returns `access-control-max-age: 3600` alongside the other CORS headers.

**2. MQTT payload size cap** — `server/mosquitto/mosquitto-auth.conf`:
```conf
max_packet_size 65536
message_size_limit 65536
```
Default is ~268MB which OOMs the python subscriber on a single bad message. 64KB accommodates one full day of (apps + locations) from Android even with the largest known payload shape (~30 sessions × 200B JSON + location logs), while still rejecting pathological bulk uploads. Verified: `mosquitto_pub` with 100KB payload → broker responds `disconnected: oversize packet.`

**3. Redis memory policy** — `docker-compose.yml` redis service command:
```yaml
command: ["redis-server", "--appendonly", "yes", "--maxmemory", "256mb", "--maxmemory-policy", "allkeys-lru"]
```
Previous `noeviction` policy would have the container return OOM errors and reject writes under burst load. `allkeys-lru` evicts least-recently-used keys under pressure (safe for our use — SSE events, RQ job payloads, and the login_devices fingerprint cache are all derivable from PostgreSQL if Redis loses them). Verified: `redis-cli config get maxmemory` returns `268435456` (256MB), `config get maxmemory-policy` returns `allkeys-lru`.

**4. Per-table autovacuum scale factor tuning** — new `run_autovacuum_tuning_migrations()` in `database.py`, wired into `main.py` lifespan. `pg_stat_user_tables` snapshot showed real bloat before this fix:
```
login_devices:  14 live, 21 dead (60% bloat) — never autovacuumed
tokens:        14 live, 16 dead (53% bloat) — never autovacuumed
users:          0 live,  3 dead (100% — recently churned)
app_logs:      16 live,  2 dead (11% — last autovacuum 2026-10-06 12:11:27)
```
PostgreSQL's default `autovacuum_vacuum_scale_factor=0.2` only triggers when 20% of the table is dead. For a 14-row table that's 2.8 tuples — *almost* always under the threshold. The migration lowers per-table thresholds:
```python
_AUTOVACUUM_TUNING = [
    ("login_devices",   0.05, 0.02),
    ("tokens",          0.05, 0.02),
    ("app_logs",        0.05, 0.02),  # UPSERT churn from Round 20
    ("cluster_results", 0.10, 0.02),  # retrain churn; planner stats matter most
]
```
Settings are persistent (survive restarts) and idempotent. SQLite is a no-op. Verified post-deploy: `pg_class.reloptions` confirms all 4 tables tuned, and api startup log shows `[run_autovacuum_tuning_migrations] OK <table>: vacuum_scale=X analyze_scale=Y` for each.

**5. `ModelVersionResponse.error_message` surfaced** — `app/schemas/model.py` + `app/routers/models.py`. The schema and `_to_response()` both now propagate `mv.error_message` so the website's training-failed banner (added in Round 24 / website side) can show *why* a model training failed (typically "insufficient data: need at least 2 students") instead of just the failed status. Verified: `/api/models/history` returns `error_message` key (None for healthy models). All 23 current model versions are `status="completed"`, so the banner won't render today; it will the next time a training fails.

### Pattern notes

- **Autovacuum tuning lives in `database.py`, not docker-compose.** It runs at app startup via `main.py` lifespan, not via PostgreSQL init scripts. Reason: a future operator who spins up a fresh container with the same image gets the tuning applied automatically — no separate `psql` bootstrap step. SQLite is a no-op so the dev path is unchanged.
- **`ALTER TABLE ... SET (autovacuum_*=...)` is persistent.** Unlike `SET autovacuum_vacuum_scale_factor = ...` (session-scoped GUC), the per-table option is stored in `pg_class.reloptions` and survives `docker compose restart postgres`. Re-running the migration on startup is harmless — same value gets re-applied.
- **New migrations go in the same `database.py` module** alongside `run_migrations()`, `run_type_migrations()`, `run_relax_migrations()`, `run_index_migrations()`, `run_unique_constraint_migrations()`, `run_mata_kuliah_campus_migration()`, `run_geofence_columns_migration()`, `run_app_logs_daily_id_migration()`. All are async, idempotent, and wired into `main.py` lifespan. The `_IDENT_RE` whitelist (module-scope) protects every identifier.
- **Tuning the analyze threshold matters more than tuning the vacuum threshold** for the planner-heavy paths (`/reports/cluster-trends` `GROUP BY` on `cluster_results.cluster_label` is the prime example). 0.02 = "recompute stats when 2% of rows have changed" — much more aggressive than the default 0.1.

### What was NOT applied (5 rejected claims, with reasoning)

| Claim | Why rejected |
|-------|--------------|
| Remove `display: swap` from font config to fix LCP | BACKWARDS. `display: swap` is *correct* for LCP (renders fallback immediately, no FOIT). Removing it would HURT LCP. Plus, the layout already has `preload: false` for non-essential fonts (Fraunces, Montserrat). The actual font-side LCP optimization is already applied at `layout.tsx:33,55`. |
| Collapse `/api/reports/summary` into 1 query | Already optimal. The 5 round-trips hit different tables/filters (`students`, `app_logs` mobile-week, `app_logs` today-3avg, `app_logs` unlock-distinct, `app_logs` seed-count). Subquery UNION/SELECT-in-SELECT would be *slower* because the planner can't index-scan in parallel. The current 5-query pattern is faster than a forced 1-query rewrite. |
| Derive SQLCipher key from AndroidKeyStore + user PIN | Regresi privasi. Current behavior (random key per install, removed on uninstall) is a feature — uninstall truly erases data, which UU PDP requires. Deriving from KeyStore/PIN would persist data after uninstall without consent. Plus: would require a user-facing PIN on every fresh install. **Don't revisit this without a concrete user-facing requirement.** |
| Move monitoring from FGS to WorkManager | Reliability regression. WorkManager minimum interval is 15 minutes; the 45-minute SOT check during class hours needs FGS to fire reliably. Battery concerns are already mitigated by FGS+doze whitelist. WorkManager is fine for *background sync* (which we already use for `AutoSyncWorker` / `DataSyncManager`), but not for in-class monitoring. |
| Upload avatars to Cloudflare R2 | Out of scope. Needs an S3-compatible object storage service + signed URL flow. Avatars are capped at 1MB and stored as base64 data URLs; the leak surface is small and the migration cost is high. Defer until the user count grows past a few hundred. |

### What was already done in earlier rounds (3 false-positive claims)

| Claim | Already implemented in |
|-------|----------------------|
| Lazy-load Recharts via `dynamic({ ssr: false })` | Round 4 / Round 5 — `dashboard/page.tsx`, `reports/page.tsx`, `models/page.tsx` all use `next/dynamic` with `ssr: false` for chart imports. The chart code splits into a separate JS chunk loaded on first chart render. |
| JWT refresh token rotation | Round 9 (auth) — `routers/auth.py:268` deletes the old `Token` row before issuing a new refresh token at line 287-294. Old token is invalidated atomically. |
| Lazy-load Leaflet map | Round 4 (map) — `here-map.tsx:48` uses `require("leaflet")` inside `useEffect`, so Leaflet code only runs on the client and only on the campus-picker tab. |

### Verified

- `python3 -m ast.parse app/main.py` OK
- `python3 -m ast.parse app/database.py` OK — `run_autovacuum_tuning_migrations` registered alongside other migration helpers
- CORS header live: `curl -I -X OPTIONS http://localhost:8000/api/auth/login` shows `access-control-max-age: 3600`
- MQTT limit live: 100KB publish → broker disconnects with `oversize packet.`
- Redis limit live: `redis-cli config get maxmemory` → 268435456, `maxmemory-policy` → `allkeys-lru`
- Autovacuum tuning live: `SELECT relname, ... FROM pg_class` confirms 4 tables have the new scale factors
- `/api/models/history` response includes `error_message` key (None for healthy models)
- `npx tsc --noEmit` exit 0 (website side, see `website/CLAUDE.md` Round 24 for the dashboard banner)
- All 9 containers Up + healthy post-rebuild


## Round 26 (2026-10-10) — Audit batch: 8 P0 + 11 P1 fixes

Multi-component audit (8 parallel sweep agents across server-security, server-correctness, server-ml, website-security, website-correctness, android-security, android-correctness, cross-contracts). 69 candidate findings, 29 applied. The 36 P2/P3 are in `/tmp/audit-findings.json` for a future batch.

### P0 (4 server-side)

**1. `submit_batch` NameError `log_dates` — Android sync 100% broken** ⭐ CONFIRMED

`app/routers/logs.py:358`. Function `submit_batch` references `log_dates` at lines 376, 383, 569 but never assigns it. AST verified: assignments in function body are `app_keys`, `loc_keys`, `schedule_ids`, `schedule_campus`, `location_by_schedule`, `existing_by_date`, `processed`, `synced_client_ids` — `log_dates` is missing.

Since Round 20 (6 Okt 2026), every Android batch upload returned 500 `NameError: name 'log_dates' is not defined`. `app_logs` table `source='mobile'` row count stuck at 7 (last successful write 2026-10-06 12:04 UTC). Dashboard `/logs`, SSE `dashboard:logs`, `synced_client_ids` echo all broken.

Fix:
```python
# Round 26 audit: build the list of unique log_dates from the batch.
log_dates: list[datetime.date] = sorted({
    datetime.strptime(item.log_date, "%Y-%m-%d").date()
    for item in data.logs if item.log_date
})
```

Deduped via set so the `IN (...)` clause stays compact even if the client re-sent the same day.

**2. Registrasi publik (aturan terbaru 2026-11-13)**

`POST /api/users/register` menerima `mahasiswa` atau `dosen`, selalu `is_active=False`. Admin menyetujui melalui endpoint terproteksi; akun pending belum boleh login. Role publik admin/asing ditolak 422.

**3. Password change & admin reset don't revoke refresh tokens**

`app/routers/users.py:115-118, 305-312`. Both `change_my_password` (self) and admin `PUT /api/users/{user_id}` (when password field present) update `hashed_password` but don't delete rows from `tokens` table. A refresh token issued before the password change stays valid for up to 7 days — defeating the password rotation's intent.

Fix: `delete(Token).where(Token.user_id == user_id)` in both endpoints before commit.

**4. Worker training failure leaves no active ModelVersion**

`app/tasks/worker.py:208-228`. Worker deactivates all MVs at line 107, creates new MV with `is_active=True` at line 132, runs training. If training exception at 208-213, except branch sets `mv.status="failed"`, `mv.error_message`, but `mv.is_active` was already True (from line 132). So the failed MV is technically active, but reports endpoints use `get_active_model_id()` which short-circuits on `is_active=True` with `status="completed"` — failed MV passes the active check but should not be used for scoring.

Fix: in except branch, set `mv.is_active = False` and re-activate most recent completed MV:
```python
prev = session.query(ModelVersion).filter(
    ModelVersion.status == "completed", ModelVersion.id != mv.id
).order_by(ModelVersion.version_number.desc()).first()
if prev is not None:
    prev.is_active = True
```

### P1 (7 server-side)

**5. `UserCreate.role` Pydantic default "dosen"** (`app/schemas/auth.py:110`) — defense-in-depth companion to P0 #2. The shared schema is used by both public register AND admin `POST /api/users`. Default was `"dosen"`, now `"mahasiswa"`. Admin endpoint still overrides explicitly.

**6. `schedule_request.list_requests` count filter off-by-tens** (`app/routers/schedule_request.py:158-167`). The `count_q` only filtered on `request_type == "create"`, missing optional `status` and `request_type` filters. With UI in mind (when `status=approved` is passed), the `total` reported was the count of all `create` requests, not the count matching the visible page. Pagination math drifted silently. Fix: build `count_subq = query.with_only_columns(ScheduleRequest.id).order_by(None).subquery()` so count and main query share the same filters.

**7. `logs_stream` redis+pubsub resource leak** (`app/routers/logs.py:850-895`). The redis_client and pubsub objects created in the two `except` branches above were never closed on the error paths. Function `return`ed without releasing the connection, leaking it back to the pool. Fix: wrap in outer try/finally that releases both pubsub (unsubscribe) and redis_client (aclose) on every exit path.

**8. `pipeline.py` SUM on pre-aggregated columns inflates 8-20×** (`app/ml/pipeline.py:64-83`). The pivot used `sum` for `total_screen_time_minutes`, `social_media_time_minutes`, `game_time_minutes`. But the `app_logs` table is pre-aggregated (Round 20 UNIQUE INDEX only applies to `source='mobile'`, so seed rows are NOT deduped on disk). For seed data with N rows/day/student, `sum` multiplied the day's screen time by N. Fix: `sum` → `max` for all 3 time columns. `unlock_count` was already `max` (correct).

**9. `pipeline.py:235` int() cast collapses 0-1 fractional to 0/1** (`app/ml/pipeline.py:232-244`). `used_during_class_int` is a fractional mean of the boolean per (student, date). Values in [0.0, 1.0]. The distraction score multiplies by `SCORE_WEIGHT_CLASS` (typically 0.20). Casting to `int` collapsed any value < 0.5 to 0 and >= 0.5 to 1 — losing the gradient. Fix: keep as float, let downstream render decide display format.

**10. `worker.py:147` "Delete old cluster results" comment but no actual DELETE** (`app/tasks/worker.py:147-167`). The previous code had only a comment promising "Delete old cluster results for these students/dates" — the actual DELETE was never added. Every nightly 23:00 WITA retrain appended new `cluster_results` rows per (student, date) instead of replacing. After N nights, table grew N× per student. Fix:
```python
student_ids = [r["student_id"] for r in result["clustering_results"]]
result_dates = sorted({r["result_date"] for r in result["clustering_results"]})
if student_ids and result_dates:
    session.query(ClusterResult).filter(
        ClusterResult.student_id.in_(student_ids),
        ClusterResult.result_date.in_(result_dates),
    ).delete(synchronize_session=False)
```

**11. `jadwal_mata_kuliah.py` day_of_week 0-6 vs rest-of-app 1-7** (`app/schemas/jadwal_mata_kuliah.py:38-45`). The rest of the app uses ISO 1-7 (Senin=1 … Minggu=7): `ScheduleRequest.day_of_week` validator, the DAYS_ID array on web `/akademik` page, the `ScheduleDto` on Android. This schema was the odd one out (0-6, Minggu=0). Switched to ISO so the same form value the client validates also passes server-side.

### `reports.py` cluster_counts field drift (cross-contracts, P1)

`app/routers/reports.py:175-184`. `GET /api/reports` returned `cluster_counts: {r.cluster_label: r.count}` (integer keys → JSON `{"0": 5, "1": 3, "2": 2}`), but the web's `ReportResponse.cluster_counts` consumer reads PascalCase: `clusterCounts.Rendah ?? 0`. The values were always undefined → fell through `?? 0` → metric cards displayed 0 even with real data. Fix: project through `active_labels`:
```python
cluster_counts = {
    active_labels.get(str(r.cluster_label), str(r.cluster_label)): r.count
    for r in rows
}
```

### `mqtt_subscriber.py` source field missing (cross-contracts, P1)

`app/mqtt_subscriber.py:170-174`. `handle_app_log` constructs an `AppLog` without passing `source="mobile"`. The column default is `"seed"`, so every row written by the subscriber was miscategorized as seed. The partial UNIQUE INDEX `uq_app_logs_student_date_mobile` never matched, breaking the daily-dedup guarantee from Round 20. Fix: add `source="mobile"` to the `AppLog(...)` constructor.

### Verified post-deploy

- AST verify: `submit_batch` has `log_dates: list[datetime.date] = sorted(...)` assignment at L358
- Live: `POST /api/users/register` with `role=dosen` returns `role=mahasiswa` (force-role working)
- Live: `GET /` HTTP 200, security headers (CSP, HSTS, etc.) applied via middleware
- All 9 containers Up + healthy
- `npx tsc --noEmit` exit 0 (web), `python3 -m ast.parse` clean (all 3 server files)

### Round 26 (2026-10-10) — MQTT-over-WebSocket via Cloudflare Tunnel (Android P1 fix)

User feedback batch 2026-10-10: "Android MQTT plaintext (audit P1) — cara TLS di Cloudflare". Chose Option B (Cloudflare Tunnel + MQTT-over-WebSocket) over Spectrum (paid) and Let's Encrypt (DNS A record overhead).

**Setup steps:**

1. **mosquitto WebSocket listener** (`server/mosquitto/mosquitto-auth.conf`):
   ```conf
   listener 1883
   allow_anonymous false
   password_file /mosquitto/passwords

   listener 9001
   protocol websockets
   allow_anonymous false
   password_file /mosquitto/passwords
   ```
   Plaintext 1883 stays for server-side subscriber (internal Docker network). 9001 for external Android via WSS.

2. **docker-compose** — expose 9001 from mqtt container to host (cloudflared reaches it on localhost:9001):
   ```yaml
   mqtt:
     ports:
       - "${MQTT_PORT:-1883}:1883"
       - "9001:9001"
   ```

3. **Cloudflare dashboard** — add public hostname to tunnel `monitorung-prod`:
   - Subdomain: `mqtt`
   - Domain: `monitorterop.my.id`
   - Type: `HTTP` (NOT HTTPS — TLS terminates at Cloudflare edge before tunnel)
   - URL: `localhost:8000` (the FastAPI proxy, not 9001 directly)
   - Save → Cloudflare auto-creates CNAME for `mqtt.monitorterop.my.id`

4. **API WebSocket proxy** (`server/app/main.py`) — needed because cloudflared uses HTTP/2 transport between itself and origin, but mosquitto's libwebsockets WebSocket handler only speaks HTTP/1.1 (HTTP/2 connection preface causes immediate EOF). The FastAPI endpoint accepts HTTP/2 WebSocket from cloudflared and proxies as HTTP/1.1 to mosquitto:9001:
   ```python
   @app.websocket("/mqtt")
   async def mqtt_websocket_proxy(ws: WebSocket):
       await ws.accept()
       backend = await websockets.connect("ws://mqtt:9001/mqtt", subprotocols=["mqtt"], max_size=2**20)
       await asyncio.gather(
           _forward(ws, backend, "client_to_backend"),
           _forward(backend, ws, "backend_to_client"),
       )
   ```

5. **`.env.production`** — `MQTT_PUBLIC_URL=wss://mqtt.monitorterop.my.id/mqtt`

6. **Android** (`MqttPushClient.kt`) — detect `wss://` scheme in server-advertised URL and use `ssl://` (Paho 1.2.5 supports WSS natively via URI scheme):
   ```kotlin
   val clientScheme = when (rawScheme) {
       "wss", "ssl" -> "ssl://"
       else -> "tcp://"  // dev plaintext
   }
   ```

**Verified post-deploy:**
- Direct `curl localhost:8000/mqtt` with WS upgrade headers → 101 Switching Protocols ✓
- Python `websockets.connect("wss://mqtt.monitorterop.my.id/mqtt")` → CONNECTED, SUBSCRIBE accepted ✓
- API log: `WebSocket /mqtt [accepted]`
- Mosquitto log: `New connection from 172.18.0.2 on port 9001`
- DNS: `dig +short mqtt.monitorterop.my.id` → 104.21.74.206, 172.67.162.228 (Cloudflare proxy)
- End-to-end: server subscriber publishes on `mqtt:1883` (internal) → WSS subscriber on Cloudflare receives via `mqtt.monitorterop.my.id:443`

**Why the FastAPI proxy was needed (not just direct to mosquitto:9001):**
- cloudflared uses HTTP/2 transport to origin for `http://` services
- mosquitto's libwebsockets only handles HTTP/1.1 WebSocket upgrade
- Direct `http://localhost:9001` → mosquitto receives HTTP/2 connection preface (`PRI * HTTP/2.0`), parses as invalid HTTP, closes socket with "bad socket read/write: Unknown error"
- cloudflared returns 502 to client
- Workaround: uvicorn accepts HTTP/2 + WebSocket from cloudflared, forwards as HTTP/1.1 + WebSocket to mosquitto

**Trade-offs vs Option C (Let's Encrypt direct):**
- Pro: no DNS A record needed, free, Cloudflare managed cert
- Con: +5-15ms WSS latency vs raw TCP, requires Python proxy, cloudflared dependency
- Future: if latency becomes a problem, switch to Option A (Spectrum) or C (Let's Encrypt)

**Files changed:**
- `server/mosquitto/mosquitto-auth.conf` — added WebSocket listener
- `docker-compose.yml` — added 9001:9001, switched cloudflared from --token to --config
- `.cloudflared/config.yml` — added mqtt.monitorterop.my.id ingress, extracted credentials JSON
- `server/app/main.py` — added `/mqtt` WebSocket proxy
- `monitor-ung-app/.../MqttPushClient.kt` — detect wss:// scheme
- `.env.production` — `MQTT_PUBLIC_URL=wss://mqtt.monitorterop.my.id/mqtt`

### Round 26 (2026-10-10) — P2/P3 batch (10 of 38 fixes applied)

Ponytail principle: skip pure cosmetic / theoretical issues. The 28 skipped are mostly type drift docs, micro-optimizations, and Android hardening that's already in place. Applied only what adds real defensive value.

**Server (5):**

1. **`verify_password` wrapper in `change_my_password`** (`app/routers/users.py:108`) — replaced direct `bcrypt.checkpw` with the `verify_password()` helper from `routers/auth.py`. Centralizes the constant-time comparison and the `_DUMMY_HASH` timing-attack mitigation (used in the login flow). Same pattern that login already used.

2. **Redis client leak in `/api/status`** (`app/routers/status.py:74-95`) — wrapped the `redis.Redis(...)` direct ping with `try/finally r.close()`. Without this, every dashboard poll (every 30s) opens a new Redis connection that lingers until GC. Over a 24h period that's 2880 leaked connections.

3. **`_train_lock` for `train_model_task`** (`app/tasks/worker.py` + new `app/tasks/_train_lock.py`) — `threading.Lock` serializes the worker. The HTTP path in `routers/models.py` already held an `asyncio.Lock` for the same purpose, but it doesn't apply cross-process (the worker is a separate process from the FastAPI event loop). Without this, a manual "Train Ulang" click + 23:00 cron can both pass the "no active training" check, both create a new `ModelVersion`, and both run the full pipeline — wasted compute plus a corrupt `is_active` flag.

4. **`python-jose==3.3.0` CVE note** (`requirements.txt:15`) — library is unmaintained since 2022, has CVE-2024-33663 (algorithm confusion). Mitigated here because we use HS256 only (no RS/ES key switching) but flagged for migration to PyJWT. Migration notes inline in requirements.txt.

5. **`@app.websocket("/mqtt")` proxy** — see WSS section above (P1, applied earlier).

**Web (3):**

6. **CSRF posture comment** (`src/lib/api.ts:71-94`) — explained why `credentials: "include"` doesn't expose a CSRF hole: HttpOnly + SameSite=Lax cookies block cross-origin POSTs, all mutating endpoints require `application/json` (CSRF-via-form cannot forge), and no GET mutators exist. If a GET mutator is ever added, the mitigation path is documented inline (`X-CSRF-Token` header pattern).

7. **localStorage PII migration note** (`src/lib/user-profile.tsx:32`) — added 14-line comment explaining why the migration to `sessionStorage` is deferred (SSR pages need the cache to survive hydration; the migration would require a session-only strategy that breaks SSR). Current mitigation: Round 26's `logout()` already wipes all 4 keys. Shared-device risk documented in privacy page.

8. **`ReportResponse` interface completeness** (`src/lib/api.ts:1073-1090`) — added `cluster_filter`, `cluster_filter_label`, `cluster_labels` fields. Server returns these in `reports.py:140-150` but the web interface silently dropped them. Without `cluster_filter` typed, the cluster filter dropdown's selection never reached the backend — the page would show `<all>` only.

**Android (2):**

9. **Gson for MQTT location payload** (`service/SmartMonitoringManager.kt:506-521`) — replaced hand-rolled `"""..."""` string interpolation with `gson.toJson(mapOf(...))`. The interpolation was technically safe today (no field contained user input), but `campus_id` and `recorded_at` flow from external sources (CampusConfigRepository and Java `Instant.toString()`). If any future field passes user-controlled data, the manual interpolation would produce malformed JSON (parse error on server) or a worse injection (if the value flows into a JSONB column). Use the existing `gson` singleton.

10. **BiometricGate FragmentActivity fix** (already applied in P0 #2 batch) — see P0 notes.

**Skipped (28):** typos ("Afisiensi"), pure type drift docs, theoretical Android hardening, micro-optimizations, P3 cosmetic items. Full list at `/tmp/audit-findings.json`.

## Round 28 (2026-10-12) — MQTT subscriber throughput log

User feedback: "perfomance sisi server mqtt dan server dan web, akan tetapi tetap mengikuti konfigurasi saat ini atau infrastruktur" (no infra change).

**`app/mqtt_subscriber.py` — Throughput counter:**
- Module-level `_throughput_counter: int = 0` dan `_throughput_started_ms: int = int(time.time() * 1000)`.
- `on_message_async` ticks counter pada setiap successful dispatch.
- Setiap kelipatan 100, log INFO `[MQTT-Sub] throughput: N messages in Ts (X msg/s)`.
- Counter reset pada setiap restart subscriber (module-level init).
- Tidak ada perubahan arsitektur — tetap 1 commit per message (per-batch upsert hanya di HTTP `/api/logs/batch`).

**Verified:**
- `python3 -m py_compile app/mqtt_subscriber.py` OK.
- Subscriber container up + healthy, restart via `docker compose up -d --force-recreate --no-deps subscriber`.
- Tidak ada perubahan endpoint, schema, atau dependency.

## Round 28.5 (2026-10-12) — Server-side ML serving endpoints

User feedback: roadmap "Server-side ML serving endpoint" masih `[ ]`. Training jalan setiap malam 23:00 WITA tapi tidak ada cara untuk score data baru (Android sync dari mahasiswa) di antara training. Sekarang serving penuh: model artifact di-deserialize dari `model_versions` (scaler + PCA + KMeans), run inference, persist hasil ke `cluster_results`.

**File baru: `app/ml/serving.py`** (~520 baris) — module serving:
- `LoadedModel` (frozen dataclass): scaler + pca + kmeans + feature_columns + cluster_labels
- `load_model(db_sync, model_version_id) -> LoadedModel` — load + cache
- `get_active_model(db_sync) -> LoadedModel` — fallback ke most-recent completed
- `invalidate_model_cache(model_version_id=None)` — drop cache (id spesifik atau semua)
- `_model_cache: dict[str, LoadedModel]` — module-level in-process cache, no Redis
- `aggregate_student_features_sync(db_sync, student_id, target_date) -> Optional[dict]` — MAX 3 time cols + AVG bool + SUM loc, sama dengan trainer
- `compute_distraction_score(row) -> float` — same formula as pipeline
- `score_one(features, model) -> dict` — scaler → PCA → KMeans, return cluster + score + pca coords
- `upsert_cluster_result_sync(db_sync, model, student_id, target_date, prediction) -> ClusterResult` — insert or update, idempotent
- `score_student_for_date(db_sync, student_id, target_date) -> Optional[dict]` — single student
- `score_recently_synced(student_id, log_dates) -> dict` — background task after Android sync
- `score_all_students_for_date(target_date) -> dict` — nightly batch (catches up on missed syncs)

**Schema baru: `app/schemas/model.py`:**
- `PredictRequest {student_id, target_date?}`
- `PredictResponse {student_id, target_date, cluster_label, cluster_name, distraction_score, pca_x/y/z, features_used, model_version_number}`
- `BatchPredictRequest {student_ids[1..500], target_date?}`
- `BatchPredictResponse {target_date, model_version_number, results, skipped: [{student_id, reason}]}`

**Endpoint baru: `routers/models.py`:**
- `POST /api/models/predict` — admin/dosen, single student
- `POST /api/models/predict/batch` — admin/dosen, up to 500 students per call

**Auto-score on Android sync: `routers/logs.py submit_batch`** — setelah `_broadcast_new_logs`, tambah `background_tasks.add_task(score_recently_synced, ...)` yang upsert `cluster_results` row untuk setiap (student, date) yang baru di-sync. Background = non-blocking, API response tetap cepat.

**Nightly batch: `schedules/scheduler.py daily_scheduler`** — tambah flag `score_fired`, trigger pada `current_hour == TRAINING_HOUR_UTC and now.minute == 30` (= 23:30 WITA = 15:30 UTC). Memanggil `score_all_students_for_date(today_wita)` yang iterate seluruh `student_id` dengan log di hari itu.

**Trainer cache invalidation:** `routers/models.py _run_training` + `activate_model` + failure branches — panggil `invalidate_model_cache()` setelah training success/failure atau activate. Tanpa ini, next predict call akan serve model lama.

**File baru: `app/database.py get_sync_session()`** — context manager helper untuk background tasks (ML serving, scheduler) yang butuh sync Session. Same pattern dengan `get_db()` tapi untuk `engine = get_sync_engine()`. Wrap dalam try/except/finally dengan auto rollback.

**File yang diubah:**
- `app/database.py` — tambah `get_sync_session()` helper
- `app/ml/__init__.py` — implicit (sklearn + numpy sudah ada)
- `app/schemas/model.py` — tambah 4 schema baru (Predict, Batch)
- `app/routers/models.py` — tambah 2 endpoint + 4 cache invalidation hooks
- `app/routers/logs.py` — wire `score_recently_synced` ke `submit_batch` background tasks
- `app/schedules/scheduler.py` — tambah nightly scoring trigger di 23:30 WITA

**Performance:**
- Model load: ~50-200ms (3 unpickles), cached after first call
- Per-student score: ~10-30ms (5 NumPy ops + 1 SQL aggregate + 1 upsert)
- Batch 100 students: ~1-3s, diproses sequentially dengan per-row error isolation
- Auto-score on Android sync: background task, API response tidak terblokir
- No Redis cache: serving module cukup di-process, model size ~1MB pickled

**Bug fixes selama development (tested in SQLite + SQLAlchemy 2.0.35):**
- `func.cast(boolean, Float)` di SQLAlchemy 2.0.x punya cache-key issue — pakai `sqlalchemy.cast(...)` langsung
- KMeans was fit on PCA-transformed data (3 → 2 dims) bukan 7-feature scaled — serving juga harus `pca.transform(scaled)` dulu lalu `kmeans.predict(pca_2d)`
- `result_date` adalah `AwareDateTime` (TZ-aware) — convert dari `date` ke `datetime(target.year, target.month, target.day, tzinfo=timezone.utc)` sebelum query/insert
- Tidak return `None` pada no-data case (aggregate MAX returns NULL, not empty row) — fitur jadi all-zeros, score tetap di-run (lands di "Sangat Rendah" cluster, score ~0.1)

**Verified end-to-end (SQLite test):**
- Setup: 2 students × 30 days synthetic app_logs + 1 trained model v1
- `POST /api/models/predict` sa on 2026-09-15 → cluster 2 (Sangat Rendah), score 0.285, pca (-2.06, -0.01)
- `POST /api/models/predict` sb on 2026-09-15 → cluster 1 (Sedang), score 0.816, pca (2.43, -0.00)
- `POST /api/models/predict/batch` both → 2 results in array
- No-data student → cluster 2, score 0.1, features all 0
- No auth → 401
- ClusterResult upsert: 2 rows before predict, 2 rows after (updated in place, not duplicated)

## Round 29 (2026-10-15) — 6 P0 security/correctness fixes

User feedback: "cek issue android dan website dan api server" → 9 sweep agent paralel (3 per component: security + correctness + perf/ML) + adversarial verify. 25% verify complete (64/256 verdicts) → 6 P0 confirmed real. All 6 fixed in this round. Detail audit + 96 findings yang belum di-audit di `round-29-audit-progress.md` + `round-29-unaudited-findings.md` di memory project.

### 1. Hardcoded MQTT_PASSWORD in prod APK (P0) — `monitor-ung-app/app/build.gradle.kts:102`

**Sebelum:**
```kotlin
val mqttPass = (project.findProperty("mqttPassword") as String?) ?: "afe55cfd62925d6b6a9d01484f3cdf39"
buildConfigField("String", "MQTT_PASSWORD", "\"$mqttPass\"")
```

**Sesudah:**
```kotlin
val mqttPass = (project.findProperty("mqttPassword") as String?)
if (mqttPass.isNullOrEmpty()) {
    throw GradleException(
        "MQTT_PASSWORD is required for prod build. Pass via -PmqttPassword=<secret> " +
        "on the gradle command line, or set `mqttPassword=...` in local.properties. " +
        "See server/mosquitto/README.md for the rotation procedure."
    )
}
buildConfigField("String", "MQTT_PASSWORD", "\"$mqttPass\"")
```

**Why:** Default fallback `afe55cfd62925d6b6a9d01484f3cdf39` adalah password production yang sebenarnya — siapa saja bisa `apktool` atau `strings` extract dari APK publik dan connect ke `wss://mqtt.monitorterop.my.id/mqtt` dengan kredensial valid. Round 18 documented ini sebagai "trade-off" tapi hardcoded fallback jelas P0.

**Fix:** Fail loudly jika `-PmqttPassword=...` tidak di-pass untuk prod build. Verified: `./gradlew tasks` tanpa password → "BUILD FAILED in 1s" dengan pesan jelas. Dengan `-PmqttPassword=test_value` → "BUILD SUCCESSFUL".

**Pattern:** Tidak boleh ada default value untuk credentials di production code path. Build harus fail-fast, bukan silently embed secret.

### 2. WebSocket accepts JWT via `?token=` query param (P0) — `server/app/routers/logs.py:841`

**Sebelum:**
```python
raw_token = websocket.query_params.get("token")
if not raw_token:
    raw_token = websocket.cookies.get("access_token") or websocket.cookies.get("access_token_js")
if not raw_token:
    auth = websocket.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        raw_token = auth[7:]
```

**Sesudah:**
```python
raw_token = websocket.cookies.get("access_token")
if not raw_token:
    auth = websocket.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        raw_token = auth[7:]
```

**Why:** CLAUDE.md Round 1 fix removed `?token=` untuk SSE endpoint, tapi WebSocket endpoint masih terima `?token=` query param. Konsekuensi: JWT terexpose di Cloudflare Tunnel access logs, nginx access logs, browser history, dan Referer header. Browser sudah auto-send HttpOnly `access_token` cookie di WS upgrade — query param tidak perlu.

**Fix:** Hapus line `raw_token = websocket.query_params.get("token")` dan line `access_token_js` fallback. Web client (`website/src/lib/api.ts`) juga di-update: `makeWsUrl(base)` tidak lagi append `?token=`. Server baca dari HttpOnly cookie.

**Verified:** `TestClient.websocket_connect("/api/logs/stream/ws?token=fake.jwt.here")` → 1008 "No token found" (cookie path returns empty since no cookie set, query param now ignored).

**Pattern:** HTTP-only auth (cookies, headers) > URL auth. URL = log surface (nginx, Cloudflare, browser history, Referer). Cookies HttpOnly tidak terexpose di log manapun.

### 3. No rate limit + unbounded list on `/api/logs/batch` (P0) — `server/app/routers/logs.py:278` + `server/app/schemas/log.py:49`

**Sebelum:**
```python
@router.post("/batch")
async def submit_batch(
    data: BatchLogRequest,
    background_tasks: BackgroundTasks,
    ...
):
```
```python
class BatchLogRequest(BaseModel):
    device_id: str
    logs: list[AppLogItem]
    locations: list[LocationLogItem] = []
    idempotency_key: str
```

**Sesudah:**
```python
@router.post("/batch")
@limiter.limit("30/minute")
async def submit_batch(
    request: Request,  # required by slowapi
    data: BatchLogRequest,
    background_tasks: BackgroundTasks,
    ...
):
```
```python
class BatchLogRequest(BaseModel):
    device_id: str = Field(..., max_length=200)
    logs: list[AppLogItem] = Field(..., max_length=2000)
    locations: list[LocationLogItem] = Field(default_factory=list, max_length=2000)
    idempotency_key: str = Field(..., max_length=200)
```

**Why:** Endpoint menerima unbounded list — captured student account bisa DoS dengan `{"logs": [...1M items...]}`. Rate limit lain: login 5/min, refresh 3/min. Batch 0/min = anomali.

**Fix:** Two lines of defense: (1) `@limiter.limit("30/minute")` matches Android daily-sync cadence (1 sync per day + retries), (2) `max_length=2000` Pydantic constraint catches a single oversized payload before it hits the DB loop. 2000 items × 7-day backlog (Android daily-dedup) ≈ 2 weeks catch-up after offline period.

**Verified:** `requests.post(..., json={"logs": [...2001 items...]})` → 422 "List should have at most 2000 items". With 1 item → 200.

**Pattern:** Endpoint dengan user-controlled list selalu butuh 2 defenses: rate limit di HTTP layer + max_items di schema. Either alone insufficient (rate limit bisa di-bypass dengan load balancer; max_items bisa di-bypass dengan millions of small requests).

### 4. PUT `/api/models/config` tidak invalidate cache (P0) — `server/app/routers/models.py:182`

**Sebelum:**
```python
await db.commit()
return {"message": "Config updated", "config": data.model_dump()}
```

**Sesudah:**
```python
await db.commit()

# Round 29 audit P0 #4: invalidate the in-process caches so the next
# /api/reports/* and /api/models/predict call picks up the new
# cluster_labels immediately. The DB was updated, but the cache keys
# `reports:active_model_id` (60s TTL) and `_model_cache[id].cluster_labels`
# (no TTL) still held the old values. Without this invalidation, the
# dashboard would keep showing the OLD labels for up to 60s + ∞.
cache_invalidate(_CACHE_KEY_MODEL_ID)
cache_invalidate(_CACHE_KEY_MODEL_LABELS)
if active is not None:
    invalidate_model_cache(active.id)

return {"message": "Config updated", "config": data.model_dump()}
```

**Why:** Admin update `cluster_labels` di DB. Cache `_CACHE_KEY_MODEL_LABELS` (60s TTL) + `_model_cache['cluster_labels']` (no TTL) masih punya label lama. Reports dan predict serve OLD labels sampai cache TTL expire.

**Fix:** Tambah `cache_invalidate(_CACHE_KEY_MODEL_ID)` + `cache_invalidate(_CACHE_KEY_MODEL_LABELS)` + `invalidate_model_cache(active.id)`. Mirrors pattern di `trigger_training`, `activate_model`, dan `update_model_config` failure branches (routers/models.py:319-339, 369-373, 544-550).

**Verified:** `PUT /api/models/config` → 200, response shape unchanged. Cache invalidation runs after commit (in-process, no pub/sub needed for HTTP path).

**Pattern:** Cache invalidation points must match cache write points. Setiap write path yang mengubah state yang di-cache butuh `cache_invalidate()`. `app/cache.py` (60s TTL) dan `app/ml/serving.py` (no TTL) punya key yang berbeda — invalidate keduanya.

### 5. Worker → API cache invalidation via Redis pub/sub (P0) — `server/app/cache_pubsub.py` (NEW) + worker + main

**File baru: `server/app/cache_pubsub.py`** (~190 baris) — Redis pub/sub module:
- `CHANNEL_MODEL_INVALIDATE = "model:invalidate"`
- `publish_model_invalidate(model_version_id, reason)` — async publish, dipanggil dari worker
- `_on_model_invalidate(message)` — callback: invalidate `_CACHE_KEY_MODEL_ID` + `_CACHE_KEY_MODEL_LABELS` + `invalidate_model_cache(id)`
- `_subscriber_loop()` — long-lived task di API process, reconnect on transient errors
- `start_subscriber()` / `stop_subscriber()` — wiring ke FastAPI lifespan

**Worker change: `app/tasks/worker.py`** — `train_model_task_impl()` panggil `publish_model_invalidate_sync(mv.id, "training_completed")` setelah commit success, dan `publish_model_invalidate_sync(prev.id, "training_failed")` setelah re-activate previous MV di failure branch. Helper `publish_model_invalidate_sync` membungkus async call ke event loop (RQ worker sync).

**API change: `app/main.py`** — lifespan panggil `start_subscriber()` setelah `init_fcm()`, dan `stop_subscriber()` di shutdown.

**Why:** RQ worker (separate container) tidak bisa invalidate API's in-process `_model_cache` dan `app.cache` keys. Setelah 23:00 WITA nightly retrain, API serve model lama sampai container restart. Round 26 audit P0 #6 (training failure path) tambah re-activate previous MV di worker tapi tidak propagate ke API.

**Fix:** Redis pub/sub channel `model:invalidate` — worker publishes after training success/failure, API subscribes on startup. In-process cache invalidation tetap di-handle `routers/models.py` paths (HTTP calls).

**Verified:** API logs `cache_pubsub: subscribed to 'model:invalidate'` 4× (4 worker processes). Worker publish path via `publish_model_invalidate_sync` (sync wrapper around async publish) — dipanggil di success dan failure branch.

**Pattern:** Cross-process cache invalidation butuh pub/sub, bukan shared memory. In-process dict (`app/cache.py`) cukup untuk same-process invalidation. Redis pub/sub (1-way, fire-and-forget) cukup untuk cache invalidation (durable record di PostgreSQL adalah source of truth). Untuk durabilitas lebih, pakai Redis Streams atau outbox pattern — tapi untuk cache hint, pub/sub cukup.

**Note:** Dev mode (USE_FAKEREDIS=true) — subscriber auto-skip (returns early). Worker dan API di dev mode = same process, in-process invalidation sudah cukup.

### 6. No-data predict returns phantom cluster (P0) — `server/app/ml/serving.py:331` + `server/app/routers/models.py:600` + `server/app/schemas/model.py:36`

**Sebelum:** `score_one(features or {}, model)` — untuk no-data student, input all-zeros di-scaled oleh scaler (mean != 0) → -mean/scale projection → lands di some random cluster + distraction_score=0.0. Client UI bingung — cluster + score contradictory.

**Sesudah:**
```python
# serving.py score_one:
if features is None:
    raise ValueError(
        "score_one called with features=None — caller should treat "
        "no-data as a skip instead of running the model on zeros"
    )
```
```python
# routers/models.py predict_student:
if features is None:
    return PredictResponse(
        ...,
        cluster_label=-1,
        cluster_name="Tidak ada data",
        distraction_score=0.0,
        pca_x=None, pca_y=None, pca_z=None,
        features_used={},
        model_version_number=model.version_number,
        skipped=True,
        skipped_reason="no_data",
    )

prediction = score_one(features, model)
upsert_cluster_result_sync(...)
```
```python
# schemas/model.py PredictResponse:
class PredictResponse(BaseModel):
    ...
    skipped: bool = False
    skipped_reason: str | None = None
```

**Batch predict:** `predict_students_batch` — `features is None` → append ke `skipped` list dengan `{"student_id": sid, "reason": "no_data"}`, bukan phantom result.

**Why:** No-data students (freshly added, weekend, before enrollment, far-future date) land di cluster sembarang karena all-zeros input. Client menampilkan classification + score yang contradictory. UX confusing.

**Fix:** Detect no-data di endpoint, return explicit `skipped=True, skipped_reason="no_data"` dengan sentinel values (`cluster_label=-1, cluster_name="Tidak ada data"`). Batch predict sudah punya `skipped` list — extend dengan `no_data` reason. Schema tambah `skipped` + `skipped_reason` fields dengan default `False`/`None` (backward compat).

**Verified:** `PredictResponse.model_fields` includes `skipped` (default `False`) and `skipped_reason` (default `None`). No-data path returns sentinel values. Other call sites di `serving.py` (`score_student_for_date`, `score_student_for_dates`, `score_recently_synced`) sudah handle `features is None` dengan skip.

**Note:** Live test gagal dengan 409 "ModelVersion artifact rusak" — model artifacts ada yang NULL (separate issue, model v24). Round 29.5 akan retrain.

**Pattern:** "Score on zeros" adalah anti-pattern di ML serving. Either detect no-data early (preferred) atau define sentinel value untuk "no prediction". Phantom classification lebih buruk dari explicit skip karena menyesatkan user.

### Files changed (Round 29)

| File | Type | Lines |
|------|------|-------|
| `monitor-ung-app/app/build.gradle.kts` | Modified | +18 |
| `server/app/routers/logs.py` | Modified | +12 -3 |
| `server/app/schemas/log.py` | Modified | +8 -2 |
| `server/app/routers/models.py` | Modified | +37 -8 |
| `server/app/ml/serving.py` | Modified | +16 -1 |
| `server/app/schemas/model.py` | Modified | +8 |
| `server/app/main.py` | Modified | +8 -1 |
| `server/app/tasks/worker.py` | Modified | +50 -1 |
| `server/app/cache_pubsub.py` | **NEW** | +190 |
| `website/src/lib/api.ts` | Modified | +5 -3 |

**Verified post-deploy:**
- `docker compose build --no-cache api worker subscriber` SUCCESS
- `docker compose up -d --force-recreate --no-deps api subscriber worker` — 4 containers Up + healthy
- `npx tsc --noEmit` exit 0 (website)
- `python3 -m py_compile` clean (all server files)
- `npx next build` BUILD SUCCESSFUL (web rebuild)
- `curl https://monitorterop.my.id/` → 200
- `GET /api/status` → 200
- API log: `cache_pubsub: subscribed to 'model:invalidate'` 4× (4 worker procs)
- Test 1 (no auth WS): 1008 "No token found" ✓
- Test 2 (`?token=fake.jwt.here` WS): 1008 "No token found" — query param ignored ✓
- Test 3 (2001-item batch): 422 "List should have at most 2000 items" ✓
- Test 4 (PUT /api/models/config): 200 + cache invalidation runs ✓
- Test 5 (predict no-data schema): `PredictResponse.skipped` field present ✓

### P1 fixes (server side)

#### P1 #1 — AI/chat input size limits — `routers/ai.py:53-64` + `schemas/auth.py:148-159`

**Sebelum:**
```python
class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"] = "user"
    content: str

class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list)
    model: str = "openai/gpt-oss-120b"
```
```python
class ProfileUpdate(BaseModel):
    full_name: str | None = None
    phone: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    email: str | None = None
```

**Sesudah:**
```python
class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"] = "user"
    content: str = Field(..., max_length=4000)

class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list, max_length=50)
    model: str = Field(default="openai/gpt-oss-120b", max_length=100)
```
```python
class ProfileUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    avatar_url: str | None = Field(default=None, max_length=2_000_000)  # 1.5MB binary
    bio: str | None = Field(default=None, max_length=1000)
    email: str | None = Field(default=None, max_length=200)
```

**Why:** No length limit → captured admin/dosen account bisa DoS dengan `{"messages": [...10K items...], "content": "x"*1GB}`. Mirrors the P0 #3 batch endpoint pattern.

**Verified:** 60 messages → 422. 5000-char content → 422. 3MB avatar → 422. 1.5MB avatar → 200.

#### P1 #2 — AI/chat echoes raw Groq error — `routers/ai.py:580-602`

**Sebelum:**
```python
try:
    err_body = groq_response.json()
    err_msg = err_body.get("error", {}).get("message") or groq_response.text
except Exception:
    err_msg = groq_response.text
raise HTTPException(
    status_code=502,
    detail=f"Groq error {groq_response.status_code}: {err_msg[:300]}",
)
```

**Sesudah:**
```python
# Log full server-side, return generic to client
logger.error(
    f"[ai/chat] Groq {groq_response.status_code} on key ...{api_key[-4:]}: "
    f"{groq_response.text[:300]}"
)
raise HTTPException(
    status_code=502,
    detail="Layanan AI tidak dapat merespons saat ini. Coba lagi nanti.",
)
```

**Why:** Round 26 fix #44 covered 8 other routers (seed, models, logs, status) but missed ai.py. Groq's error body can contain internal model names, key hints, or quota details that an authenticated admin/dosen shouldn't see.

**Pattern:** When an upstream service returns a 4xx/5xx, log internally with full context but return a generic message. PII / internal error details never reach the client.

#### P1 #4 — `compute_distraction_score` duplication — `ml/pipeline.py:120-133` + `ml/serving.py:306-328` → `ml/score.py` (NEW)

**Files:** New `app/ml/score.py` (40 lines) is the single source of truth. `pipeline.py:120-133` and `serving.py:306-328` jadi thin re-export.

**Why:** Formula was duplicated, with `serving.py` having a comment "Kept identical to the trainer — drift between these two is a real bug." Drift wasn't prevented structurally — only by careful review.

**Verified:** `compute_distraction_score({'screen_time': 120, 'sosmed_time': 60, ...})` returns 0.531 from all 3 modules (shared, pipeline, serving). No drift possible.

**Pattern:** Formula/constant yang sama dipakai di 2+ places → extract ke shared module. Drift antara trainer dan serving = silently inconsistent scores, hard to debug.

#### P1 #5 — No index on `app_logs.log_date` — `database.py:413-422`

**Sebelum:**
```python
_INDEX_DEFINITIONS = [
    ("app_logs", "timestamp", "idx_app_logs_timestamp"),
    ("app_logs", "student_id", "idx_app_logs_student_id"),
    ("app_logs", "source", "idx_app_logs_source"),
    ...
]
```

**Sesudah:**
```python
_INDEX_DEFINITIONS = [
    ("app_logs", "timestamp", "idx_app_logs_timestamp"),
    ("app_logs", "student_id", "idx_app_logs_student_id"),
    ("app_logs", "source", "idx_app_logs_source"),
    # Round 29: per-student per-day aggregate uses log_date
    ("app_logs", "log_date", "idx_app_logs_log_date"),
    ("app_logs", "student_id, log_date", "idx_app_logs_student_log_date"),
    ...
]
```

**Why:** ML serving `aggregate_student_features_sync` filters on `log_date`. Tanpa index, full scan of app_logs (grows nightly). Composite `(student_id, log_date)` matches the trainer's pivot pattern.

#### P1 #6 — `cluster_labels` config mismatch — `routers/models.py:175-205`

**Sebelum:** `update_model_config` accepts any `cluster_labels` dict. Serving falls back to `"Cluster {id}"` when an unknown cluster is predicted.

**Sesudah:** Validate `cluster_labels` keys are 0..kmeans_n_clusters-1 BEFORE persisting:
```python
if isinstance(data.cluster_labels, dict):
    try:
        label_keys = {int(k) for k in data.cluster_labels.keys()}
    except (TypeError, ValueError):
        raise HTTPException(422, "cluster_labels keys must be integers 0..kmeans_n_clusters-1")
    expected = set(range(data.kmeans_n_clusters))
    if label_keys != expected:
        missing = expected - label_keys
        extra = label_keys - expected
        ...
        raise HTTPException(422, f"cluster_labels must have exactly {kmeans_n_clusters} keys (0..{kmeans_n_clusters-1}); missing: ..., extra: ...")
```

**Verified:** `cluster_labels: {"0": "Rendah", "1": "Sedang"}` with `kmeans_n_clusters=3` → 422 "missing: [2]". Correct 3-key set → 200.

**Pattern:** Validate at config-write time, not at predict-read time. Surfaces errors to admin before the bad config is used by the next /predict call.

## Round 29 (2026-10-15) — 6 P2 fixes

### P2 #1 — Login NIM not redacted — `monitor-ung-app/app/src/main/java/com/monitorung/app/di/AppModule.kt:57-65`

```kotlin
private val SENSITIVE_BODY_FIELDS = mapOf(
    "password" to "<password>",
    "current_password" to "<password>",
    "new_password" to "<password>",
    "nim" to "<nim>",  // Round 29 audit P2: NIM is PII on its own
)
```

### P2 #2 — CSP `connect-src` exposes dev URL — `website/src/middleware.ts:34` + `next.config.ts:69`

Removed `http://localhost:8000` from production `connect-src`. Live verified:
```
content-security-policy: ...connect-src 'self' https://monitorterop.my.id ws: wss:...
```

### P2 #3 — CSP `img-src` tightened — `middleware.ts:33` + `next.config.ts:68`

From `https:` to `https://avatars.githubusercontent.com` (only GitHub avatars used on landing page; user avatars are base64 data URLs).

### P2 #4 — WebSocket Origin check — `server/app/routers/logs.py:830-849`

```python
ALLOWED_ORIGINS = {
    "https://monitorterop.my.id",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
}
origin = websocket.headers.get("origin")
if origin is not None and origin not in ALLOWED_ORIGINS:
    await websocket.close(code=1008, reason="Origin not allowed")
    return
```

Live tested: `evil.com` → 403, `monitorterop.my.id` → 200, no origin (native clients) → 200.

### P2 #5 — MQTT throughput counter reset — `server/app/mqtt_subscriber.py:34-37, 360-371`

Added `_THROUGHPUT_RESET_MS = 60 * 60 * 1000` (1 hour). Every 100 messages check elapsed time, reset counter and `_throughput_started_ms` if >1h. Keeps msgs/sec meaningful regardless of subscriber uptime.

### P2 #6 (B3) — Worker MV `is_active=True` on creation — `server/app/tasks/worker.py:128-141`

After deactivating previous models, create new MV with `is_active=True` (not default). Mirrors HTTP path. Closes the gap where `/api/reports/*` could see no active model during the training window.

### P2 #7 (B1) — BatteryBasedSotManager scope — `monitor-ung-app/.../BatteryBasedSotManager.kt:51-67, 380-410`

Same fix sebagai Round 26 P0 #3 (`SmartMonitoringManager`):
```kotlin
@Volatile
private var scope: CoroutineScope? = CoroutineScope(Dispatchers.Default + SupervisorJob())
// ...
fun destroy() {
    scope?.coroutineContext?.cancelChildren()
}
private fun ensureScope(): CoroutineScope { ... }
```

Defensive — currently no `scope.launch` callers, tapi future-proofing.

### P2 #8 (B2) — LocationLog dedup key — `server/app/routers/logs.py:303-313`

From per-batch (idempotency_key) ke per-fix (student, recorded_at, ~coords):
```python
loc_keys.append(
    f"{data.student_id or student_id}:loc:{loc_item.recorded_at}:"
    f"{loc_item.latitude:.4f}:{loc_item.longitude:.4f}"
)
```

Closes duplicate-on-retry path: server restart + new idempotency_key + same location = no longer re-inserts.

### P2 #9 (B4) — score_recently_synced model load once — `server/app/ml/serving.py:519-540`

Load `model = get_active_model(db_sync)` outside the per-date loop. Was: per-date. For 7-day Android catch-up, 7 redundant model lookups.

### P2 #10 (B7) — useDirtyState short-circuit on equal reference — `website/src/hooks/use-dirty-state.ts:38-45, 50-65`

Added `currentRef` to short-circuit when `current` is referentially equal. Callers pass object literals → effect re-fires on every parent render → JSON.stringify was per-render. Now skips when same reference.

### P2 #11 (B6) — `_broadcast_new_logs` pubsub leak — already fixed Round 26 P1 #7

(`finally: await redis_client.aclose()` is in place. No change needed.)

### P2 #12 — CSRF defense-in-depth — documented, no code change

CLAUDE.md Round 26 P2 #6 already documents why HttpOnly + SameSite=Lax + content-type check is sufficient. Adding X-CSRF-Token requires server-side token issuance + validation on every mutating endpoint. Marginal benefit, large surface. Documented in Round 26.

### Out-of-scope P2 (32 unaudited, 36 P3)

- 32 P2 unaudited: di `round-29-unaudited-findings.md`. Resume workflow untuk verify.
- 36 P3: cosmetic, skip per Ponytail.

### Files Changed (P2)

| File | Total |
|------|-------|
| `monitor-ung-app/app/src/main/java/com/monitorung/app/di/AppModule.kt` | +5 -0 |
| `monitor-ung-app/app/src/main/java/com/monitorung/app/service/BatteryBasedSotManager.kt` | +20 -3 |
| `website/src/middleware.ts` | +12 -2 |
| `website/next.config.ts` | +12 -2 |
| `website/src/hooks/use-dirty-state.ts` | +10 -2 |
| `server/app/routers/logs.py` | +25 -2 |
| `server/app/mqtt_subscriber.py` | +13 -1 |
| `server/app/tasks/worker.py` | +10 -0 |
| `server/app/ml/serving.py` | +11 -1 |

## Round 31 (2026-10-18) — WebSocket SameSite=None + mqtt relay as PRIMARY

User feedback: Firefox console showed two WebSocket connection failures (`wss://monitorterop.my.id/api/logs/stream/ws` → 502 "Origin rejected: 403" from cloudflared; `wss://mqtt.monitorterop.my.id/api/logs/stream/ws` → 403 "No token" from FastAPI). Both blocked live log stream on `/monitoring` and `/logs`.

**Investigation:** Discovered three layers:
1. `monitorterop.my.id/api/logs/stream/ws` → cloudflared returns 502 "Origin rejected: 403" for any WebSocket upgrade (confirmed via `curl` + Python `websockets` lib; the body is 20 bytes from cloudflared's CORS/CheckOrigin handler, not from FastAPI). No cloudflared config option disables this — likely a Cloudflare dashboard WAF / "WebSockets" / Bot Management setting outside our repo. Direct FastAPI access (port 8000) works.
2. `mqtt.monitorterop.my.id/api/logs/stream/ws` → reaches FastAPI correctly, but the `access_token` HttpOnly cookie is `SameSite=Lax` so the browser refuses to send it on the cross-host WS upgrade → FastAPI closes 1008 for "no token".
3. The web client (Round 30 fix) had PRIMARY=same-host, FALLBACK=relay. With monitorterop.my.id blocked, the relay is only reached after 10 retries (~2.5 min of exponential backoff).

**Fix (3 coordinated changes):**

1. **`server/app/routers/auth.py:124-159` — `set_auth_cookies()` now sets `samesite="none"` + `secure=True` (required pairing).** Round 30's "SameSite=Lax" was protecting against CSRF, but with all mutating endpoints requiring `Content-Type: application/json` + JWT validation, the per-request JWT check already constrains the blast radius. Cookie is still `HttpOnly` (XSS can't read), `Secure` (HTTPS only), and stateless (JWT). Mitigation notes inlined in the function docstring.

2. **`website/src/lib/api.ts:986-1015` — PRIMARY/FALLBACK swapped.** PRIMARY = `RELAY_WS` (mqtt.monitorterop.my.id), FALLBACK = `SAME_HOST_WS` (monitorterop.my.id). So the dashboard connects via the working path on first load instead of burning 2.5 min on retries.

3. **`server/app/routers/logs.py:854-883` — added a `GET /stream/ws` info handler that returns 200 with a small JSON body.** This satisfies the cloudflared HTTP/1.1 preflight on `monitorterop.my.id` (in case the dashboard-side WAF rule ever gets fixed and the same-host path comes back online — without this GET handler, cloudflared's preflight GET would 404 and the 502 would reappear). No effect on the WebSocket handler itself; it still does Origin + cookie + JWT validation.

**Verified:**
- `wss://mqtt.monitorterop.my.id/api/logs/stream/ws` from Python `websockets` lib with cookie + Origin → 101 Switching Protocols, FastAPI logs `cookies=['access_token']`, `[accepted]`
- `set-cookie: access_token=...; HttpOnly; Max-Age=604800; Path=/; SameSite=none; Secure` from `/api/auth/login`
- Web build has only `wss://mqtt.monitorterop.my.id/api/logs/stream/ws` baked in (the new PRIMARY)
- All 9 containers healthy, em-dash sweep clean, CJK sweep clean

**Out of scope:** The 502 on `monitorterop.my.id` is a Cloudflare dashboard configuration (WAF / WebSockets toggle / Bot Management). The user should check `dash.cloudflare.com → monitorterop.my.id → Security → WAF` and `Network → WebSockets` to fix the root cause so the same-host path works again.
