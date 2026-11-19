# Impor massal PDF alumni dan embedding

Importer ini adalah command backend untuk pengelola server. Tidak ada endpoint
atau halaman frontend baru. Pengembangan/tes dilakukan lokal; pemasangan di VM
menunggu pengguna push dan meminta deployment.

## Input dan hasil

Siapkan folder khusus berisi **dokumen alumni**:

```text
repository-inbox/
├── proposal/
│   ├── dokumen-a.pdf
│   └── prodi/angkatan/dokumen-b.pdf
└── skripsi/
    └── dokumen-c.pdf
```

Nama folder jenis tidak peka huruf besar/kecil. PDF di luar kedua jenis tersebut
ditandai gagal, kecuali `--document-type` diberikan untuk seluruh folder.
Folder yang namanya sendiri proposal/skripsi juga dapat menjadi `--root`.
PDF diproses dalam urutan nama; file non-PDF dan symlink dilewati. Folder input
harus terpisah dari uploads. Sumber disalin, tidak dipindah atau dihapus.

Validasi ukuran, PDF berbasis teks, batas halaman/teks/chunks, dan struktur BAB
mengikuti backend sekarang: proposal BAB I–III; skripsi BAB I–V. Tidak ada OCR
atau pengecualian batas validasi untuk impor. Filename hanya menjadi judul;
path penyimpanan memakai checksum sehingga karakter filename tidak menjadi kode/path.

Setiap dokumen baru dibuat ownerless (`user_id=null`), `source_origin=alumni`,
awalnya draft. Setelah teks dan chunks berhasil dibuat, dokumen diterbitkan
sebagai pembanding. Kegagalan embedding tidak menghapus PDF/teks/chunks;
TF-IDF tetap bisa memakai dokumen alumni yang sudah siap. Hasil import dan
status embedding dilaporkan terpisah agar kegagalan indexing tidak dianggap sukses.
Dokumen mahasiswa tidak dipakai sebagai target dedup/publikasi. Proposal tetap
dibandingkan dengan proposal, skripsi dengan skripsi.

## Menjalankan di lokal

Dari folder backend, dengan `.venv` aktif dan konfigurasi privat backend tersedia:

```powershell
python -m app.workers.import_repository --root "C:\data\repository-inbox" --dry-run --limit 10
```

Preview memvalidasi PDF dan mendeteksi byte identik di antara input, tanpa
menghubungkan database, menulis salinan/report, atau memanggil embedding API.
Preview **belum memeriksa duplikat terhadap database**. `--with-embeddings` pada
preview tetap tidak mengirim teks ke provider. Seluruh hasil tampil di terminal.

Jika sudah sesuai, impor 10 dokumen pertama sekaligus indexing:

```powershell
python -m app.workers.import_repository --root "C:\data\repository-inbox" --limit 10 --with-embeddings
```

Hilangkan `--limit` untuk seluruh input. Untuk folder satu jenis tanpa struktur
proposal/skripsi, tambahkan `--document-type proposal` atau skripsi.

Contoh validasi semua 100 PDF (50 proposal dan 50 skripsi), tanpa batas 10 file:

```powershell
python -m app.workers.import_repository --root "C:\data\repository-inbox" --dry-run
```

Impor tanpa embedding cukup tanpa flag `--with-embeddings`. Setelah itu command
yang sama dengan flag tersebut akan mengindeks dokumen yang sudah ada.
Impor sungguhan memerlukan PostgreSQL, schema backend terbaru, dan akun admin
aktif `cihuy` (atau `--admin IDENTIFIER`). Identitas admin dicatat di checkpoint;
ini bukan login publik atau bypass autentikasi, melainkan alat operator server
yang sudah memiliki akses privat ke database/storage.

`--with-embeddings` juga memerlukan EMBED_URL/EMBED_API_KEY/EMBED_MODEL yang sudah
dipakai backend serta kolom pgvector. Konfigurasi/storage yang belum siap ditolak
sebelum pemrosesan dan sebelum request provider. Tidak ada model Torch baru
atau layanan embedding yang dijalankan lokal. Ukuran batch default 32 chunks,
dapat diatur `--embedding-batch-size 1` sampai 128.

## Menjalankan di VM setelah deployment diminta

Contoh di bawah memakai service Compose/volume yang sudah ada. API dan worker
tetap memakai project dan database yang sama; jangan membuat database/volume baru.
Jalankan backend versi ini sekali agar migrasi kolom checksum sudah diterapkan.

Buat staging folder yang bisa diisi lewat SFTP dan dibaca UID/GID 10001 container:

```bash
sudo install -d -o evopuki -g 10001 -m 2750 /home/evopuki/repository-inbox
sudo install -d -o evopuki -g 10001 -m 2750 /home/evopuki/repository-inbox/proposal
sudo install -d -o evopuki -g 10001 -m 2750 /home/evopuki/repository-inbox/skripsi
```

