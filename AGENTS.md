# Memory Backend Monitor UNG

Snapshot kode: 2026-11-13. Versi API aktif: 0.29.0. Berlaku untuk `/home/terio-op/server`. Baca juga [memory proyek](../AGENTS.md); pasangan integrasi adalah [website](../website/AGENTS.md) dan [Android](../monitor-ung-app/AGENTS.md).

## Peran dan stack

Backend adalah sumber otorisasi, data akademik, log mahasiswa, model distraksi, laporan, dan konteks AI. API memakai prefix `/api`; `/` memberikan metadata aplikasi dan `/health` menyediakan pemeriksaan dasar. Jangan menganggap respons health membuktikan seluruh pipeline MQTT/ML berfungsi.

- Python: Dockerfile default 3.13; requirements menyatakan kompatibilitas 3.14. Ikuti interpreter environment yang digunakan, jangan menyimpulkan versi dari satu catatan historis.
- FastAPI 0.118.0, Uvicorn 0.32.0, Pydantic 2.13.5, pydantic-settings 2.7.1.
- SQLAlchemy 2.0.35: AsyncSession untuk HTTP; Session sync untuk pekerjaan ML/background. PostgreSQL memakai asyncpg/psycopg2, SQLite memakai aiosqlite untuk development.
- JWT HS256 melalui python-jose, bcrypt; SlowAPI menggunakan limiter bersama di `app/limiter.py`.
- Redis 5.2.1, RQ 2.0.0, fakeredis untuk mode dev; paho-mqtt 2.1+.
- pandas, NumPy, scikit-learn untuk StandardScaler/PCA/KMeans; httpx untuk layanan HTTP; Firebase Admin opsional untuk push.

## Peta kepemilikan kode

| Lokasi | Tanggung jawab |
| --- | --- |
| `app/main.py` | FastAPI lifespan, migrasi startup, CORS/Origin, router, proxy MQTT WebSocket `/mqtt`. |
| `app/config.py` | Settings environment, JWT, koneksi, default ML/skor, jam operasional, pool Groq. |
| `app/database.py` | Engine async/sync, dependency session, `AwareDateTime`, migrasi lintas PostgreSQL/SQLite. |
| `app/models/` | Tabel User/Token/LoginDevice, Student, Campus, akademik, Schedule, request, AppLog/LocationLog, ModelVersion/ClusterResult, institusi. |
| `app/schemas/` | Kontrak request/response dan validasi; jangan menurunkan batas input saat menyelaraskan DTO. |
| `app/routers/` | Auth, users, students, logs, campus, schedule, schedule_request, models, reports, ai, config, akademik, institution_settings, seed/status. |
| `app/ml/pipeline.py` | Penyusunan fitur, train/test split, scaling, PCA, clustering dan serialisasi artefak. |
| `app/ml/serving.py` | Memuat model aktif, agregasi fitur, prediksi, upsert hasil mahasiswa per tanggal. |
| `app/ml/score.py` | Rumus skor bersama trainer dan serving. |
| `app/tasks/worker.py` | RQ queue, cutoff, training terjadwal, persist hasil, invalidasi cache lintas proses. |
| `app/tasks/_train_lock*.py` | Penguncian training; baca saat mengubah pemicu manual/terjadwal. |
| `app/schedules/scheduler.py` | Loop slot cutoff/training; proses terpisah dari Uvicorn. |
| `app/mqtt_subscriber.py` | Konsumen `students/+/logs` dan `students/+/location`; DB dan publikasi event. |
| `app/cache.py`, `app/cache_pubsub.py` | Cache API dan invalidasi model antarproses. |
| `app/services/fcm.py` | Inisialisasi dan pengiriman FCM; konfigurasi service account diperlukan. |
| `scripts/`, `mosquitto/` | Pemeriksaan/seed dan konfigurasi broker; bukan sumber data untuk disalin ke memory. |

## Autentikasi dan hak akses

