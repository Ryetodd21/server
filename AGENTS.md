# Memory Backend Monitor UNG

Terakhir dicocokkan: 2026-11-17. Berlaku untuk `/home/terio-op/server`. Baca juga [memory proyek](../AGENTS.md); pasangan integrasi [website](../website/AGENTS.md) dan [Android](../monitor-ung-app/AGENTS.md).

## Peran dan stack

Sumber otorisasi, data akademik, log mahasiswa, model distraksi, laporan, dan konteks AI. Prefix `/api`; `/` metadata, `/health` pemeriksaan dasar (tidak membuktikan pipeline MQTT/ML).

- Python **3.14.10** di semua lingkungan: Dockerfile (`ARG PYTHON_VERSION`), CI (`.github/workflows/ci.yml`), dan venv dev lokal. `requirements.txt` tidak memaku versi Python; satu pin `sqlalchemy==2.0.58` dipakai bersama (mendukung 3.11–3.15). Selisih versi Python antar-lingkungan **tidak** boleh diatasi dengan pin SQLAlchemy berbeda — itu yang dulu membuat bug khusus 3.14 lolos CI. **Kenapa 3.14.10 dan bukan 3.14.4:** `ubuntu-latest` sudah Ubuntu 26.04, sedangkan manifest `actions/python-versions` hanya punya build 26.04 untuk 3.14.6+; dengan 3.14.4 `setup-python` gagal `version not found for Ubuntu 26.04`. Base Python lokal: `/home/terio-op/.local/share/python/3.14.10` (python-build-standalone, permanen — **jangan** taruh di `/tmp`, tmpfs hilang saat reboot).
- FastAPI, Uvicorn, Pydantic 2, pydantic-settings.
- SQLAlchemy 2 (`2.0.58`): AsyncSession untuk HTTP, Session sync untuk ML/background. PostgreSQL (asyncpg/psycopg2), SQLite (aiosqlite) untuk dev. **Jangan turunkan ke < 2.0.44**: 2.0.35 dan sebelumnya memanggil `typing.Union.__getitem__(tuple)` di `sqlalchemy/util/typing.py::make_union_type`, yang gagal di Python 3.14 dengan `TypeError: descriptor '__getitem__' requires a 'typing.Union' object` — setiap model `Mapped[...]` gagal dideklarasikan, jadi seluruh app tidak bisa di-import. Jalur 2.1.x sengaja tidak dipakai (baru ~2 bulan, rilis cepat).
- JWT HS256 (PyJWT 2.15.1), bcrypt, SlowAPI (`app/limiter.py`).
- Redis, RQ 2 (`monitoring` queue), fakeredis untuk dev; paho-mqtt 2.1+.
- pandas/NumPy/scikit-learn (StandardScaler/PCA/KMeans), httpx, Firebase Admin opsional.

## Peta kepemilikan kode

| Lokasi | Tanggung jawab |
| --- | --- |
| `app/main.py` | Lifespan, migrasi startup, CORS/Origin, router, proxy MQTT `/mqtt`. |
| `app/config.py` | Settings env, JWT, koneksi, default ML/skor, jam operasional, pool Groq. |
| `app/database.py` | Engine async/sync, `get_db`, `AwareDateTime`, migrasi lintas dialek. |
| `app/academic_scope.py` | Scope akademik per request: `resolve_programs`, `install_scope`, `normalize_academic_name`, `match_program`, `match_department`, `stream_program_ids`. |
| `app/models/` | User/Token/LoginDevice, Student, Campus, Fakultas/Jurusan/ProgramStudi, MataKuliah/JadwalMataKuliah, Schedule, ScheduleRequest, AppLog/LocationLog, ModelVersion/ClusterResult. |
| `app/schemas/` | Kontrak request/response dan validasi. Jangan turunkan batas input saat menyelaraskan DTO. |
| `app/routers/` | auth, users, students, logs, campus, schedule, schedule_request, jadwal_mata_kuliah, mata_kuliah, fakultas, jurusan, program_studi, models, reports, ai, config, institution_settings, status. |
| `app/ml/pipeline.py` | Fitur, split, scaling, PCA, clustering, serialisasi artefak. |
| `app/ml/serving.py` | Model aktif, agregasi fitur, prediksi, upsert hasil per tanggal. |
| `app/ml/score.py` | Rumus skor bersama trainer dan serving. |
| `app/tasks/worker.py` | RQ queue, cutoff, training terjadwal, persist, invalidasi cache. |
| `app/schedules/scheduler.py` | Loop slot cutoff/training; proses terpisah dari Uvicorn. |
| `app/mqtt_subscriber.py` | Konsumen `students/+/logs` dan `students/+/location`. |
| `app/cache.py`, `app/cache_pubsub.py` | Cache API dan invalidasi antarproses. |
| `app/services/fcm.py` | FCM; butuh service account. |
| `scripts/` | `backend_regression_check.py`, `scheduler_slot_check.py`, `ml_pipeline_smoke_test.py`, seed. |
| `tests/` | Pytest gate CI: startup, `/health`, kontrak tipe token JWT. Env uji di `tests/conftest.py`, `pytest.ini` menambah root ke `pythonpath`. |

