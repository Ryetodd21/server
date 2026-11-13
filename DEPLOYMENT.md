# Deployment Guide — Monitor UNG (Linux Native + Cloudflare Tunnel)

Panduan deploy untuk Linux native (Ubuntu 24.04 LTS). Dua dokumen panduan terkait:

- [`../DUALBOOT_UBUNTU_SETUP.md`](../DUALBOOT_UBUNTU_SETUP.md) — Install Ubuntu + Docker dari awal (dual boot, partisi, install Docker, setup nginx/cloudflared)
- [`../MIGRATION_GUIDE.md`](../MIGRATION_GUIDE.md) — Migrasi data WSL2 → Ubuntu pakai `backup.sh` + `restore.sh` (recommended untuk yang sudah punya stack running di WSL2)

## Arsitektur Production

```
[Android / Browser]
        │
        │ HTTPS
        ▼
[Cloudflare Edge] ◀─── Cloudflare Tunnel (QUIC) ───▶ [Linux Host]
                                                         │
                                                cloudflared (systemd)
                                                         │
                                                         ▼
                                               nginx (port 80)
                                               /api/* → api:8000 (Docker)
                                               /*     → web:3000 (Docker)
```

cloudflared jalan di **host** (bukan container) sebagai systemd service. nginx jalan di **host** (bukan container Docker). Docker services hanya expose port ke host network.

---

## Prasyarat

- Ubuntu 24.04 LTS (dedicated atau dual boot)
- Docker Engine terinstall (`sudo apt install docker.io`)
- User di group `docker`
- Domain `monitorterop.my.id` sudah ditambahkan ke Cloudflare

---

## Setup Docker

```bash
# Add Docker repo (recommended)
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# User ke docker group
sudo usermod -aG docker $USER
newgrp docker

# Docker daemon config (untuk DNS + host.docker.internal)
sudo tee /etc/docker/daemon.json << 'EOF'
{
  "dns": ["8.8.8.8", "1.1.1.1"]
}
EOF
sudo systemctl restart docker
```

---

## Setup Environment

```bash
cd /home/terop_ong

# Generate keys
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(64))")
POSTGRES_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")

cat > .env.production << EOF
POSTGRES_USER=monitorung
POSTGRES_DB=monitoring_distraksi
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
DATABASE_URL=postgresql+asyncpg://monitorung:$POSTGRES_PASSWORD@localhost:5432/monitoring_distraksi
DATABASE_SYNC_URL=postgresql+psycopg2://monitorung:$POSTGRES_PASSWORD@localhost:5432/monitoring_distraksi
SECRET_KEY=$SECRET_KEY
CORS_ORIGINS=https://monitorterop.my.id
NEXT_PUBLIC_API_URL=https://monitorterop.my.id
UVICORN_WORKERS=4
MQTT_BROKER_URL=tcp://host.docker.internal:1883
LOG_START_HOUR=7
LOG_END_HOUR=22
CUTOFF_HOUR=22
TRAINING_HOUR=23
EOF
```

**Note:** Tidak perlu `network: host` di build section — DNS works natively di Linux.

---

## Setup Nginx (Host)

nginx jalan di host, bukan di Docker.

```bash
sudo apt install -y nginx

sudo tee /etc/nginx/nginx.conf << 'EOF'
events {
    worker_connections 1024;
}

http {
    client_max_body_size 50M;

    upstream api_backend {
        server localhost:8000;
    }

    upstream web_backend {
        server localhost:3000;
    }

    server {
        listen 80;
        server_name _;

        location /api/ {
            proxy_pass http://api_backend;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }

        location /docs { proxy_pass http://api_backend; proxy_set_header Host $host; }
        location /openapi.json { proxy_pass http://api_backend; }
        location /health { proxy_pass http://api_backend; }
        location /api/status { proxy_pass http://api_backend; }

        location / {
            proxy_pass http://web_backend;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
        }
    }
}
EOF

sudo nginx -t
sudo systemctl enable --now nginx
```

---

## Setup Cloudflare Tunnel

### 1. Download Credentials File

> ⚠️ **PENTING:** Ini BUKAN JWT token. Wajib download credentials file dari Dashboard.

