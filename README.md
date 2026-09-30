# Plagiarism Checker - Backend Service

Backend REST API untuk sistem deteksi plagiarisme dokumen berbasis FastAPI, PostgreSQL (SQLAlchemy), dan PyMuPDF/NLTK.

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

- **Python**: Versi 3.10 atau lebih baru
- **PostgreSQL**: Versi 13+ (bisa dijalankan via Docker)
- **Virtual Environment** (`venv`)

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
  python -m venv venv
  .\venv\Scripts\Activate.ps1
  ```
- **Linux / macOS**:
  ```bash
  python3 -m venv venv
  source venv/bin/activate
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
JWT_EXPIRE_MINUTES=1440
```

> 💡 **Info Database & Migrasi:**
> Backend **tidak memerlukan Alembic manual**. Tabel database dan akun default akan dibuat otomatis saat aplikasi pertama kali dijalankan melalui perintah `Base.metadata.create_all(bind=engine)` di `app/main.py`.

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

1
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
  - `POST /api/plagiarism/approve/{document_id}` (Approval dosen)