## Autentikasi dan hak akses

1. `POST /api/auth/login` memverifikasi user aktif + password + metadata perangkat. Web menerima cookie; Android memakai token di body. Login menerima email, username, atau NIM (fallback join ke `students.nim` setelah email/username gagal).
2. `POST /api/auth/refresh` body opsional untuk browser (cookie); JSON `refresh_token` untuk Android. Token kosong → 401. Default access 30 menit, refresh 7 hari.
3. `GET /api/auth/me` = claims; `GET/PUT /api/users/me` = profil. Bentuk response berbeda.
4. `POST /api/users/register` hanya `mahasiswa`/`dosen`, selalu `is_active=False`; admin menyetujui via `PUT /api/users/{id}`.
5. `authenticate_access_token()` memeriksa akun aktif dan membaca role terbaru dari DB tiap request. Access token stateless terhadap logout sampai expiry.

`require_admin` / `require_admin_or_dosen` lokal per router (hindari circular import). Staff gate website bukan bukti endpoint boleh ditulis dosen; baca dependency router.

## Scope akademik

`configure_request_scope()` dipanggil dari `get_db()` untuk path di `prefixes` (`/students`, `/reports`, `/models`, `/logs`, `/ai`, `/mata-kuliah`, `/jadwal-mata-kuliah`, `/schedules`, `/schedule-requests`, `/request-akademik`, `/program-studi`, `/jurusan`, `/fakultas`, `/users`).

- Filter `x-jurusan-id` / `x-program-studi-id` (atau query param) **hanya mempersempit GET/HEAD**.
- `install_scope()` memasang `with_loader_criteria` per model. `skip_academic_scope` execution option melewati filter untuk query master internal.
- **Exact match wajib.** `normalize_academic_name()` = casefold + buang non-alfanumerik + rapikan spasi. `match_program()` / `match_department()` hanya membandingkan `id`/`code`/`name` persis. Substring matching pernah membuat kode prodi `SI` cocok ke "Pendidikan Teknologi Informasi" dan membocorkan data antar-jurusan. Jangan dikembalikan.
- `resolve_programs()`: admin tanpa filter → `None` (tanpa batas); dosen dari `payload.jurusan`; mahasiswa mencoba `students.program_studi` lalu `users.jurusan`. Tidak cocok → daftar kosong (sengaja tidak memberi baris).
- `stream_program_ids()` memetakan `students.id` → `program_studi_id` untuk event `dashboard:logs`, memakai exact match yang sama.

## Ingest, realtime, dan konfigurasi

- `POST /api/logs/batch` menerima log + lokasi dengan `client_id`/`idempotency_key`, mengembalikan `synced_client_ids`. Konflik → 409 tanpa ACK agar seluruh batch di-retry; lokasi yang sudah tersimpan tetap diakui.
- MQTT memakai envelope HMAC per sesi: `monitorung-mqtt-v1\n{topic}\n{sent_at}\n{payload}`, kunci refresh token. Pesan tanpa signature, sesi dicabut, atau selisih jam >300 detik ditolak.
- Ingest MQTT: `asyncio.Queue(maxsize=1000)` + 15 worker tetap; thread paho diblokir maks 5 detik saat backlog penuh (PUBACK baru dikirim setelah `on_message`, jadi broker retry). Log `MQTT backlog penuh` = ingest macet, bukan pesan hilang normal.
- Redis pub/sub `dashboard:logs` → WebSocket `/api/logs/stream/ws`. Stream hanya admin/dosen; validasi ulang sebelum tiap event. Event membawa `program_studi_id` untuk filter klien.
- WebSocket memakai `pubsub.get_message(timeout=30)` + `{"event":"ping"}` tiap 30 detik (deteksi klien half-open). Klien Redis singleton per proses (`_make_redis()` di `routers/logs.py`); yang ditutup hanya `pubsub`, bukan client.
- `/api/config/campus` memberi konfigurasi kampus/geofence dan alamat MQTT ke Android.