1. `POST /api/auth/login` memverifikasi pengguna aktif, password, dan metadata perangkat; menghasilkan access/refresh token. Web menerima cookie, Android memakai token body untuk Authorization.
2. `POST /api/auth/refresh` menangani pembaruan token. Body opsional untuk browser yang mengirim cookie HttpOnly; JSON `refresh_token` tetap diterima untuk Android. Token kosong ditolak 401. Perbaikan body wajib penyebab 422 sudah deploy 2026-11-14; regresi SQLite lokal dan image production lulus, probe sesi kosong menghasilkan 401. Default access 30 menit, refresh 7 hari; baca `config.py` dan `auth.py` untuk aturan efektif.
3. `GET /api/auth/me` memberikan identitas/claims; `GET/PUT /api/users/me` adalah profil pengguna. Jangan menganggap kedua bentuk response sama.
4. Logout dan pengelolaan perangkat berada di `auth.py`; perubahan revocation harus diperiksa pada decoder, Token, LoginDevice dan kedua client, bukan hanya penghapusan cookie.
5. `POST /api/users/register` menerima `mahasiswa` atau `dosen` dengan `is_active=False` tanpa mempercayai field aktivasi dari client. Role publik lainnya, termasuk admin, ditolak 422. Admin menyetujui melalui `PUT /api/users/{id}`; akun pending ditolak saat login/akses API.

`authenticate_access_token()` memeriksa akun aktif dan membaca role/identitas terbaru dari DB pada setiap request. Stream log hanya untuk admin/dosen; validasi diulang sebelum setiap event sehingga perubahan role, penonaktifan akun, dan expiry menghentikan pengiriman berikutnya. Access token tetap stateless terhadap logout/reset password sampai expiry; penghapusan refresh token tidak otomatis mencabut access token.

Helper `require_admin` dan `require_admin_or_dosen` umumnya lokal per router. Ikuti pola existing untuk menghindari circular import. Staff gate website bukan bukti semua endpoint dapat ditulis dosen: baca dependency masing-masing operasi, terutama pengguna, institusi dan model. Endpoint mahasiswa harus memperoleh `student_id` dari identitas login, bukan mempercayai ID kiriman client.

Cookie aktif pada `set_auth_cookies`: HttpOnly, Secure, SameSite=None, path `/`; fungsi tidak mengatur Domain. Mutasi HTTP diperiksa terhadap Origin/CORS, WebSocket punya allowlist Origin tersendiri. Jangan menyalin komentar lama yang menyebut SameSite=Lax atau menganggap `DEBUG` otomatis mematikan Secure. Pengiriman cookie ke host relay berbeda harus dibuktikan di browser; SameSite=None sendiri tidak memperluas scope domain cookie.

## Alur ingest HTTP dan MQTT

Kontrak HTTP di `app/schemas/log.py`:

- `BatchLogRequest`: `device_id`, `logs`, `locations`, `idempotency_key`; masing-masing daftar dibatasi 2000 item.
- `AppLogItem`: `client_id`, `log_date` format YYYY-MM-DD, total/sosmed/game/other dalam menit, unlock, `used_during_class`, `schedule_id` dan metadata aplikasi.
- Batas saat ini: waktu 0..1440 menit, unlock 0..200; validator memastikan masing-masing komponen tidak melebihi total. Jangan mengklaim validator sudah memeriksa jumlah semua komponen jika belum membaca implementasinya.
- `LocationLogItem`: `client_id`, `recorded_at`, koordinat, campus, flags kampus/jadwal, `schedule_id`.
- Response: `idempotency_key`, `status`, `processed_count`, `synced_client_ids`.

`submit_batch()` memakai identitas authenticated, menyiapkan lookup kampus/jadwal/lokasi, lalu meng-upsert log sumber mobile. Index parsial `uq_app_logs_student_date_schedule_mobile` mencakup `student_id`, `log_date`, `schedule_id` untuk `source='mobile'`. Periksa cabang `schedule_id=None` dan perilaku UNIQUE terhadap NULL ketika mengubah deduplikasi; jangan menganggap index menjamin semua bentuk data.

HTTP mengganti total yang dikirim, bukan menambah total kumulatif ke nilai lama. Lokasi mempunyai dedup key per mahasiswa, timestamp, koordinat yang dibulatkan. Geofence menggunakan kampus yang terhubung lewat jadwal/mata kuliah dan lokasi terkait; status `inside`, `outside`, `unknown` harus tetap berbeda.

Konflik insert HTTP batch membatalkan seluruh transaksi dan mengembalikan 409 tanpa ACK; client mempertahankan antrean untuk retry seluruh batch. Lokasi duplikat tetap mendapat ACK `client_id`, termasuk duplikat dalam satu batch.