Salin PDF ke folder sesuai jenisnya. Setelah transfer, pastikan seluruh folder
input dapat ditelusuri dan PDF dapat dibaca group 10001. Transfer SFTP/SCP bisa
membawa izin folder yang berbeda. Jalankan di terminal VM:

```bash
sudo find -P /home/evopuki/repository-inbox -type d \
  -exec chgrp 10001 {} + -exec chmod 2750 {} +
sudo find -P /home/evopuki/repository-inbox -type f -iname '*.pdf' \
  -exec chgrp 10001 {} + -exec chmod 640 {} +
```

Perintah tersebut hanya mengatur group/izin pada staging, tanpa membaca naskah
atau menghapus PDF. Symlink tidak diikuti. Folder memerlukan izin baca dan
telusur (`r-x`), bukan hanya izin baca file. `sudo docker compose` tetap memakai
UID/GID 10001 di dalam container. `input_unreadable` berarti traversal folder
gagal sebelum validasi PDF; membetulkan izin file saja belum mencukupi.
Tidak perlu mengubah pemilik kode, `.env`, atau data PostgreSQL.

Preview semua PDF memakai container terpisah tanpa membuka port API tambahan
(tambahkan `--limit 10` hanya bila ingin membatasi uji awal):

```bash
cd /home/evopuki/backend
sudo docker compose run --rm --no-deps -T \
  --volume /home/evopuki/repository-inbox:/import:ro \
  backend python -m app.workers.import_repository --root /import --dry-run
```

Kemudian impor sekaligus embedding:

```bash
sudo docker compose run --rm --no-deps -T \
  --volume /home/evopuki/repository-inbox:/import:ro \
  backend python -m app.workers.import_repository --root /import --with-embeddings
```

Container menjalankan UID/GID yang sama dengan API/worker agar file yang dibuat
dapat dibaca aplikasi. Input di-mount read-only, uploads tetap memakai bind mount
existing. Tidak perlu menyalin seluruh staging folder ke dalam image atau uploads.
Biarkan terminal tetap tersambung selama proses berjalan.

## Resume, checksum, dan data lama

Jalankan ulang command yang sama setelah terputus/gagal. Tidak perlu memberikan
report lama: checkpoint tersimpan di metadata dokumen dan pencocokan memakai
`content_sha256` + jenis dokumen. Satu proses impor memakai advisory lock PostgreSQL
yang tetap bertahan selama commit tiap dokumen; proses impor kedua ditolak.

- PDF identik dengan nama berbeda dalam input dilewati.
- Dokumen alumni yang sudah ada dipakai kembali; ID dan hasil cek sebelumnya tetap.
- Dokumen yang sudah soft delete dilewati, tidak diaktifkan kembali.
- Tahap teks/chunks yang sudah tersimpan memakai cache versi saat ini.
- Embedding yang sudah berhasil tidak dihitung ulang. Chunks gagal/pending atau
  yang memakai model berbeda dicoba saat menjalankan kembali --with-embeddings.
- File yang berubah selama penyalinan ditolak. Salinan memakai file sementara
  dan atomic replace; salinan selesai yang tertinggal sebelum commit bisa dipakai
  kembali. Ctrl+C membersihkan file sementara milik operasi yang sedang berjalan.

Migrasi hanya menambah nullable `documents.content_sha256` beserta index biasa.
Upload web baru ikut menyimpan checksum. Checksum dokumen alumni lama dihitung
sekali dari file yang masih tersedia saat impor. Tidak ada constraint unik yang
menghapus/menggabungkan duplikat historis. File lama yang hilang/di luar managed
storage dilaporkan melalui historical_files_unavailable, tanpa menghapus metadata.
Dedup merupakan byte identik, bukan pencarian kemiripan isi atau parafrase.

Simpan staging sampai seluruh import dan embedding yang diinginkan selesai.
Jika input dipindah/hilang, command folder tidak dapat mencoba file tersebut lagi.
Hindari upload/reindex manual bersamaan agar sumber dan indexing stabil selama
impor besar. Jalankan benchmark setelah seluruh korpus selesai dimasukkan.

## Indeks HNSW untuk pemilihan kandidat

Setelah embedding repositori siap dan backend versi ini sudah berjalan, bangun
indeks di PostgreSQL dengan pgvector 0.8 atau lebih baru:

```bash
sudo docker compose exec -T backend python -m app.workers.build_candidate_index
```

Perintah memakai `EMBED_MODEL` dari konfigurasi yang sudah ada dan mendeteksi
dimensi dari embedding alumni aktif. Jika dimensi perlu dipilih secara eksplisit,
tambahkan `--dimensions 384` untuk model yang benar-benar menghasilkan 384 dimensi.
Status `created` atau `already_exists` berarti indeks siap. Jalankan perintah
lagi setelah berpindah model atau dimensi. Pembuatan indeks berjalan secara
concurrent di luar startup dan tidak memanggil layanan embedding.