## Akademik dan jadwal

Hierarki: **Fakultas → Jurusan → ProgramStudi → MataKuliah → JadwalMataKuliah**. Master jadwal disalin ke `schedules` per mahasiswa.

`sync_master_schedules()` (dipanggil `GET /api/schedules`):
1. Resolve **satu** prodi dengan `match_program()` dari `students.program_studi` lalu `users.jurusan`. Tidak cocok → `return` tanpa menulis baris.
2. Filter `ProgramStudi.id == prodi_val` (exact). `ilike '%...%'` sudah dihapus.
3. Rakit seluruh baris dulu, lalu **satu** UPSERT batch; hanya menulis bila ada perubahan. Baris basi dinonaktifkan lewat satu `UPDATE ... WHERE id IN`.
4. ID memakai UUIDv5 `(students.id, jadwal_id)` agar stabil; jadwal personal UUIDv4 lama dipertahankan.

`_get_or_provision_student()` membuat baris `Student` untuk user yang belum punya, supaya pendaftaran baru tetap bisa melihat jadwal.

`mata_kuliah._get_or_create_subject()` menjembatani `mata_kuliah` → `subjects` agar dropdown Android (`GET /api/schedules/directory`) sinkron.

### Approve request jadwal = promosi ke jadwal prodi

`POST /api/schedule-requests/{id}/approve` tidak hanya membuat baris `schedules` pribadi. `_promote_request_to_master()` (`routers/schedule_request.py`) juga membuat/mereuse `mata_kuliah` + `jadwal_mata_kuliah` di prodi mahasiswa lewat `match_program` exact, sehingga baris tampil di section 03 Akademik dan disalin `sync_master_schedules()` ke seluruh mahasiswa prodi. Urutan sumber matkul: `subjects` (bila `subject_id` terisi, dalam scope request) → `subject_name_raw` dengan kode sintetis stabil `REQ-xxxxxxxx`. Kampus: `campus_id` request → kampus mata kuliah prodi → kampus aktif pertama. Bila prodi tidak ketemu, nama kosong, kampus tidak ada, atau waktu legacy tidak valid, promosi dilewati **tanpa menggagalkan approve**; respons approve membawa `published_to_master` (true/false, null di endpoint lain). Baris `schedules` mahasiswa memakai `master_schedule_id()` (UUIDv5) yang sama dengan sync agar tidak dobel; approve kedua untuk slot identik me-reuse baris itu. Tes: `tests/test_schedule_request_promote.py`.

**Penting untuk Android:** `schedules.student_id` = `students.id`, sedangkan `student_id` di response `/auth/login` = `users.id`. Android harus mengunci Room dengan `users.id` dari auth, bukan nilai response.

### Relasi wajib eager-load (HTTP 500)

`_to_response()` (`routers/schedule_request.py`) membaca `req.subject.name` / `req.lecturer.name`. `db.refresh()` hanya memuat ulang kolom skalar, jadi relasi harus disebut eksplisit: `db.refresh(req, _REQ_RELATIONS)` (`["subject", "lecturer"]`). Tanpa itu SQLAlchemy melakukan lazy-load di luar greenlet → `MissingGreenlet` → **500**. Bug ini hanya muncul bila `subject_id`/`lecturer_id` terisi; nilai null short-circuit dan menyembunyikannya. Query list/mine/approve/reject sudah memakai `selectinload`; `routers/schedule.py` memakai pola sama untuk `subject`/`lecturer`/`campus`.

## Pipeline ML dan skor