Commit DB terjadi sebelum background broadcast dan scoring. `_broadcast_new_logs` memberi pembaruan; `score_recently_synced` menilai tanggal terdampak memakai model aktif. Kegagalan push tidak boleh ditafsirkan sebagai kegagalan persist data.

MQTT adalah proses terpisah (`python -m app.mqtt_subscriber`) yang membaca topik mahasiswa. Android kini mengirim total absolut per kelas lewat MQTT maupun HTTP. Subscriber MQTT menormalkan `log_date` ke tanggal bisnis, meng-upsert `(student_id, log_date, schedule_id)`, serta memakai hash snapshot untuk deduplikasi replay. Saat mengubah durasi, sumber data atau dedup, telusuri subscriber, batch handler, DAO Android, agregasi laporan dan fitur ML bersama-sama.

Telemetri MQTT wajib memakai envelope JSON `payload` (string JSON asli), `sent_at` (epoch detik), `signature` (HMAC-SHA256 hex lowercase). Pesan yang ditandatangani adalah `monitorung-mqtt-v1\n{topic}\n{sent_at}\n{payload}` dengan UTF-8, memakai refresh token sebagai kunci; token tidak dikirim ke broker. Subscriber memeriksa sesi refresh yang belum kedaluwarsa, akun/mahasiswa aktif, role mahasiswa, dan selisih waktu maksimal 300 detik; ID topik boleh User.id atau Student.id milik sesi yang sama, penyimpanan memakai Student.id. Profil Student harus tersedia. Pesan lama tanpa signature ditolak; HTTP batch tetap tersedia untuk antrean offline. Simulator memerlukan sesi login mahasiswa yang masih berlaku. Signature mengamankan penulisan ingest, bukan isolasi baca topik: kredensial/ACL broker masih bersama.

## Realtime dan konfigurasi client

- Kanal Redis dashboard: `dashboard:logs`; WebSocket aktif: `/api/logs/stream/ws`.
- Handler GET pada path WebSocket hanya metadata/preflight; koneksi stream membutuhkan WebSocket upgrade.
- Auth WS membaca cookie `access_token`, lalu Bearer header untuk client non-browser. JWT query parameter tidak digunakan.
- Dokumentasi historis menyebut SSE `/api/logs/stream`, `/api/logs/stream-auth`, `sse_token`; route tersebut tidak ada di router logs yang diperiksa. Jangan mendokumentasikannya sebagai transport aktif.
- `/mqtt` pada `main.py` adalah proxy frame biner ke broker `ws://mqtt:9001/mqtt`, menegosiasikan subprotocol `mqtt` dan menutup kedua arah ketika salah satu berakhir; berbeda dari stream log dashboard.
- `GET /api/config/campus` mengembalikan kampus aktif pertama atau default, radius, URL MQTT publik/fallback. `MQTT_BROKER_URL` untuk koneksi internal tidak otomatis dapat dijangkau Android.

## Model data akademik dan alur pengguna

Struktur akademik mencakup fakultas, jurusan, program studi, mata kuliah, dan jadwal mata kuliah. `Schedule` merupakan jadwal mahasiswa; `ScheduleRequest` adalah pengajuan yang disetujui/ditolak staf melalui `/schedule-requests`. `RequestAkademik` adalah alur request akademik tersendiri melalui `/request-akademik`; jangan menyatukan kedua jenis request hanya karena namanya mirip.

Perubahan field akademik bisa menjangkau router/schema/model, halaman `website/src/app/(dashboard)/akademik/page.tsx`, API web, DTO/repository/screen jadwal Android, dan lookup kampus pada geofence. Detail relasi harus dibaca dari model aktual sebelum migrasi.

## Pipeline ML dan arti hasil