1. Buka **Cloudflare Zero Trust** → Networks → Tunnels → `monitorterop`
2. Tab **Access** → **Download credentials**
3. Simpan ke `~/.cloudflared/creds.json`
4. `chmod 600 ~/.cloudflared/creds.json`

### 2. Buat Config

```bash
mkdir -p ~/.cloudflared

cat > ~/.cloudflared/config.yml << 'EOF'
tunnel: dae0f81e-75b1-4d19-b2a5-bbdcc0ab8ded
credentials-file: /home/terio-op/.cloudflared/creds.json

ingress:
  - hostname: monitorterop.my.id
    service: http://localhost:80
    originRequest:
      noTLSVerify: true
      connectTimeout: 30s
      keepAliveConnections: 100
      keepAliveTimeout: 90s
  - service: http_status:404
EOF

chmod 644 ~/.cloudflared/config.yml
```

### 3. Install sebagai Systemd Service

```bash
# Install cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o ~/.local/bin/cloudflared
chmod +x ~/.local/bin/cloudflared

# Install service
cloudflared service install

# Start & enable
sudo systemctl start cloudflared
sudo systemctl enable cloudflared

# Cek status
sudo systemctl status cloudflared
```

---

## Build & Start Stack

```bash
cd /home/terop_ong

# Build images (Linux native — DNS works, no network:host needed)
docker compose --env-file .env.production build api web

# Start services
docker compose --env-file .env.production up -d

# Tunggu ~30 detik
sleep 30

# Verify
docker compose ps
curl http://localhost:8000/health
curl -s -o /dev/null -w "%{http_code}" http://localhost:3000
```

---

## Cloudflare SSL/TLS

Buka **Cloudflare Dashboard** → `monitorterop.my.id` → SSL/TLS:
1. Encryption mode → **Full**
2. Always Use HTTPS → **ON**

---

## Verifikasi

```bash
# Local
curl http://localhost:8000/health
curl http://localhost/api/status | python3 -m json.tool

# Via domain
curl -fsS https://monitorterop.my.id/health
curl -fsS https://monitorterop.my.id/api/status | python3 -m json.tool

# Tunnel status
sudo systemctl status cloudflared
sudo journalctl -u cloudflared --since "5 minutes ago"
```

---

## Buat User Admin Pertama

DB kosong saat awal — public register hanya boleh `mahasiswa`/`dosen`.

```bash
# Generate bcrypt hash
docker exec monitorung-api python -c "
import bcrypt
h = bcrypt.hashpw(b'StrongAdminPass123!', bcrypt.gensalt(rounds=12))
print(h.decode())
"
# Copy output hash

# Insert admin (ganti HASH dengan output di atas)
docker exec -i monitorung-postgres psql -U monitorung -d monitoring_distraksi << SQL
INSERT INTO users (email, username, full_name, hashed_password, role, is_active, created_at)
VALUES ('admin@ung.ac.id', 'admin', 'Admin UNG', 'HASH_DARI_ATAS', 'admin', true, NOW())
ON CONFLICT (email) DO NOTHING;
SQL
```

---

## Service Management

```bash
# Cek status
docker compose --env-file .env.production ps
sudo systemctl status cloudflared

# Restart
docker compose --env-file .env.production restart
sudo systemctl restart cloudflared

# Update (setelah code change)
docker compose --env-file .env.production build api web
docker compose --env-file .env.production up -d

# Stop
docker compose --env-file .env.production down
sudo systemctl stop cloudflared
```

---

## Troubleshooting

### cloudflared "Invalid tunnel secret"
Credentials file corrupt atau salah. Download ulang dari: Zero Trust → Networks → Tunnels → monitorterop → Access → Download credentials.

### Port 80/443 already in use
```bash
sudo ss -tlnp | grep -E ':80|:443'
# Stop service yang pakai port tersebut
```

### nginx 502 Bad Gateway
Pastikan api dan web container jalan:
```bash
docker compose ps
# Kalau tidak healthy, cek logs:
docker compose logs api
docker compose logs web
```

### Docker build DNS error
Pastikan `/etc/docker/daemon.json` ada dengan `"dns": ["8.8.8.8", "1.1.1.1"]` dan restart docker:
```bash
sudo systemctl restart docker
```

---

## Keamanan Checklist

