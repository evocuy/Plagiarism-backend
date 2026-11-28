# Penyimpanan PDF di MinIO

FastAPI memvalidasi upload sebelum menyimpan PDF ke bucket privat. API yang
sudah ada melayani PDF highlight dan pembanding ber-watermark setelah mengecek
izin akun. PostgreSQL menyimpan referensi file, metadata, dan hasil pengecekan.

Gunakan bucket yang sudah dibuat dan kredensial layanan untuk prefix
`plagiarism/`. Pastikan API dan worker dapat mencapai alamat internal MinIO.
Layanan perlu membaca, menulis, dan memeriksa objek di prefix tersebut, serta
menghapus objek jika transaksi upload gagal. Client SDK juga mencari lokasi
bucket. Cek aturan expiry bucket jika penyimpanannya dipakai bersama.

Enam variabel di bawah ini dibaca oleh API dan worker. Isikan nilai asli di
`.env` yang privat. Untuk endpoint HTTPS, gunakan `MINIO_USE_SSL=true`.

```dotenv
MINIO_ENABLED=true
MINIO_ENDPOINT=minio.internal:9000
MINIO_USE_SSL=false
MINIO_ACCESS_KEY=
MINIO_SECRET_KEY=
MINIO_BUCKET=
```

`MINIO_ENABLED` memilih tujuan file baru. Referensi file lama dan MinIO dapat
dibaca selama transisi. Frontend mengakses PDF melalui FastAPI.

Jika PUT atau verifikasi PDF highlight gagal, worker menyimpan hasil render
di `uploads/highlighted/`. API dan worker memakai volume `uploads` yang sama
agar PDF tetap tersedia dari Riwayat. Salinan tersebut dapat dimigrasikan
dengan command migrasi yang sama setelah koneksi MinIO pulih.

## Aktifkan versi backend yang mendukung MinIO

Setelah commit tersedia di GitHub, jalankan dari folder backend di VM. Tunggu
pengecekan yang sedang berjalan selesai sebelum hentikan worker.

```bash
git pull --ff-only origin main &&
sudo docker compose -p backend build backend check-worker &&
sudo docker compose -p backend stop check-worker &&
sudo docker compose -p backend up -d --no-deps --no-build --wait --wait-timeout 120 backend &&
sudo docker compose -p backend up -d --no-deps --no-build check-worker
```

## Pulihkan PDF highlight yang hilang

Gunakan ID pengecekan yang sudah selesai, misalnya `123` dan `124`. Command ini
membuat ulang PDF dari kecocokan yang tersimpan agar skor dan hasil cek tetap
dipertahankan. Status `already_available` berarti PDF tersebut sudah tersedia.

```bash
sudo docker compose -p backend run --rm --no-deps -T backend \
  python -m app.workers.repair_highlighted_pdfs 123 124
```

## Salin dan alihkan PDF lama

Inventaris membaca referensi PostgreSQL dan memeriksa PDF di disk, termasuk
file mahasiswa, repositori, highlight, dan cache watermark yang masih ada.

```bash
sudo docker compose -p backend run --rm --no-deps -T backend \
  python -m app.workers.migrate_pdf_storage --dry-run
```

Periksa ringkasan. `local_missing` berarti file perlu diperiksa berdasarkan ID
yang dicetak. `checksum_mismatch` berarti isi file berbeda dari checksum yang
sudah tersimpan. Untuk file yang siap disalin, jalankan migrasi.

```bash
sudo docker compose -p backend run --rm --no-deps -T backend \
  python -m app.workers.migrate_pdf_storage --apply \
  --report /app/uploads/minio-migration/migrasi-01.jsonl
```

Migrasi mencocokkan ukuran dan SHA-256 objek sebelum mengganti referensi
database. ID, skor, dan embedding dipertahankan. Simpan report privat untuk
langkah pembersihan. Jika terputus, jalankan ulang dengan nama report baru.
Migrasi melanjutkan dari referensi lokal yang tersisa dan memakai ulang
salinan objek yang cocok. Simpan report dari setiap percobaan.

## Verifikasi dan ambil kembali ruang disk

Uji satu upload baru dan satu pengecekan, lalu buka PDF highlight lama dan
PDF Top 3 dari Riwayat. Setelah lolos, bersihkan salinan lokal saat layanan
sedang dalam jadwal pemeliharaan. Tunggu pengecekan aktif selesai terlebih dulu.

```bash
sudo docker compose -p backend stop check-worker backend &&
sudo docker compose -p backend run --rm --no-deps -T backend \
  python -m app.workers.migrate_pdf_storage \
  --cleanup-local /app/uploads/minio-migration/migrasi-01.jsonl
```

Pembersihan memeriksa referensi aktif serta mencocokkan isi PDF lokal dengan
objek. Jalankan untuk tiap report migrasi. Mulai lagi layanan setelah
pembersihan, termasuk bila ada file yang perlu diperiksa.

```bash
sudo docker compose -p backend up -d --no-deps --no-build --wait --wait-timeout 120 backend &&
sudo docker compose -p backend up -d --no-deps --no-build check-worker
```

Retensi upload mahasiswa bisa ditambahkan kemudian menggunakan prefix unggahan
dan tanggal upload yang tersimpan di PostgreSQL.
