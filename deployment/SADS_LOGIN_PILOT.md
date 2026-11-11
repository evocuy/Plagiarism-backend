# Login SADS dan akses mata kuliah aktif

Integrasi ini menguji login mahasiswa menggunakan endpoint SADS yang sudah
dimiliki kampus. Endpoint dan kredensial integrasi cukup diisi sendiri di
`backend/.env`; jangan dibagikan melalui chat atau dimasukkan ke Git. VM baru
diperbarui setelah pengguna push dan secara eksplisit meminta deployment.

Mahasiswa harus memiliki kredensial SADS valid, role aktif `Mahasiswa`, dan
mengambil mata kuliah `Seminar`, `Skripsi`, atau `Tugas Akhir` pada periode aktif dari API mata
kuliah kampus. Nama ini dikonfirmasi pengguna; pencocokan nama utuh mengabaikan
kapitalisasi dan normalisasi spasi. `Seminar Proposal`, `Seminar Nasional`, dan
`Pra Skripsi` tidak dianggap nama mata kuliah yang sama. Kode `kdmk` belum
dipakai sebagai aturan karena kode resmi ketiga mata kuliah belum diberikan.
Role dosen dan review tetap ditangani di SADS.

`SADS_TEST_ALLOWED_USERNAMES` dan `SADS_TEST_ACCESS_EXPIRES_AT` sudah tidak
dibaca dan tidak dapat melewati pemeriksaan mata kuliah. Implementasi dan tes
dilakukan lokal dengan data sintetis; response mahasiswa asli tidak dimasukkan
ke Git. Daftar izin NIM manual tidak menggantikan verifikasi mata kuliah.

## Konfigurasi

Isi variabel berikut di `.env` lokal:

```dotenv
STUDENT_AUTH_PROVIDER=sads
SADS_LOGIN_URL=
SADS_LOGIN_TIMEOUT_SECONDS=10
SADS_LOGIN_HEADERS_JSON={}
SADS_ACTIVE_COURSES_URL_TEMPLATE=
SADS_ACTIVE_COURSES_TIMEOUT_SECONDS=10
SADS_ACTIVE_COURSES_HEADERS_JSON={"secret":"isi-sendiri-di-env"}
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
- `SADS_ACTIVE_COURSES_URL_TEMPLATE`: URL HTTPS lengkap endpoint mata kuliah
  aktif dengan tepat satu placeholder `{nim}` di path, mengikuti endpoint
  privat yang disediakan pengguna. Backend menggantinya dengan NIM hasil login
  yang di-encode sebagai komponen URL dan mengirim `GET` tanpa body.
- `SADS_ACTIVE_COURSES_HEADERS_JSON`: header wajib `secret` dengan nilai tetap,
  dikonfirmasi pengguna. Nilainya hanya di `.env`. Header login tidak otomatis
  diteruskan ke endpoint mata kuliah; konfigurasi keduanya terpisah.
- Timeout mata kuliah default 10 detik, dapat diatur antara 1–30 detik. TLS
  diverifikasi, redirect ditolak, dan respons dibatasi 256 KiB. Konfigurasi
  URL/header mata kuliah yang belum lengkap memblokir login mahasiswa dengan
  503; admin lokal tetap dapat masuk untuk pengelolaan.

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
mahasiswa, backend memvalidasi respons login SADS dan role aktif, lalu memanggil
API mata kuliah. `mahasiswa.nim` harus cocok dengan NIM login, `periode` harus
terisi, dan `jadwal_matakuliah` harus berupa daftar mata kuliah valid. Semua
baris dianggap berada pada periode aktif sesuai kontrak endpoint; data jadwal
historis tidak dicampurkan. Jika ada `matkul` Seminar/Skripsi/Tugas Akhir, backend menyimpan
profil seperlunya lalu mengembalikan JWT dan cookie sesi lokal seperti biasa.
Respons tetap `{"token":"...","user":{...}}`.

Tanpa mata kuliah yang diizinkan, backend mengembalikan 403 tanpa membuat akun
atau menerbitkan sesi baru. Gangguan/penolakan HTTP API mata kuliah menghasilkan
503, sedangkan respons rusak atau NIM tidak cocok menghasilkan 502. Keduanya
memblokir login baru dan tidak dianggap salah password. Raw response, URL,
header `secret`, daftar dosen, dan jadwal kuliah tidak disimpan atau diteruskan
ke frontend.

JWT memuat penanda boolean verifikasi mata kuliah. Token mahasiswa SADS lama
tanpa penanda ini ditolak dengan 401 dan harus login ulang. Verifikasi dilakukan
setiap login, bukan pada setiap polling hasil. Izin mengikuti masa sesi JWT
(`JWT_EXPIRE_MINUTES`, contoh konfigurasi 60 menit); perubahan KRS baru diperiksa
saat login berikutnya. Jika pemeriksaan login berikutnya memastikan mahasiswa
sudah tidak memenuhi syarat, sesi sebelumnya ikut dicabut. Kegagalan password
atau gangguan API tidak mencabut sesi yang sebelumnya sudah terverifikasi.

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
menambah penghitung kegagalan autentikasi akun. HTTP 401, penolakan terstruktur
403, dan penolakan validasi 422 tetap dihitung sebagai login gagal. Penolakan role
aktif selain mahasiswa juga tetap ditolak. Adapter tidak melakukan retry login
otomatis. Error hanya memuat status HTTP dan arahan umum; URL, header, dan isi
response privat SADS tidak disertakan.

Status yang perlu ditangani frontend:

| Status | Arti |
| --- | --- |
| 401 | Autentikasi ditolak SADS (HTTP 401), kredensial lokal ditolak, atau sesi tidak berlaku |
| 403 | Tidak mengambil Seminar/Skripsi/Tugas Akhir aktif, role bukan mahasiswa, akun tidak dapat ditautkan, atau login SADS ditolak dengan JSON HTTP 403 |
| 409 | Akun sedang diperbarui oleh login bersamaan; ulangi login |
| 422 | Data login ditolak SADS; pesan aman meminta pengguna memeriksa NIM dan kata sandi |
| 429 | Batas percobaan login backend tercapai; ikuti Retry-After |
| 502 | Respons login/mata kuliah SADS rusak, NIM mata kuliah tidak cocok, atau request login ditolak dengan HTTP 4xx lainnya |
| 503 | Verifikasi mata kuliah belum dikonfigurasi atau API tidak tersedia; termasuk timeout, redirect, dan penolakan HTTP API mata kuliah |

Kontrak respons **login gagal** dari SADS belum diberikan. HTTP 401 dilaporkan
sebagai autentikasi ditolak; penyebab bisa kredensial mahasiswa maupun header
integrasi yang kurang. HTTP 403 JSON dilaporkan sebagai akses ditolak tanpa
menyatakan password salah. HTTP 403 non-JSON/challenge dianggap penolakan
server/perantara, bukan kegagalan password. Dalam satu uji VM dengan password
tiruan, SADS mengembalikan HTTP 422. Backend mempertahankan status 422 dengan
pesan `Data login tidak valid. Periksa NIM dan kata sandi Anda.`; status ini
tidak lagi diubah menjadi 502/gangguan server. Detail/body privat SADS tetap
tidak diteruskan. Ini juga mempertahankan batas lima login gagal per akun sesuai
konfigurasi. Login gagal tidak mengubah profil, password lokal, atau sesi yang
sudah valid. Respons 2xx tanpa profil valid ditolak dengan 502. Setelah pola
gagal resmi lainnya diketahui, pemetaannya dapat disesuaikan. Jangan melonggarkan
validasi menjadi "semua HTTP 200 berarti berhasil".

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
login beberapa mahasiswa yang memenuhi syarat, pengabaian daftar/tenggat lama,
pemisahan akun dan sesi mahasiswa, preservasi histori, redaksi error, kegagalan
jaringan, batas percobaan, serta penolakan redirect dengan server HTTP loopback.
Termasuk mata kuliah yang diizinkan, penolakan nama mirip, NIM yang tidak cocok,
respons mata kuliah rusak, header server, serta penolakan token lama yang belum
terverifikasi. Tidak ada request ke endpoint SADS asli dalam suite tersebut.

Setelah mengisi konfigurasi sendiri, uji dari Insomnia ke backend lokal dengan
beberapa NIM mahasiswa. Periksa mahasiswa dengan Seminar/Skripsi/Tugas Akhir aktif,
mahasiswa tanpa ketiganya (403), password salah, penolakan role selain mahasiswa,
`GET /api/auth/me`, dan logout. Bila muncul 502, cocokkan bentuk respons tanpa
membagikan credential/token. Jangan menyalin response mahasiswa asli ke Git.

Integrasi ini belum mencakup pengiriman hasil ke SADS, pemetaan pembimbing,
atau review. Dokumen mahasiswa tetap privat dan tidak otomatis masuk repositori
pembanding. Tidak ada perubahan endpoint frontend atau migrasi database untuk
pembatasan mata kuliah ini. Deployment VM menunggu push dan permintaan pengguna;
URL beserta header mata kuliah juga perlu diisi pada konfigurasi VM saat itu.