- [ ] `SECRET_KEY` baru (64 char hex)
- [ ] `DEBUG=false`
- [ ] `CORS_ORIGINS=https://monitorterop.my.id` (https, bukan http)
- [ ] PostgreSQL password kuat
- [ ] SSL/TLS mode → Full
- [ ] Credentials file `~/.cloudflared/creds.json` permission 600
- [ ] Backup database harian (`pg_dump` via cron)

Panduan deploy Sistem Monitoring Distraksi UNG. Dua pilihan:
- **Bash script** (`scripts/run-server.sh`) — langsung di WSL2
- **Docker Compose** (`docker-compose.yml` di root) — Postgres + Redis + API + Worker + Scheduler + Web dalam container

## Arsitektur

```
[Mahasiswa via Android]                    [Browser admin/dosen]
        │                                          │
        │ HTTPS                                    │ HTTPS
        ▼                                          ▼
   [Cloudflare Edge / Domain] ◀── tunnel ──▶ [WSL2 host: 0.0.0.0:8000/3000]
                                                │
                                          uvicorn (multi-worker)
                                                │
                              ┌─────────────────┼─────────────────┐
                              ▼                 ▼                 ▼
                        [PostgreSQL]         [Redis]          [Scheduler]
                                              [RQ worker]
```

Tunnel (cloudflared/ngrok) handle HTTPS termination — server lokal cukup HTTP.

---

## Cara 1 — Bash script (langsung di WSL2)

### Prasyarat

Di WSL2 harus terinstall:
- Python 3.11+ (3.14 OK)
- PostgreSQL 14+ (untuk production)
- Redis 6+ (untuk production)
- `cloudflared` (jika pakai Cloudflare Tunnel) atau tunnel tool lain
- Domain yang sudah ditambahkan ke Cloudflare (untuk CF Tunnel)

```bash
# Install PostgreSQL di WSL2 (Ubuntu/Debian)
sudo apt update && sudo apt install -y postgresql redis-server

# Start services
sudo service postgresql start
sudo service redis-server start
```

## Setup database production

```bash
sudo -u postgres psql -c "CREATE DATABASE monitoring_distraksi;"
sudo -u postgres psql -c "CREATE USER monitor_user WITH PASSWORD 'STRONG_PASSWORD';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE monitoring_distraksi TO monitor_user;"
```

## Konfigurasi `.env`

```bash
cd ~/server
cp .env.example .env
# Edit .env:
```

Isi nilai production:

```bash
# Mode
MODE=prod
UVICORN_WORKERS=4
RQ_WORKERS=2

# Database (PostgreSQL)
DATABASE_URL=postgresql+asyncpg://monitor_user:STRONG_PASSWORD@localhost:5432/monitoring_distraksi
DATABASE_SYNC_URL=postgresql+psycopg2://monitor_user:STRONG_PASSWORD@localhost:5432/monitoring_distraksi

# Redis (real)
REDIS_URL=redis://localhost:6379/0
USE_FAKEREDIS=false

# JWT — WAJIB generate ulang
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
# paste output ke .env

# CORS — domain production (Cloudflare Tunnel)
CORS_ORIGINS=https://monitorterop.my.id

# Production settings
DEBUG=false
MQTT_BROKER_URL=tcp://your-mqtt-broker:1883  # atau managed MQTT
```

## Validasi prasyarat

```bash
cd ~
./scripts/check-prereqs.sh prod
```

Output harus `✓ Production prerequisites OK`. Kalau ada yang fail, fix dulu.

## Start service

```bash
cd ~
./scripts/run-server.sh start
```

Output:
```
[server] Mode: prod
[server] Validasi prasyarat production...
[server] Production validation passed
[server] Menjalankan uvicorn (mode=prod, workers=4) di 0.0.0.0:8000...
[server] uvicorn started (PID: 1234)
[server] Menjalankan scheduler (cutoff 22:00 WITA, training 23:00 WITA)...
[server] scheduler started (PID: 1235)
[server] Menjalankan RQ worker 1/2...
[server] rq worker 1 started (PID: 1236)
[server] Menjalankan RQ worker 2/2...
[server] rq worker 2 started (PID: 1237)
```