1. Fitur dari agregasi log harian; sampel lokasi dipakai sebagai agregasi, bukan pengukuran menit.
2. Split test 0.2, random_state 42, n_init 10. Dataset kecil punya cabang khusus; data kosong harus error jelas.
3. StandardScaler di-fit pada train; PCA + KMeans pada representasi itu. Simpan scaler/PCA/KMeans/metrik/konfigurasi/label bersama versi model.
4. Label dipetakan dari urutan rata-rata screen time; ID klaster mentah tidak bermakna risiko tetap.
5. `serving.py` harus memakai urutan fitur, transformasi, label, dan versi yang konsisten dengan trainer. Artefak pickle hanya dari sumber internal.
6. Hasil di `ClusterResult`; invalidasi cache Redis setelah training/pergantian model.
7. `_model_cache` di `serving.py` adalah LRU maks 2 entri; `train_pipeline` melepas DataFrame/array sementara + `gc.collect()` sebelum kembali.
8. **Tanggal efektif wajib identik di kedua jalur**: `coalesce(log_date, timestamp)`. Trainer memakainya di `build_feature_dataframe`; serving memakainya di `_aggregate_student_features_batch_sync` dan `score_all_students_for_date`. Memakai `date(log_date)` saja membuang baris `log_date` NULL (seed legacy, payload MQTT tanpa field) sehingga mahasiswa dinilai dengan vektor fitur berbeda dari yang dipakai melatih. Tes: `test_train_and_serve_aggregate_log_date_null_identically`.
9. `cluster_distribution` dihitung dari `all_labels` (seluruh dataset), bukan `train_labels` — harus menjumlah ke `dataset_size` agar konsisten dengan `clustering_results`.
10. `upsert_cluster_result_sync` cabang **update** harus menulis semua kolom hasil (`pca_x/y/z`, `cluster_name`, `distraction_score`, `features`). `pca_z` pernah hilang di cabang update sehingga koordinat 3D beku di nilai tulis pertama. Tes: `tests/test_ml_serving_upsert.py`.


### Gate sebelum model boleh `completed`

K-Means tidak punya label ground truth, jadi "akurasi" klasifikasi tidak ada. Yang dipakai adalah indeks internal pada **data uji (holdout)** — partisi yang tidak pernah dilihat `fit`:

- `ML_MIN_DATASET_SIZE` (default 30 baris student-date) — ditolak di `train_pipeline` sebelum fitting. Floor lama hanya `len(X) >= k_n` (3 baris), terlalu rendah untuk silhouette/inertia yang berarti.
- `ML_MIN_HOLDOUT_SILHOUETTE` (default 0.10, `None` = nonaktif) — `_check_holdout_gate()` menolak training bila silhouette holdout di bawah ambang. Gate **tidak** menggagalkan training saat metrik tak terdefinisi (holdout degenerate/terlalu kecil) — itu "tidak diketahui", bukan "buruk".
- Metrik disimpan di `model_versions`: `holdout_silhouette`, `holdout_davies_bouldin`, `holdout_calinski_harabasz`, `holdout_min_silhouette`. `silhouette_score` lama tetap = partisi **train**.
- `train_metrics` dan `holdout_metrics` juga dikembalikan `train_pipeline` untuk perbandingan (gap besar = indikasi overfit meski unsupervised).

Kegagalan gate adalah jalur **normal**, bukan error tak terduga. Karena `_run_training` menonaktifkan semua model di awal, kedua cabang gagal (HTTP `ValueError`/generik dan worker) memanggil fallback re-aktivasi model completed terakhir — tanpa itu `/api/reports/*` dan `/api/models/results/latest` 404 sampai training berikutnya berhasil. Tes: `tests/test_ml_pipeline.py`, `tests/test_ml_training_gate.py`.


Rumus skor di `ml/score.py`, terpisah dari assignment KMeans. Default: screen time cap 480 menit bobot 0.35; sosmed+game cap 240 bobot 0.25; unlock cap 10 bobot 0.20; fraksi penggunaan dalam kelas 0.10; penalti kecocokan jadwal <0.5 sebesar 0.10. Hasil dibatasi 1, dibulatkan 3 desimal. Jangan duplikasi rumus di UI.

Scheduler cek tiap 60 detik, slot `>=` untuk mengejar waktu terlewat, flag direset per tanggal UTC. Scoring dipanggil saat training baru diantrekan, bukan menunggu selesai. Flag cutoff/training hanya diset setelah enqueue berhasil.

