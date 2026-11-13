from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache


class Settings(BaseSettings):
    # App
    APP_NAME: str = "Sistem Monitoring Distraksi"
    APP_VERSION: str = "0.29.0"
    # Security: default False. Operators must opt-in via env (DEBUG=true)
    # to enable SQL echo + non-Secure cookies. Default-on was a deployment
    # footgun — see audit finding #7.
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api"

    # Database — required; dev values only work if DATABASE_URL env is set
    DATABASE_URL: str = ""
    DATABASE_SYNC_URL: str = ""

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    USE_FAKEREDIS: bool = False

    # Campus default — fallback coordinates when no campus row exists in DB.
    # UNG Kampus Utama, Kota Gorontalo (Jl. Sudirman No. 6, Wumialo, Kota
    # Tengah). Operators can override via env to point a deployment at a
    # different institution without code changes.
    DEFAULT_CAMPUS_LAT: float = -0.5436
    DEFAULT_CAMPUS_LON: float = 123.0618

    # MQTT — broker URL for INTERNAL containers (subscriber + api). Inside the
    # Docker network, the broker is reachable as `mqtt:1883` via compose DNS.
    MQTT_BROKER_URL: str = "tcp://localhost:1883"
    # MQTT — URL advertised to EXTERNAL clients (Android). Android sits on the
    # public internet and cannot resolve the internal `mqtt` hostname; it must
    # hit the broker via the public domain. Default empty → router falls back
    # to MQTT_BROKER_URL (dev). In prod, set to e.g. `tcp://monitorterop.my.id:1883`.
    MQTT_PUBLIC_URL: str = ""
    # Round 27: MQTT fallback URL advertised to Android. Jika primary MQTT
    # (via Cloudflare tunnel → laptop mosquitto) down/tidak reachable, Android
    # otomatis fallback ke VPS mosquitto (DNS-only, dedicated server). Format
    # sama dengan MQTT_PUBLIC_URL (`wss://host/mqtt`).
    MQTT_FALLBACK_URL: str = ""
    # MQTT — credentials for both Android and server-side clients. Wave 4
    # closes the no-auth hole (mosquitto was exposed at monitorterop.my.id:1883
    # over the public internet with anonymous=true). Required at startup.
    MQTT_USERNAME: str = Field(default=..., description="MQTT broker username — must match hash in server/mosquitto/passwords")
    MQTT_PASSWORD: str = Field(default=..., description="MQTT broker password — must match hash in server/mosquitto/passwords")

    # JWT — REQUIRED; no default to prevent running with insecure key
    SECRET_KEY: str = Field(default=..., description="JWT secret key — must be set in environment")
    ALGORITHM: str = "HS256"
    # Round 10 — diperpendek dari 60 → 30 menit atas permintaan user. Masa hidup
    # yang lebih pendek mengurangi window paparan token yang dicuri; refresh
    # token (7 hari) tetap sama sehingga sesi panjang tidak terputus.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Bcrypt — rounds=12 ≈80ms (industry default, 2026)
    # rounds=13 ≈160ms, rounds=14 ≈320ms. Each +1 doubles login latency.
    # Override via env only after measuring on target hardware.
    BCRYPT_ROUNDS: int = 12

    # Groq API keys, server-side 4-key pool for AI Analisis load balance.
    # Operator mengisi minimal 1 di .env.production. Pool dipakai round-robin
    # oleh routers/ai.py. Pada rate limit (429), otomatis fallback ke key
    # berikutnya. Default kosong, AI Analisis nonaktif sampai env diisi.
    GROQ_API_KEY_1: str = ""
    GROQ_API_KEY_2: str = ""
    GROQ_API_KEY_3: str = ""
    GROQ_API_KEY_4: str = ""

    # ML Pipeline
    PCA_N_COMPONENTS: int = 2
    KMEANS_N_CLUSTERS: int = 3
    ACTIVE_MODEL_VERSION: int | None = None
    # Train/test split + K-Means reproducibility. 0.2 = 80/20 split (typical).
    # random_state=42 pins split + K-Means init so reruns produce the same
    # clusters for the same input. n_init=10 is sklearn's recommended default
    # for K-Means (runs 10 random restarts, keeps the best inertia).
    ML_TEST_SIZE: float = 0.2
    ML_RANDOM_STATE: int = 42
    ML_N_INIT: int = 10
    # Distraction level labels — comma-separated string from env
    # (DISTRACTION_LABELS=Rendah,Sedang,Tinggi) overrides this default. The
    # list is indexed by cluster mean rank at training time (lowest mean →
    # first label), so order matters. Length should be ≥ KMEANS_N_CLUSTERS;
    # extras are ignored, missing entries fall back to "Cluster {id}".
    DISTRACTION_LABELS: str = "Sangat Rendah,Rendah,Sedang,Tinggi,Sangat Tinggi,Ekstrem"

    # Distraction score formula — weights + normalization caps
    # score_time_cap: menit screen time yang dianggap 100% (8 jam)
    SCORE_SCREEN_TIME_CAP: int = 480
    # score_media_cap: menit sosmed + game yang dianggap 100%
    SCORE_MEDIA_CAP: int = 240
    # score_unlock_cap: jumlah unlock yang dianggap 100%
    SCORE_UNLOCK_CAP: int = 10
    # weight per feature
    SCORE_WEIGHT_SCREEN: float = 0.35
    SCORE_WEIGHT_MEDIA: float = 0.25
    SCORE_WEIGHT_UNLOCK: float = 0.20
    SCORE_WEIGHT_CLASS: float = 0.10
    SCORE_WEIGHT_SCHEDULE: float = 0.10
    # schedule_match_rate threshold — di bawah ini dianggap tidak sesuai jadwal
    SCORE_SCHEDULE_THRESHOLD: float = 0.5

    # Data Collection Window (WITA)
    LOG_START_HOUR: int = 7   # 07:00 WITA
    LOG_END_HOUR: int = 22     # 22:00 WITA
    CUTOFF_HOUR: int = 22      # data cutoff
    TRAINING_HOUR: int = 23     # training start

    # CORS — comma-separated list of allowed origins (no whitespace)
    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