Cek:
```bash
curl http://localhost:8000/health
curl http://localhost:8000/api/status
./scripts/run-server.sh status
./scripts/run-server.sh logs uvicorn
```

## Setup Cloudflare Tunnel

### Cara A — Docker (recommended, pakai token)

Cara paling simpel: cloudflared jalan sebagai container di compose, dikasih token langsung.

#### 1. Dapatkan token

Di **Cloudflare Dashboard → Zero Trust → Networks → Tunnels**:
- Pilih tunnel `monitorterop` (Tunnel ID: `dae0f81e-75b1-4d19-b2a5-bbdcc0ab8ded`)
- Klik **Configure** → tab **Install and run**
- Pilih **Docker** → copy token (format `eyJh...`)

#### 2. Setup ingress di Cloudflare Dashboard

⚠️ **Penting**: Karena cloudflared jalan sebagai Docker container, dia reach service via Docker DNS, BUKAN `localhost`.

Di **Cloudflare Dashboard → Zero Trust → Networks → Tunnels → monitorterop → Configure → Public Hostname**:
- Subdomain: `monitorterop` (atau `@` untuk root)
- Domain: `my.id` (jadi `monitorterop.my.id`)
- Service: Type `HTTP`, URL **`http://api:8000`** (Docker service name, bukan localhost!)
- Additional settings: (kosongkan, default OK)

Simpan.

#### 3. Paste token ke `.env.production`

```bash
cd ~
nano .env.production
# Isi:
#   CLOUDFLARE_TUNNEL_TOKEN=eyJh...  (paste token dari step 1)
```

#### 4. Start tunnel (sekalian sama stack lain)

```bash
./scripts/docker-prod.sh start
```

Output akan include `monitorung-tunnel` container. Cek:

```bash
docker compose ps
docker compose logs -f cloudflared
```

Kalau sukses, log akan mirip:
```
INF Starting tunnel ...
INF Connection established connIndex=0
INF Connection established connIndex=1
INF Connection established connIndex=2
INF Connection established connIndex=3
```

#### 5. Verify dari internet

```bash
curl -fsS https://monitorterop.my.id/health
curl -fsS https://monitorterop.my.id/api/status | python3 -m json.tool
```

---

### Cara B — Cloudflared di host (alternatif)

Kalau kamu lebih suka run cloudflared langsung di WSL2 (bukan di Docker), tanpa setup DNS `api`/`localhost` confusion:

#### 1. Install cloudflared

```bash
cd /tmp
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared-linux-amd64.deb
rm cloudflared-linux-amd64.deb
cloudflared --version
```

#### 2. Run dengan token langsung (tanpa login manual)

Token sudah berisi semua credentials yang dibutuhkan, jadi tidak perlu `cloudflared tunnel login`:

```bash
nohup cloudflared tunnel --no-autoupdate run \
    --token "eyJhIjoiYmMwY2ExOWViYTgwODM0MTI5NjM4YTJlZjI3ZGYwZmIiLCJ0IjoiMzU1YzM2ODctMjI0Zi00MjVmLWE4NTUtYzUzYmE1NmNjM2FhIiwicyI6Ik9UUTBZVFUwTXprdE4yWXdPUzAwWkRsaUxXSXpZek10TWpOak5tVmtNVFZrWlRBeiJ9" \
    > ~/.cloudflared/cloudflared.log 2>&1 &
echo $! > ~/.cloudflared/cloudflared.pid
```

Atau pakai wrapper `~/.cloudflared/start-tunnel.sh` (sudah saya buat) dengan edit command-nya jadi `--token ...`.

#### 3. Setup ingress di Cloudflare Dashboard

Sama seperti Cara A, tapi URL service: **`http://localhost:8000`** (karena cloudflared ada di host, bukan di Docker).

---

### Konfigurasi Cloudflare tambahan (penting!)

Setelah tunnel jalan, di **Cloudflare Dashboard → monitorterop.my.id → SSL/TLS**:

1. **SSL/TLS encryption mode**: Set ke **Full** (atau Full Strict kalau pakai origin cert)
   - Default `Flexible` akan bypass SSL ke origin (tidak aman)

2. **Edge Certificates**: Auto-generate (default aktif)

3. **Always Use HTTPS**: ON (paksa HTTPS)