Backend memakai `CANDIDATE_HNSW_EF_SEARCH=160` secara default. PostgreSQL bisa
memilih pencarian eksak untuk cakupan yang kecil. Bila hasil aproksimasi kurang
dari limit, backend memeriksa cakupan yang sama secara eksak. Tanpa indeks yang
cocok atau saat pengaturan bernilai `0`, pencarian pgvector berjalan eksak.
Ketersediaan embedding tetap menentukan kapan alur beralih ke TF-IDF.

Ukur durasi pencarian dan kemunculan sumber yang diketahui di top 20 setelah
migrasi korpus kampus. Ukur juga ruang indeks dan waktu pembangunan karena
jumlah embedding potongan teks menentukan biaya bersama spesifikasi VM.

## Progres, report, dan ruang disk

Terminal mengeluarkan JSON per tahap/file dan summary. Report live disimpan di
`uploads/import-reports/import-TIMESTAMP-ID.jsonl`. Lokasi baru dapat dipilih lewat
`--report PATH.jsonl`; file existing tidak ditimpa. Preview tidak membuat report.
Report mencakup nama relatif input, ID, checksum, action, status, status embedding,
jumlah chunks, dan error aman/ID referensi. Tidak berisi teks PDF, token, key, atau URL
provider. Perlakukan report sebagai data administratif privat.

Action umum: imported, resumed, existing, duplicate_in_input, soft_deleted.
Summary membedakan imported, completed/skipped/failed, embedding_completed,
embedding_failed, processed, dan not_attempted. `imported=10, failed=2` bisa berarti
10 PDF tersimpan, tetapi dua di antaranya belum selesai embedding; periksa row file.

Satu file diproses tiap waktu. Checkpoint/commit per tahap mempertahankan hasil
yang sudah berhasil saat proses berhenti. Default menyisakan setidaknya 512 MiB
sebelum copy/indexing; dapat diatur `--min-free-mb`. Ruang rendah menghentikan
sisa antrean, bukan menghapus dokumen lama. Estimasi ini hanya cadangan ruang,
bukan jaminan ukuran akhir database embedding. Staging, salinan PDF, chunks/vector,
highlight, dan backup tetap perlu kapasitas disk yang cukup.

Exit code: 0 selesai/preview valid; 1 ada file atau embedding gagal/proses berhenti
karena disk; 2 input/config/database belum siap; 130 dihentikan Ctrl+C.
Kegagalan satu PDF biasanya tidak menghalangi file lain.

## Jika validasi gagal

PDF yang dapat ditampilkan viewer belum tentu memiliki teks atau struktur BAB
yang dapat dibaca parser. Namun pesan native MuPDF tentang gambar/font yang
masih dapat dipulihkan tidak boleh merusak respons JSON validator. Worker
dan importer menyembunyikan diagnostik native tersebut dan tetap menilai hasil
ekstraksi; output terminal tetap berupa JSON, termasuk saat impor sungguhan.

- `PDF_UNREADABLE`: parser tidak berhasil membaca PDF.
- `PDF_TEXT_NOT_EXTRACTABLE`: PDF tidak memiliki teks yang dapat diekstrak; OCR belum tersedia.
- `PDF_PROCESSING_LIMIT` / `PDF_VALIDATION_TIMEOUT`: batas pemrosesan dokumen terlampaui.
- `PDF_VALIDATION_FAILED`: proses validator terhenti; kode keluar ditampilkan,
  tanpa isi PDF atau output native. Kode keluar saja belum memastikan penyebabnya.
- `PDF_VALIDATION_UNAVAILABLE` / `PDF_VALIDATION_INVALID_RESPONSE`: proses validator
  tidak dapat dijalankan atau memberikan respons yang tidak sesuai.
- `INVALID_CHAPTER_STRUCTURE`: periksa BAB hilang/tambahan dan urutan yang
  dilaporkan. Detektor membedakan deklarasi judul BAB dari kalimat seperti
  "BAB III menjelaskan metode penelitian". Daftar isi tidak menggantikan BAB isi.

Pesan urutan menampilkan urutan yang benar-benar terbaca, karena daftar BAB
terdeteksi yang diurutkan berdasarkan nomor saja tidak menjelaskan penolakan.
Jangan mematikan validasi atau menganggap PDF rusak hanya dari masalah proses
validator. Untuk diagnosis, cukup bagikan error code dan pesan aman tanpa nama
file, naskah, NIM, `.env`, atau log parser mentah.

Setelah perbaikan dipasang, jalankan ulang `--dry-run` pada folder yang sama.
Preview tidak mengimpor dokumen dan tidak memanggil embedding API. Versi cache
ekstraksi dinaikkan ketika deteksi BAB berubah, sehingga ekstraksi/chunks lama
dibangun ulang saat diperlukan. Riwayat dan skor pengecekan lama tidak diubah.
