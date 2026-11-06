# Uji lokal login SADS

Integrasi ini menguji login mahasiswa menggunakan endpoint SADS yang sudah
dimiliki kampus. Endpoint dan kredensial integrasi cukup diisi sendiri di
`backend/.env`; jangan dibagikan melalui chat atau dimasukkan ke Git. VM baru
diperbarui setelah pengguna push dan secara eksplisit meminta deployment.

Sesuai permintaan uji login, pembatasan mata kuliah dan izin NIM sementara
dihapus. Semua akun dengan kredensial SADS valid dan role aktif `Mahasiswa`
dapat mengakses aplikasi, termasuk yang belum mengambil seminar proposal atau
skripsi. Ini **bukan verifikasi KRS**. Autentikasi, status akun aktif, masa
berlaku JWT, dan batas percobaan login tetap diterapkan. Role dosen dan review
tetap ditangani di SADS, tanpa menambah alur dosen di aplikasi ini.

`SADS_TEST_ALLOWED_USERNAMES` dan `SADS_TEST_ACCESS_EXPIRES_AT` sudah tidak
dibaca. Nilai lama yang masih ada di `.env` tidak membatasi login atau sesi;
tidak perlu mengedit `.env` untuk menerapkan perubahan ini. Nilai tersebut boleh
dihapus sendiri saat pemilik konfigurasi merapikannya. Implementasi dan tes
dilakukan lokal; tidak perlu membuka `.env` VM atau melakukan request nyata ke
SADS untuk memverifikasi perubahan kebijakan ini.

## Konfigurasi

Isi variabel berikut di `.env` lokal:

```dotenv
STUDENT_AUTH_PROVIDER=sads
SADS_LOGIN_URL=
SADS_LOGIN_TIMEOUT_SECONDS=10
SADS_LOGIN_HEADERS_JSON={}
```

- `SADS_LOGIN_URL`: URL HTTPS **lengkap** endpoint login resmi, diisi sendiri.
  Adapter saat ini mengirim `POST` dengan JSON `username` dan `password`.
  Sesuaikan adapter terlebih dahulu bila kontrak Insomnia ternyata berbeda.
- `SADS_LOGIN_HEADERS_JSON`: header autentikasi tambahan jika diwajibkan SADS.
  Contoh bentuk tanpa secret: `{"X-API-Key":"isi-sendiri-di-env"}`. Header ini
  hanya dikirim backend. TLS tetap diverifikasi dan redirect ditolak.
  Default User-Agent mengidentifikasi aplikasi sebagai
  `INSTIKI-SimilarityChecker/1.0`, bukan identitas bawaan library Python.
  Header User-Agent yang diwajibkan kampus dapat diisi melalui object ini.

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
mahasiswa, backend memvalidasi respons login SADS dan role aktif, menyimpan
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
SADS atau blokir perantara menghabiskan kuota percobaan IP, tetapi tidak
menambah penghitung kegagalan autentikasi akun. HTTP 401 dan
penolakan terstruktur 403 tetap dihitung sebagai login gagal. Penolakan role
aktif selain mahasiswa juga tetap ditolak. Adapter tidak melakukan retry login
otomatis. Error hanya memuat status HTTP dan arahan umum; URL, header, dan isi
response privat SADS tidak disertakan.

Status yang perlu ditangani frontend:

| Status | Arti |
| --- | --- |
| 401 | Autentikasi ditolak SADS (HTTP 401), kredensial lokal ditolak, atau sesi tidak berlaku |
| 403 | Role bukan mahasiswa, akun tidak dapat ditautkan, atau SADS menolak akses dengan respons JSON HTTP 403 |
| 409 | Akun sedang diperbarui oleh login bersamaan; ulangi login |
| 429 | Batas percobaan login backend tercapai; ikuti Retry-After |
| 502 | Respons SADS tidak sesuai contoh JSON atau request ditolak dengan HTTP 4xx selain 401/403/429 |
| 503 | SADS tidak terjangkau, timeout, redirect, HTTP 429/5xx, atau blokir HTTP 403 non-JSON/challenge |

Kontrak respons **login gagal** dari SADS belum diberikan. HTTP 401 dilaporkan
sebagai autentikasi ditolak; penyebab bisa kredensial mahasiswa maupun header
integrasi yang kurang. HTTP 403 JSON dilaporkan sebagai akses ditolak tanpa
menyatakan password salah. HTTP 403 non-JSON/challenge dianggap penolakan
server/perantara, bukan kegagalan password. Respons 2xx tanpa profil valid
ditolak dengan 502. Setelah pola gagal resmi diketahui, pemetaannya dapat
disesuaikan. Jangan melonggarkan validasi menjadi "semua HTTP 200 berarti berhasil".

Pada investigasi awal, client Python bawaan menerima HTTP 403/error 1010 dari
domain publik backend, sedangkan client dengan User-Agent aplikasi mendapat
200. Itu **belum membuktikan** endpoint SADS mengalami hal yang sama. Cloudflare
mendokumentasikan bahwa identitas bawaan urllib bisa ditolak dan menyarankan
identitas aplikasi yang jelas:
[Error 1010](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/).
Jangan menonaktifkan TLS, melewati challenge, mengubah firewall, atau mencoba
berulang-ulang untuk mengatasi penolakan. Cocokkan kontrak resmi/izin integrasi
dengan tim SADS bila penolakan berlanjut.

## Verifikasi

Pengujian otomatis menggunakan respons SADS tiruan, akun sintetis, dan database
SQLite terisolasi. Termasuk penolakan role lain, tidak adanya fallback lokal,
login beberapa mahasiswa tanpa daftar izin, pengabaian daftar/tenggat lama,
pemisahan akun dan sesi mahasiswa, preservasi histori, redaksi error, kegagalan
jaringan, batas percobaan, serta penolakan redirect dengan server HTTP loopback.
Tidak ada request ke endpoint SADS asli dalam suite tersebut.

Setelah mengisi konfigurasi sendiri, uji dari Insomnia ke backend lokal dengan
beberapa NIM mahasiswa. Periksa login benar, password salah, penolakan role
selain mahasiswa, `GET /api/auth/me`, dan logout. Bila muncul 502, cocokkan bentuk respons tanpa
membagikan credential/token. Jangan menyalin response mahasiswa asli ke Git.

Pilot ini belum mencakup API mata kuliah, pengiriman hasil ke SADS, pemetaan
pembimbing, atau review. Dokumen mahasiswa tetap privat dan tidak otomatis
masuk repositori pembanding. Validasi KRS resmi beserta pemetaan kode mata
kuliah ditambahkan setelah kontrak API tersedia dan pekerjaan itu diminta.