Di **Security → WAF**:
- Bot Fight Mode: ON (default)
- Security Level: Medium (default)

Di **Speed → Optimization**:
- Auto Minify: HTML, CSS, JS (recommended)
- Brotli: ON

## Verify

Test dari browser luar:

```
https://monitorung.example.com/api/status
https://monitorung.example.com/docs
```

Test dari device mobile (Android):
- Login via app → harus bisa hit API
- Cek cookie: di DevTools browser, `access_token` cookie harus `Secure` dan `HttpOnly`

## Auto-start saat WSL boot (optional)

WSL2 tidak support systemd default. Pakai crontab:

```bash
crontab -e
# Tambah:
@reboot sleep 30 && /home/USER/scripts/run-server.sh start
@reboot sleep 35 && /home/USER/scripts/run-web.sh start
@reboot sleep 40 && /usr/local/bin/cloudflared tunnel run monitorung
```

Atau pakai Windows Task Scheduler yang jalankan command di WSL:
```cmd
wsl -u USER /home/USER/scripts/run-all.sh start
```

## Monitoring & Maintenance

```bash
# Status semua service
./scripts/run-all.sh status

# Logs (tail -f)
./scripts/run-server.sh logs uvicorn
./scripts/run-server.sh logs scheduler
./scripts/run-server.sh logs rq-worker-1

# Restart
./scripts/run-all.sh restart

# Stop
./scripts/run-all.sh stop
```

## Backup database

```bash
# Manual
pg_dump -U monitor_user monitoring_distraksi > backup_$(date +%Y%m%d).sql

# Restore
psql -U monitor_user monitoring_distraksi < backup_20260922.sql
```

## Troubleshooting

### Server tidak bisa start di mode prod

Cek `./scripts/check-prereqs.sh prod` — lihat error spesifik.

### Cookie tidak secure di browser

`CORS_ORIGINS` harus pakai `https://` (bukan `http://`). `DEBUG=false` di `.env`.

### Rate limit salah target (semua request dianggap dari 1 IP)

Slowapi pakai `get_remote_address` default — di belakang tunnel, semua request datang dari IP tunnel. Solusi: custom key function baca `X-Forwarded-For`. Untuk sekarang, naikkan limit atau disable sementara.

### uvicorn restart tidak graceful

Stop script kirim SIGTERM, tunggu 5 detik, baru SIGKILL. Kalau masih tidak exit, cek `SHUTDOWN_TIMEOUT` env.

### ML pipeline training stuck

Cek scheduler log: `./scripts/run-server.sh logs scheduler`. RQ worker log: `./scripts/run-server.sh logs rq-worker-1`.

## Keamanan checklist