1. `build_feature_dataframe()` membaca log, memakai `coalesce(log_date, timestamp)`, lalu mengelompokkan per mahasiswa/tanggal. Waktu dan unlock saat ini diagregasi `max`, flag penggunaan dalam kelas `mean`. Data lokasi memberi jumlah sampel di kampus dan rata-rata kecocokan jadwal.
2. Tujuh fitur berurutan: `screen_time`, `sosmed_time`, `game_time`, `unlock_count`, `campus_duration`, `schedule_match_rate`, `used_during_class_int`. `campus_duration` pada trainer adalah agregasi sampel lokasi, bukan pengukuran menit langsung.
3. Default split test 0.2, random state 42, n_init 10. Dataset kecil menggunakan cabang khusus; data kosong/tidak cukup harus menghasilkan error yang jelas.
4. StandardScaler di-fit pada train; PCA dan KMeans bekerja pada representasi tersebut. Simpan scaler/PCA/KMeans, metrik, konfigurasi dan label bersama versi model.
5. Label dipetakan berdasarkan urutan rata-rata screen time klaster. Label/config dapat berubah; ID mentah klaster tidak bermakna tingkat risiko tetap.
6. `serving.py` harus memakai urutan fitur, transformasi, label, dan versi aktif yang konsisten dengan trainer. Artefak pickle hanya dari sumber internal tepercaya; jangan menerima pickle pengguna.
7. Hasil disimpan pada `ClusterResult`; invalidasi cache lintas proses melalui Redis diperlukan setelah pergantian/training model.

Rumus skor ada di `ml/score.py`, terpisah dari assignment K-Means. Default: screen time cap 480 menit berbobot 0.35; sosmed+game cap 240 berbobot 0.25; unlock cap 10 berbobot 0.20; fraksi penggunaan dalam kelas berbobot 0.10; penalti kecocokan jadwal di bawah 0.5 sebesar 0.10. Hasil dibatasi maksimum 1 dan dibulatkan tiga desimal. Environment dapat mengubah parameter; jangan menduplikasi rumus di UI.

Manual training melalui `/api/models/train`; history/config/results/predict/activate berada di `routers/models.py`. Manual training dan RQ worker mempunyai jalur orkestrasi masing-masing; baca keduanya jika mengubah persist status/model.

Scheduler memeriksa tiap 60 detik, memakai slot `>=` untuk mengejar waktu terlewat, reset flag berdasarkan tanggal UTC. Meskipun komentar menyebut scoring 23:30, kode snapshot ini memanggil scoring pada iterasi saat training baru diantrekan, bukan menunggu 30 menit atau keberhasilan job. Jangan menjanjikan urutan selesai-training-lalu-score tanpa memperbaiki dan mengujinya.

Flag cutoff/training hanya disetel setelah enqueue berhasil; gangguan Redis dicoba ulang pada tick berikutnya.

Laporan `/api/reports` mengembalikan `avg_distraction_score` dari AVG skor tersimpan pada model aktif, rentang tanggal, dan filter ID klaster. Tidak ada skor menghasilkan null, bukan nol. `/api/reports/cluster-trends` menyertakan `cluster_counts` berlabel dinamis dan rerata skor berbobot jumlah skor non-null dalam bucket; ID klaster tidak digunakan sebagai urutan risiko.

## AI dan push

`routers/ai.py` mengatur konteks, chat, daftar model dan pool Groq. Key berasal dari `GROQ_API_KEY_1` sampai `GROQ_API_KEY_4` di server; default model kode adalah `openai/gpt-oss-120b` melalui Groq. Jangan meminta key Groq dari browser berdasarkan catatan lama. Tanpa key terkonfigurasi fitur AI dapat mengembalikan 503.

FCM memerlukan Firebase service account melalui konfigurasi server dan registrasi token perangkat. Kehadiran SDK tidak membuktikan push siap produksi. Hindari mencatat token perangkat atau konteks mahasiswa yang sensitif.

## Menjalankan dan memeriksa

Semua perintah Python berikut dari `server/`, setelah virtualenv/dependency dan environment disiapkan pada DB development terpisah:

```bash
source venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
# Proses terpisah, gunakan Redis yang sama dengan konfigurasi API:
rq worker monitoring --url redis://localhost:6379/0
python -m app.schedules.scheduler
python -m app.mqtt_subscriber
```

Nama queue benar adalah `monitoring`, bukan `monitoring-worker` dari README lama. `USE_FAKEREDIS=true` menjalankan enqueue via thread in-process; tidak membuktikan pub/sub atau antrean lintas proses. Settings memerlukan `SECRET_KEY`, `MQTT_USERNAME`, `MQTT_PASSWORD`, URL DB async/sync yang sesuai. Jangan mencetak nilainya.