Laporan `/api/reports`: `avg_distraction_score` dari AVG skor model aktif; tanpa skor → null bukan nol. `/api/reports/cluster-trends` memberi `cluster_counts` berlabel dinamis.

## AI dan push

`routers/ai.py` mengatur konteks, chat, daftar model, pool Groq. Key `GROQ_API_KEY_1..4` di server; default `openai/gpt-oss-120b`. Tanpa key → 503. Jangan minta key Groq dari browser.

FCM butuh service account server + token perangkat. Kehadiran SDK bukan bukti push siap produksi. Jangan catat token perangkat atau konteks mahasiswa sensitif.

## Menjalankan dan memeriksa

Dari `server/`, setelah virtualenv dan environment dev siap:

```bash
source venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
rq worker monitoring --url redis://localhost:6379/0
python -m app.schedules.scheduler
python -m app.mqtt_subscriber
```

Queue bernama `monitoring`. `USE_FAKEREDIS=true` menjalankan enqueue in-process; tidak membuktikan pub/sub lintas proses. Settings butuh `SECRET_KEY`, `MQTT_USERNAME`, `MQTT_PASSWORD`, URL DB async/sync. Jangan cetak nilainya.

Startup FastAPI menjalankan migrasi dan cleanup DB — pilih DB disposable sebelum smoke test. Mode lokal dikelola `../scripts/run-server.sh`; `server/run.sh` meneruskannya.

CI: `.github/workflows/ci.yml` (push/PR) — `ruff check app --select E9,F63,F7,F82`, `pytest -q`, Trivy fs (CRITICAL/HIGH, `ignore-unfixed`), SBOM CycloneDX via `anchore/sbom-action`. Python **3.14.10** di runner, sama dengan Dockerfile dan venv lokal. Style ruff tidak digate (masih ~500 temuan lama).

**Celah yang perlu disadari:** CI, Docker, dan venv dev harus tetap di versi Python yang sama. Sebelum disamakan, CI menguji 3.13 sedangkan dev berjalan di 3.14 — inkompatibilitas khusus 3.14 (SQLAlchemy 2.0.35) **lolos CI sepenuhnya** dan hanya muncul di mesin developer. Jika versi Python dinaikkan lagi, naikkan ketiganya bersamaan (Dockerfile `ARG`, `ci.yml`, dan venv lokal). **Kasus nyata berikutnya:** 3.14.4 dipakai serentak di ketiganya tetapi CI tetap gagal karena `ubuntu-latest` bermigrasi ke Ubuntu 26.04 dan GitHub tidak menyediakan build 3.14.4 untuk 26.04 — versi Python yang sama di tiga tempat belum cukup bila runner OS-nya bergeser. Naikkan patch (mis. 3.14.10) atau pin `runs-on: ubuntu-24.04`; jangan biarkan `ubuntu-latest` memilih OS secara diam-diam.

Alat lint/test dipisah ke `requirements-dev.txt` (`pytest==9.2.0`, `ruff==0.17.5`, plus `-r requirements.txt`), bukan di `requirements.txt` produksi. CI memasang `pip install -r requirements-dev.txt`; venv lokal yang hanya memasang `requirements.txt` tidak bisa menjalankan `pytest tests/`. Pin versinya disengaja: sebelumnya `pip install pytest ruff` tanpa versi membuat hasil CI tidak reproducible.

Pemeriksaan:
- `python -m scripts.backend_regression_check` — fixture SQLite in-memory, tanpa `.env`/lifespan. Mencakup password, otorisasi HTTP/WS, rollback/ACK batch, signature MQTT, retry scheduler. `MONITORUNG_CHECK_DATABASE_URL` hanya untuk DB disposable bernama `monitorung_regression`.
- `python -m scripts.scheduler_slot_check`, `python -m scripts.ml_pipeline_smoke_test`.
- `scripts/smoke_test.py` lama memakai kontrak login usang; jangan jalankan ke data aktif.

Untuk kontrak baru, tambah satu regression check terfokus pada input/otorisasi/dedup/waktu/agregasi yang berubah. Manual training: `POST /api/models/train`; history/config/results/predict/activate di `routers/models.py`. Manual training dan RQ worker punya jalur orkestrasi berbeda — baca keduanya bila mengubah persist status/model.

## Optimasi performa (terpasang)