- [ ] `SECRET_KEY` baru (bukan dari `.env.example`), 64 char
- [ ] `DEBUG=false`
- [ ] `CORS_ORIGINS` cuma domain production (https://)
- [ ] PostgreSQL password kuat
- [ ] Redis tanpa password publik (atau pakai password + restrict ke localhost)
- [ ] Tunnel credentials file aman (`chmod 600 ~/.cloudflared/*.json`)
- [ ] `app.log` di-rotate (atau pakai logrotate)
- [ ] Backup database harian (cron + pg_dump)
- [ ] `limit_req` di Cloudflare untuk mitigasi DDoS

---

## Cara 2 — Docker Compose (reproducible)

Semua service jalan sebagai container. Cocok kalau mau gampang pindah host atau setup ulang dari nol.

### Prasyarat di WSL2

- Docker Engine terinstall (lihat [install Docker di Ubuntu/WSL2](https://docs.docker.com/engine/install/ubuntu/))
- User di group `docker` supaya tidak perlu `sudo` setiap kali:
  ```bash
  sudo usermod -aG docker $USER
  # logout-login WSL2 atau: newgrp docker
  ```
- Verifikasi:
  ```bash
  docker run hello-world
  docker compose version   # harus v2+
  ```

### Setup env production

```bash
cd ~
cp .env.production.example .env.production
nano .env.production
```

Isi:
```bash
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
POSTGRES_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
```

Edit `.env.production`:
- `SECRET_KEY=...` (paste hasil generate)
- `POSTGRES_PASSWORD=...` (paste)
- `NEXT_PUBLIC_API_URL=https://monitorung.example.com`
- `CORS_ORIGINS=https://monitorung.example.com,https://www.monitorung.example.com`

### Build & start

```bash
cd ~
docker compose --env-file .env.production up -d --build
```

Output:
```
[+] Running 7/7
 ✔ Network monitorung-net        Created
 ✔ Volume monitorung-postgres-data  Created
 ✔ Volume monitorung-redis-data     Created
 ✔ Container monitorung-postgres    Healthy
 ✔ Container monitorung-redis       Healthy
 ✔ Container monitorung-api         Started
 ✔ Container monitorung-worker      Started
 ✔ Container monitorung-scheduler   Started
 ✔ Container monitorung-web         Started
```

### Verify

```bash
docker compose ps
curl http://localhost:8000/health
curl http://localhost:8000/api/status | python3 -m json.tool
curl -I http://localhost:3000
```

### Logs

```bash
# Semua service
docker compose logs -f

# Service tertentu
docker compose logs -f api
docker compose logs -f worker
docker compose logs -f scheduler

# 100 baris terakhir
docker compose logs --tail=100 api
```

### Restart / stop / update

```bash
docker compose restart api      # restart 1 service
docker compose restart          # restart semua
docker compose stop             # stop semua (volume tetap)
docker compose down             # stop + remove container (volume tetap)
docker compose down -v          # stop + remove container + volumes (HILANGKAN DATA!)
```

### Update image setelah code change

```bash
docker compose build api web    # rebuild image yang berubah
docker compose up -d            # recreate container dengan image baru
```

### Setup Cloudflare Tunnel (sama seperti bash)

Cloudflared jalan di HOST (bukan di dalam compose), karena tunnel target ke `localhost:8000` (api) dan `localhost:3000` (web) yang di-expose oleh compose.

Lihat bagian **Setup Cloudflare Tunnel** di atas (Cara 1) — config sama persis.

### Backup database (Docker)

```bash
# Backup
docker compose exec postgres pg_dump -U monitorung monitoring_distraksi > backup_$(date +%Y%m%d).sql

# Restore
cat backup_20260922.sql | docker compose exec -T postgres psql -U monitorung monitoring_distraksi
```

### Akses database dari host (opsional)

Kalau mau pakai pgAdmin / DBeaver dari Windows untuk debug, uncomment baris `ports: ["5432:5432"]` di `postgres` service, lalu:

```bash
# Host
PGPASSWORD=$POSTGRES_PASSWORD psql -h localhost -U monitorung -d monitoring_distraksi
```

### Docker — struktur file

```
~/
├── docker-compose.yml              # Orchestration
├── .env.production                 # Secrets + config (jangan commit!)
├── .env.production.example         # Template
├── server/
│   ├── Dockerfile                  # Python image (api/worker/scheduler)
│   ├── .dockerignore
│   └── ...
└── website/
    ├── Dockerfile                  # Next.js image
    ├── .dockerignore
    └── ...
```

### Troubleshooting Docker

| Masalah | Solusi |
|---|---|
| `permission denied` untuk docker | `sudo usermod -aG docker $USER`, lalu logout-login |
| Postgres tidak healthy | Cek log: `docker compose logs postgres`. Mungkin port conflict di host |
| Web tidak bisa hit API | `NEXT_PUBLIC_API_URL` harus pakai domain (bukan `http://api:8000`) — di-inlined saat build |
| MQTT tidak reachable dari container | Default pakai `host.docker.internal`. Untuk Linux Docker, mungkin perlu `--add-host=host.docker.internal:host-gateway` atau pakai host IP |
| Container restart terus | Cek logs: `docker compose logs --tail=50 api`. Biasanya SECRET_KEY kosong / DB tidak reachable |

---

## Pilih mana?

| Situasi | Pilihan |
|---|---|
| WSL2 single host, jarang pindah, edit file sering | **Bash script** — lebih simple, edit langsung effective |
| Mau pindah host (laptop → VPS) tanpa setup manual | **Docker** — image + compose, reproducible |
| Setup untuk orang lain / tim | **Docker** — sekali build, jalan di mana pun |
| Production serius dengan HA / scaling | **Docker** + orchestration (k8s, swarm) |
| Development dengan hot reload | **Bash script** + MODE=dev |
