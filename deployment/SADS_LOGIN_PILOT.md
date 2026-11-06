# Uji lokal login SADS

Integrasi ini menguji login mahasiswa menggunakan endpoint SADS yang sudah
dimiliki kampus. Endpoint dan kredensial integrasi cukup diisi sendiri di
`backend/.env`; jangan dibagikan melalui chat atau dimasukkan ke Git. VM baru
diperbarui setelah pengguna push dan secara eksplisit meminta deployment.

API mata kuliah aktif belum tersedia. Akses mahasiswa untuk pilot memakai
daftar NIM yang disetujui manual dan waktu kedaluwarsa. Ini **bukan verifikasi
KRS**. Daftar kosong, tenggat kosong, atau tenggat lewat selalu menolak akses
mahasiswa. Daftar dan tenggat diperiksa setiap request berautentikasi, termasuk
pada token yang sudah diterbitkan. Role dosen dan review lokal tidak ditambah.

## Konfigurasi

Isi variabel berikut di `.env` lokal:

```dotenv
STUDENT_AUTH_PROVIDER=sads
SADS_LOGIN_URL=
SADS_LOGIN_TIMEOUT_SECONDS=10
SADS_LOGIN_HEADERS_JSON={}
SADS_TEST_ALLOWED_USERNAMES=
SADS_TEST_ACCESS_EXPIRES_AT=
```

- `SADS_LOGIN_URL`: URL HTTPS **lengkap** endpoint login resmi, diisi sendiri.
  Adapter saat ini mengirim `POST` dengan JSON `username` dan `password`.
  Sesuaikan adapter terlebih dahulu bila kontrak Insomnia ternyata berbeda.
- `SADS_LOGIN_HEADERS_JSON`: header autentikasi tambahan jika diwajibkan SADS.
  Contoh bentuk tanpa secret: `{"X-API-Key":"isi-sendiri-di-env"}`. Header ini
  hanya dikirim backend. TLS tetap diverifikasi dan redirect ditolak.
- `SADS_TEST_ALLOWED_USERNAMES`: NIM/username yang boleh menguji, dipisahkan
  koma, tanpa wildcard. Username SADS telah dikonfirmasi merupakan NIM.
- `SADS_TEST_ACCESS_EXPIRES_AT`: tenggat uji yang dipilih admin dalam ISO 8601
  dengan zona waktu, misalnya tanggal yang diinginkan diikuti
  `T23:59:59+08:00`. Nilai kosong menolak mahasiswa.

Restart backend lokal setelah mengubah `.env`. Untuk Uvicorn lokal melalui
HTTP, pastikan `SESSION_COOKIE_SECURE=false` dan `SESSION_COOKIE_SAMESITE=lax`;
gunakan Secure=true pada HTTPS. Mode awal `local` mempertahankan login lokal
yang sudah ada. Setelah memilih `sads`, mahasiswa wajib login ulang melalui
SADS; token mahasiswa lokal lama ditolak. Akun yang sudah ditautkan ke SADS
tidak dapat masuk kembali lewat password lokal saat mode dikembalikan ke local.

## Alur dan kontrak frontend

Endpoint dan bentuk respons frontend tetap:

```http
POST /api/auth/login
Content-Type: application/json
```

```json
{"identifier":"NIM_mahasiswa","password":"password_SADS"}
```

Backend memetakan `identifier` ke `username` ketika memanggil SADS. Akun admin
lokal tetap diverifikasi di database lokal dan tidak dikirim ke SADS. Untuk
mahasiswa, backend mengecek izin uji, memvalidasi respons login SADS, menyimpan
profil seperlunya, lalu mengembalikan JWT dan cookie sesi lokal seperti biasa.
Respons tetap `{"token":"...","user":{...}}`.

Adapter mengikuti contoh respons yang diberikan: object top-level dengan
`id1`, `id2`, `nama`, `role.name`, dan `jurusan`. Role aktif harus `Mahasiswa`;
adanya Mahasiswa hanya pada daftar `roles` tidak memberi izin jika role aktif
berbeda. `jurusan` disimpan sebagai kode prodi. `fakultas` baru dibiarkan kosong
karena tidak disediakan; data fakultas lama dipertahankan. `id1`, `id2`, `sbt`,
menu SADS, password SADS, dan token/session SADS tidak disimpan atau dikirim ke
frontend. Arti ID dan `sbt` perlu dikonfirmasi sebelum integrasi lanjutan.

Akun ditautkan menggunakan NIM yang dipakai login. Akun mahasiswa lokal aktif
dengan NIM **persis sama** mempertahankan ID, dokumen, profil fakultas, serta
riwayatnya. Password lokalnya diganti hash acak yang tidak diketahui dan metode
autentikasi menjadi `sads`. Akun nonaktif tidak diaktifkan ulang dan role lain
tidak diubah. Tautan NIM/kata sandi akun SADS tidak dapat diubah melalui endpoint
edit pengguna lokal. Migrasi runtime hanya menambah `users.auth_provider`
dengan default `local`; tidak menghapus akun, dokumen, atau hasil.

Login/logout tetap merotasi versi sesi; throttle akun/IP tetap berlaku. Gangguan
SADS atau penolakan izin manual menghabiskan kuota percobaan IP, tetapi tidak
dianggap password salah. Adapter tidak melakukan retry login otomatis.

Status yang perlu ditangani frontend:

| Status | Arti |
| --- | --- |
| 401 | Kredensial ditolak (HTTP 401/403 upstream) atau sesi tidak berlaku |
| 403 | Izin uji tidak aktif, akun nonaktif, atau role aktif bukan mahasiswa |
| 409 | Akun sedang diperbarui oleh login bersamaan; ulangi login |
| 429 | Batas percobaan login backend tercapai; ikuti Retry-After |
| 502 | Respons SADS tidak sesuai contoh JSON; tidak ada sesi yang diterbitkan |
| 503 | SADS tidak dapat dihubungi, timeout, redirect, atau gangguan upstream |

Kontrak respons **login gagal** dari SADS belum diberikan. Adapter menganggap
HTTP 401/403 sebagai kredensial ditolak. Respons 2xx tanpa profil valid ditolak
dengan 502; setelah pola gagal resmi diketahui, pemetaannya dapat disesuaikan.
Jangan melonggarkan validasi menjadi "semua HTTP 200 berarti berhasil".

## Verifikasi

Pengujian otomatis menggunakan respons SADS tiruan, akun sintetis, dan database
SQLite terisolasi. Termasuk penolakan role lain, tidak adanya fallback lokal,
izin kedaluwarsa/dicabut, preservasi histori, redaksi error, kegagalan jaringan,
batas percobaan, dan penolakan redirect dengan server HTTP loopback untuk test.
Tidak ada request ke endpoint SADS asli dalam suite tersebut.

Setelah mengisi konfigurasi sendiri, uji dari Insomnia ke backend lokal dengan
NIM yang diizinkan. Periksa login benar, password salah, NIM di luar daftar,
`GET /api/auth/me`, dan logout. Bila muncul 502, cocokkan bentuk respons tanpa
membagikan credential/token. Jangan menyalin response mahasiswa asli ke Git.

Pilot ini belum mencakup API mata kuliah, pengiriman hasil ke SADS, pemetaan
pembimbing, atau review. Dokumen mahasiswa tetap privat dan tidak otomatis
masuk repositori pembanding. Sebelum dibuka untuk mahasiswa secara umum, ganti
izin manual dengan validasi KRS resmi beserta pemetaan kode mata kuliahnya.