- DB UpCloud remote (~1,5 ms/round-trip): jumlah query lebih menentukan daripada CPU. `GET /api/schedules` turun dari 96,4 ms/53 query ke 18,4 ms/12 query lewat UPSERT batch + tulis hanya saat berubah.
- `get_db()` → `configure_request_scope()` sudah mengautentikasi; payload dibagikan lewat `request.state.auth_payload` agar route tidak autentikasi dua kali. **Jangan memoize hasil `get_current_user()`**: regression test menonaktifkan akun pada objek `Request` yang sama dan harus tetap 401.
- Index di `_INDEX_DEFINITIONS` (`database.py`), dibuat idempoten saat startup: `location_logs.idempotency_key`, `location_logs(student_id, timestamp)`, `app_logs.idempotency_key`, `cluster_results(model_version_id, result_date)`.
- Opsi pool DB dipusatkan di `pool_kwargs(url, size)` (`database.py`) dan dipakai oleh `engine`, `get_sync_engine()`, `mqtt_subscriber.engine`, serta `mqtt_client_stub.make_sync_engine()`. SQLite (StaticPool) menolak `pool_size`/`max_overflow`, jadi helper mengembalikan `{}` untuk SQLite dan `pool_size`/`max_overflow`/`pool_recycle=1800`/`pool_pre_ping` untuk PostgreSQL. Jangan duplikasi dict pool di tempat lain.
- `dispose_engines()` dipanggil di shutdown lifespan supaya worker yang di-recycle `--limit-max-requests` tidak menahan koneksi PostgreSQL.
- Production menjalankan uvicorn 2 worker + `--limit-max-requests 1500 --timeout-graceful-shutdown 30`; jangan kembalikan gunicorn/UvicornWorker (race notify vs timeout membunuh worker sehat).

## Production

VPS `root@213.163.207.199`, `/opt/monitorung`. Compose:
`docker compose --env-file .env.production -f docker-compose.yml -f compose.vps.yml <cmd>`

Database **Managed PostgreSQL UpCloud 18.7** (`defaultdb`) via private network. `UPCLOUD_DATABASE_URL`/`UPCLOUD_DATABASE_SYNC_URL` di `.env.production`, `PGSSLMODE=verify-full` + `PGSSLROOTCERT=/app/upcloud-ca.pem`. URL tanpa parameter SSL agar asyncpg dan helper sync sama-sama jalan (keduanya membaca `PGSSLMODE`/`PGSSLROOTCERT` dari environment). CA UpCloud ada di `/opt/monitorung/upcloud-ca.pem` di VPS dan di-bind-mount read-only ke api/worker/scheduler/subscriber lewat `compose.vps.yml`. Sertifikat server memuat SAN `monitorterop-pphsdagzsaph.db.upclouddatabases.com`, jadi hostname check lolos. Container postgres lokal berhenti, profile `local-db-rollback`; jangan rollback ke data lokal tanpa sinkronisasi balik.

Deploy: build → `docker save | gzip | ssh | docker load` → `up -d --no-deps --wait api worker scheduler subscriber web`. **Tidak ada backup tersisa** (dihapus 2026-11-17); rollback hanya lewat rebuild.

Status 2026-11-17 (deploy optimalisasi RAM): `monitorung-api:latest` `sha256:9ffed70fc405…`. Rollback tag di VPS: `monitorung-api:rollback` `sha256:2f22741ace64…` (+ `docker-compose.yml.pre-opt`, `.env.production.pre-opt` di `/opt/monitorung`). Tujuh layanan healthy; `UVICORN_WORKERS` VPS diturunkan 4 → 2 (env menimpa default compose). Batas RAM aktif: api 1536m, worker 1024m, web 768m, scheduler 320m, subscriber 384m. api pasca-deploy ~357 MiB / 26 PIDs. Data akademik production dikosongkan agar dibangun ulang manual; users, students, campuses, app_logs, location_logs, cluster_results, model_versions dipertahankan.

## Batas memory

Disusun dari pembacaan kode dan konfigurasi, bukan pengujian runtime. `README.md`, `DEPLOYMENT.md`, dan `CLAUDE.md` memuat detail historis dan sebagian usang. Perbarui bagian ini setiap kontrak, role, versi, agregasi, atau alur berubah.