`./run.sh status` meneruskan ke `../scripts/run-server.sh`; mode start mengelola proses. Startup FastAPI memanggil migrasi dan cleanup DB: pilih DB disposable sebelum smoke test.

Pemeriksaan yang tersedia:

- `python -m scripts.backend_regression_check`: fixture sintetis SQLite in-memory, tanpa `.env`/lifespan API; password, otorisasi HTTP/WS, rollback/ACK batch, signature MQTT, proxy biner, retry scheduler. `MONITORUNG_CHECK_DATABASE_URL` opsional hanya untuk DB disposable bernama `monitorung_regression`; jangan arahkan ke DB aktif. Pemeriksaan juga telah dijalankan pada PostgreSQL 16 disposable.
- `python -m scripts.scheduler_slot_check`: loop scheduler dengan fake clock/enqueue, setelah environment development valid tersedia.
- `python -m scripts.ml_pipeline_smoke_test`: pemeriksaan timezone/COALESCE/agregasi SQLite in-memory; masih mengimpor konfigurasi aplikasi.
- `scripts/smoke_test.py` lama menggunakan kontrak login `username` dan menulis batch; jangan jalankan terhadap data aktif atau anggap kompatibel tanpa pembaruan.
- Untuk kontrak baru, tambah satu regression check terfokus pada input, otorisasi, dedup, waktu atau agregasi yang berubah. Perubahan DB memerlukan pemeriksaan SQLite dan PostgreSQL bila memakai fitur spesifik dialek.

## PostgreSQL VPS 2026-11-14

Production VPS `213.163.207.199` memakai PostgreSQL **18.7** (`postgres:18.7-alpine`), override `/opt/monitorung/compose.vps.yml`; compose dasar/lokal tetap 16. Migrasi logical dump/restore ke volume `monitorung-postgres18-data` dengan PGDATA `/var/lib/postgresql/18/docker`. Regresi backend pada DB PostgreSQL 18 disposable lulus; fingerprint seluruh tabel/sequence cocok setelah restore final, ANALYZE dan koneksi asyncpg/psycopg2 lulus. Delapan layanan healthy. Backup serta konfigurasi sebelumnya tersimpan privat di `/opt/monitorung/releases/postgres18/`, volume PostgreSQL 16 dipertahankan. Rollback setelah ada penulisan baru membutuhkan rekonsiliasi agar tidak kehilangan data.

## Managed PostgreSQL UpCloud 2026-11-14

Status production terbaru menggantikan database container VPS: **UpCloud PostgreSQL 18.7**, database `defaultdb`, melalui private network. `/opt/monitorung/.env.production` menyimpan `UPCLOUD_DATABASE_URL` dan `UPCLOUD_DATABASE_SYNC_URL`; Compose override menerapkannya pada API/worker/scheduler/subscriber dengan `PGSSLMODE=require`. URL tanpa parameter query SSL menjaga kompatibilitas asyncpg dan helper `get_sync_engine()` yang menurunkan URL dari async; kedua driver terbukti menggunakan TLS di keempat proses. TLS require mengenkripsi, belum validasi CA/hostname verify-full.

Regresi backend pada DB disposable UpCloud lulus. Dump final setelah penghentian writer, restore, fingerprint seluruh tabel/sequence dan ANALYZE lulus. Container postgres lokal berhenti, profile `local-db-rollback`; volume dan backup privat `/opt/monitorung/releases/upcloud/` dipertahankan. Jangan rollback ke data lokal lama setelah ada penulisan di UpCloud tanpa sinkronisasi balik. Website/API 200 dan auth tanpa sesi 401; login nyata dan training telah diuji pengguna (konfirmasi 2026-11-14; bukan pengujian ulang oleh agen). Kredensial yang pernah masuk chat harus dirotasi di penyedia lalu URL runtime diperbarui, tanpa menyalin secret ke memory/log.

## Batas dan pemeliharaan memory

Sumber utama: file pada tabel kepemilikan, `requirements.txt`, `Dockerfile`, `../docker-compose.yml`. `README.md`, `DEPLOYMENT.md`, dan `CLAUDE.md` membantu konteks historis tetapi memuat detail usang. Memory ini tidak menyatakan API, broker, DB, worker, training atau AI sudah diuji berjalan. Perbarui bagian terkait setiap kali kontrak, role, versi, agregasi atau alur berubah.
