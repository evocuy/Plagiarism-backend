# Plagiarism Checker - Backend Service

Backend REST API untuk pemeriksaan kemiripan dokumen berbasis FastAPI, PostgreSQL (SQLAlchemy), PyMuPDF, Sastrawi, dan scikit-learn.

Panduan deployment dan batas resource: [deployment/HARDENING.md](deployment/HARDENING.md).

Login mahasiswa SADS dengan pembatasan mata kuliah aktif Seminar/Skripsi/Tugas Akhir:
[deployment/SADS_LOGIN_PILOT.md](deployment/SADS_LOGIN_PILOT.md).

Impersonasi mahasiswa oleh admin melalui NIM, termasuk kontrak frontend:
[deployment/IMPERSONATION.md](deployment/IMPERSONATION.md).

---

## 📁 Struktur Direktori Backend

```text
backend/
├── app/
│   ├── database/
│   │   └── session.py            # Koneksi database SQLAlchemy engine & session
│   ├── models/
│   │   └── schemas.py            # Model tabel database (User, Mahasiswa, Dosen, Document, PlagiarismReport, dll.)
│   ├── routes/
│   │   ├── auth.py               # Endpoint autentikasi, JWT cookie/header, CRUD pengguna, & seeding user
│   │   ├── documents.py          # Endpoint upload file, daftar dokumen, & download/view dokumen
│   │   └── plagiarism.py         # Endpoint analisis plagiarisme, approval dosen, dan riwayat cek
│   ├── services/
│   │   ├── pdf_service.py        # Ekstraksi teks & pemrosesan file PDF
│   │   └── preprocessing_service.py # Normalisasi teks, tokenisasi, n-gram, & komparasi kemiripan
│   └── main.py                   # Entry point aplikasi FastAPI, inisialisasi CORS, auto-create tables
├── uploads/                      # Direktori penyimpanan file upload (PDF/dokumen)
├── .env.example                  # Template variabel environment
├── .env                          # Konfigurasi environment lokal (jangan di-commit)
├── Dockerfile                    # Container definition untuk backend
├── requirements.txt              # Daftar dependensi Python
└── README.md                     # Panduan setup & dokumentasi backend
```

---

## ⚙️ Persyaratan Sistem

- **Python**: Versi 3.12 atau lebih baru (sesuai image deployment)
- **PostgreSQL**: Versi 13+ (bisa dijalankan via Docker)
- **Virtual Environment** (`.venv`)

---

## 🚀 Panduan Setup & Menjalankan Backend

### 1. Masuk ke Direktori Backend
Buka terminal dan navigasikan ke folder backend:
```bash
cd backend
```

### 2. Buat & Aktifkan Virtual Environment
- **Windows (PowerShell)**:
  ```powershell
  python -m venv .venv
  .\.venv\Scripts\Activate.ps1
  ```
