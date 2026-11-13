# Server Backend — Sistem Monitoring Distraksi Belajar Mahasiswa

Backend API + Pipeline Data Mining (PCA + K-Means) untuk Sistem Monitoring Distraksi Belajar Mahasiswa.

## Arsitektur

```
Android App  →  FastAPI Server  →  Redis Queue  →  PostgreSQL
                    ↓
            PCA + K-Means
            (daily 23:00 WITA)
                    ↓
              Website Dashboard
```

## Prasyarat

- Python 3.11+
- PostgreSQL 14+
- Redis 6+

## Instalasi

### 1. Clone & Virtual Environment

```bash
cd server
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Setup Environment

```bash
cp .env.example .env
# Edit .env — ganti SECRET_KEY dan DATABASE_URL
```

### 3. Setup PostgreSQL

```sql
CREATE DATABASE monitoring_distraksi;
CREATE USER postgres WITH PASSWORD 'postgres';  -- atau sesuai .env
GRANT ALL PRIVILEGES ON DATABASE monitoring_distraksi TO postgres;
```

### 4. Setup Redis

```bash
sudo systemctl start redis-server   # Linux
# atau jalankan: redis-server
```

### 5. Jalankan

**Mode development (3 terminal):**

```bash
# Terminal 1: API Server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Terminal 2: RQ Worker
rq worker monitoring-worker

# Terminal 3: Scheduler (optional — bisa juga pakai cron)
python -m app.schedules.scheduler
```

**Mode production:**

```bash
# Gunicorn + Uvicorn workers
gunicorn app.main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000

# RQ Worker (jalankan di proses terpisah)
rq worker monitoring-worker &

# Scheduler
python -m app.schedules.scheduler &
```

## Endpoint API

| Method | Endpoint | Deskripsi |
|--------|----------|-----------|
| POST | `/api/auth/login` | Login, dapat access + refresh token |
| POST | `/api/auth/refresh` | Refresh access token |
| GET | `/api/campuses` | Daftar kampus (Android) |
| POST/PUT | `/api/campuses` | Kelola kampus (Admin) |
| GET/POST/PUT/DELETE | `/api/schedules` | Kelola jadwal (Mahasiswa) |
| GET | `/api/schedules/directory` | Direktori MK & Dosen (Admin) |
| POST | `/api/logs/batch` | Kirim batch log App Usage + Lokasi |
| GET | `/api/logs/sync-status` | Cek status sinkronisasi |
| GET | `/api/models/results/latest` | Hasil klaster terbaru |
| GET | `/api/models/history` | Riwayat versi model |
| PUT | `/api/models/config` | Update parameter PCA/K-Means |
| GET | `/api/reports` | Laporan rekap |

## Pipeline ML

- **Cut-off**: Setiap hari 22:00 WITA — data ditandai siap training
- **Training**: Setiap hari 23:00 WITA — PCA + K-Means dijalankan
- **Features**: screen time, sosmed/game time, unlock count, used during class, campus location
- **Output**: Cluster label (0=Rendah, 1=Sedang, 2=Tinggi), distraction score (0.0–1.0), PCA coordinates

## Struktur

```
server/
├── app/
│   ├── main.py              # FastAPI app
│   ├── config.py            # Settings
│   ├── database.py          # DB connection
│   ├── models/              # SQLAlchemy models
│   ├── schemas/             # Pydantic schemas
│   ├── routers/             # API routes
│   ├── tasks/               # RQ worker tasks
│   ├── ml/                  # PCA + K-Means pipeline
│   └── schedules/           # Cron scheduler
├── .env.example
├── requirements.txt
└── README.md
```