- **Linux / macOS**:
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  ```

### 3. Install Dependensi
```bash
pip install -r requirements.txt
```

### 4. Konfigurasi Environment (`.env`)
Salin file `.env.example` menjadi `.env`:
```bash
cp .env.example .env
```
Sesuaikan konfigurasi koneksi database di `.env`:
```env
APP_ENV=development
DATABASE_URL=postgresql://<db_user>:<db_password>@localhost:5432/<db_name>
UPLOAD_DIR=uploads
CORS_ORIGINS=http://localhost:3000,http://localhost:3001,http://127.0.0.1:3000,http://127.0.0.1:3001
JWT_SECRET_KEY=ganti_dengan_random_secret_key_anda
JWT_EXPIRE_MINUTES=60
```

> 💡 **Info Database & Migrasi:**
> Backend membuat tabel dan memperbarui kolom runtime saat startup. Akun admin
> `cihuy` hanya dibuat jika belum ada dan `BOOTSTRAP_ADMIN_PASSWORD` diisi dengan
> password unik minimal 12 karakter. Akun yang sudah ada tidak ditimpa.

### 5. Jalankan Server API
Jalankan backend menggunakan `uvicorn`:
```bash
uvicorn app.main:app --reload --port 8000
```
Server akan aktif di:
- **Base URL**: `http://localhost:8000`
- **Interactive Swagger Docs**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`

---

## 👥 Akun Bawaan (Default Seed Accounts)

Tidak ada password bawaan dalam kode. Untuk database baru, isi
`BOOTSTRAP_ADMIN_PASSWORD` di `.env` saat bootstrap admin `cihuy`, lalu kosongkan
setelah akun berhasil dibuat. Akun mahasiswa dan dosen dibuat oleh admin.
---

## 📌 Ringkasan Endpoint Utama

- **Health Check**: `GET /api/health`
- **Auth**:
  - `POST /api/auth/login` (Mendukung JWT via response body & session cookie)
  - `GET /api/auth/me` (Cek info profil user yang sedang login)
  - `POST /api/auth/logout`
  - `GET /api/auth/users` (Admin only)
  - `POST /api/auth/users` (Admin only)
- **Dokumen**:
  - `POST /api/documents/upload` (Upload file PDF)
  - `GET /api/documents` (Daftar dokumen)
  - `GET /api/documents/{id}/file` (Download/preview file)
- **Plagiarism**:
  - `POST /api/plagiarism/check` (Jalankan pemindaian plagiarisme dokumen)
  - `GET /api/plagiarism/reports/{document_id}` (Detail hasil analisis per dokumen)
  - `GET /api/plagiarism/check/{id}/status` (Status pemrosesan)

## Repositori alumni dan penghapusan alur pembimbing

Role aktif adalah `admin` dan `mahasiswa` (`super_admin` lama tetap diperlakukan
sebagai admin). Tidak ada lagi penugasan pembimbing atau approval dokumen.
`POST /api/auth/users` hanya menerima role mahasiswa/admin; endpoint
`GET /api/auth/users/dosen` dan `PATCH /api/plagiarism/check/{id}/approval` dihapus.
Field pembimbing, approval, catatan reviewer, serta counter review dashboard
dihapus dari kontrak API. Frontend harus diperbarui bersama versi backend ini.

Admin mengimpor dokumen alumni melalui `POST /api/documents/upload`. Admin
bertanggung jawab memastikan asal dokumen dari alumni yang sudah lulus;
aplikasi belum terhubung ke layanan verifikasi kelulusan kampus. Sumber harus
memiliki `source_origin=alumni`, tanpa pemilik mahasiswa, berstatus published,
dan belum dihapus. Dokumen mahasiswa selalu `source_origin=student` dan draft,
termasuk dokumen lama yang pernah disetujui. Pemeriksaan tetap membandingkan
proposal dengan proposal dan skripsi dengan skripsi.

Jika sumber sejenis belum tersedia, proses selesai dengan
`comparison_status=no_comparison_sources`, skor/teks persentase pada API bernilai
null, dan pesan bahwa kemiripan belum dapat ditentukan. `available` berarti
pembanding tersedia. Frontend menampilkan kondisi ini secara eksplisit.

Saat startup, migrasi PostgreSQL menyimpan metadata pembimbing/review lama ke
`legacy_workflow_archive`, menonaktifkan akun dosen serta mencabut sesinya,
kemudian menghapus tabel/kolom workflow lama. Dokumen, hasil pemeriksaan, dan
skor lama dipertahankan. Impor lama tanpa pemilik diklasifikasikan alumni satu
kali; daftar impor tersebut tetap perlu ditinjau admin sesuai asal dokumennya.
Migrasi dapat diulang dan tidak menerbitkan kembali dokumen mahasiswa.
Perubahan ini dikembangkan lokal; VM belum diperbarui.

## Penyaringan judul dan posisi highlight

Pemeriksaan baru mengabaikan judul struktur yang dikenali, seperti `BAB I`,
`PENDAHULUAN`, `Latar Belakang`, dan subbab bernomor pendek. Penyaringan berlaku
pada baris judul tersendiri; isi paragraf Bab I dan latar belakang tetap dihitung.
Deteksi memakai pola judul umum, sehingga format judul khusus perlu dievaluasi
dengan contoh PDF kampus. TF-IDF dan cosine similarity tetap menjadi algoritma
skor dokumen. Skor tersebut menunjukkan kemiripan, bukan putusan plagiarisme.

Highlight memakai posisi frasa pada teks halaman yang telah disaring dan
koordinat kata PDF. Frasa sama di lokasi lain tidak otomatis ikut ditandai.
Batas minimum kecocokan tetap 3 token hasil preprocessing dan kini berlaku juga
pada jalur kalimat identik. Kalimat utuh memerlukan skor minimal 0.70 serta
teks identik setelah normalisasi ringan; segmen frasa identik dapat ditandai
di bawah skor kalimat itu. Angka tersebut adalah aturan teknis saat ini,
bukan ambang resmi kampus. Menambah kata minimum perlu pengujian akurasi.

Kolom posisi halaman ditambahkan oleh migrasi runtime saat startup API.
Cache teks/kalimat lama diperbarui otomatis ketika dipakai untuk pemeriksaan
baru; embedding lama tidak dipakai untuk memilih kandidat sampai cache korpus
sesuai versi ekstraksi. Pemeriksaan pertama pada repositori lama dapat lebih
lama karena pembaruan cache. Hasil dan PDF pemeriksaan terdahulu tetap menjadi
riwayat; jalankan pemeriksaan baru untuk mendapatkan hasil dengan aturan ini.

Frontend tetap dapat memakai endpoint hasil dan PDF yang sama. Respons detail
kecocokan menambahkan `page_start_position` dan `page_end_position`, yaitu offset
karakter pada teks halaman yang sudah disaring. Field `start_position` dan
`end_position` tetap relatif terhadap kalimat asal.

## Respons dan status indexing embedding

Layanan embedding harus mengembalikan `index` integer unik untuk setiap input
batch. Backend memasangkan vektor berdasarkan indeks tersebut meskipun urutan
respons berubah. Respons dengan indeks hilang/duplikat, jumlah vektor tidak
sesuai, vektor kosong/tidak valid, atau dimensi batch tidak konsisten ditolak.

Status dan ringkasan dari `ensure_document_embeddings` dihitung dari seluruh
chunk setelah proses indexing. `completed` hanya diberikan jika tidak ada
chunk yang masih pending atau gagal. Kegagalan lama tetap dihitung saat batch
lain dilanjutkan; `refresh_embeddings=true` pada endpoint indexing dapat dipakai
untuk mencoba ulang chunk gagal. Counter di `index_result` tetap menunjukkan
pekerjaan pada percobaan tersebut. Endpoint dan format request tidak berubah.

## PDF top tiga pembanding dengan watermark

`GET /api/plagiarism/check/{check_id}/top-matches` menambahkan `pdf_available`,
`pdf_watermarked`, dan `pdf_url` pada setiap hasil. Jika tersedia, `pdf_url`
menunjuk ke `GET /api/plagiarism/check/{check_id}/sources/{source_document_id}/pdf`.
Endpoint mengembalikan PDF inline dengan logo kampus transparan di setiap
halaman. Hanya pemilik hasil atau admin yang dapat mengakses tiga sumber dengan
skor tertinggi setelah proses selesai; sumber harus masih menjadi dokumen
alumni yang aktif. Dokumen asli tidak diubah. Salinan watermark disimpan di
`uploads/watermarked/` dan dipakai ulang; file lama di direktori tersebut dapat
dihapus saat pemeliharaan karena akan dibuat kembali bila diperlukan.

Frontend mengambil `pdf_url` dengan header `Authorization: Bearer <token>`,
memeriksa status HTTP, lalu membuka atau menampilkan hasil `Blob` melalui object
URL. Membuka URL langsung di tab baru tidak otomatis menyertakan token Bearer
yang tersimpan di localStorage. Tangani `403`, `404`, `409`, `503`, dan `504`,
serta revoke object URL ketika tidak diperlukan. Opsi backend
`WATERMARK_MAX_CONCURRENT` (default 2) dan `WATERMARK_TIMEOUT_SECONDS`
(default 120) membatasi pembuatan PDF pada proses terpisah.
