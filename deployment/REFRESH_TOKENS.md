# Sesi login otomatis 3 hari dan refresh token

Fitur ini dikerjakan lokal. Pengguna push dahulu, lalu meminta pull/restart VM.
Frontend dikerjakan terpisah; file frontend lokal tidak diubah.

## Perilaku

- Setiap login berhasil menerbitkan access JWT dan cookie refresh HttpOnly.
  Access JWT berlaku maksimal 15 menit secara default. Cookie refresh berlaku
  sampai 3 hari sejak login awal.
- Refresh merotasi token sekali pakai dengan tenggat yang sama.
  Reuse token yang telah dipakai mencabut sesi beserta access JWT terkait.
- Login ulang, logout, reset password lokal, perubahan identitas/provider/role,
  penonaktifan atau penghapusan akun membatalkan grant lama. Sesuai perilaku
  sebelumnya, login ulang mencabut sesi akun yang sama di browser lain.
- Mahasiswa SADS diperiksa kembali mata kuliah aktifnya saat refresh. Tidak ada
  password atau token SADS yang disimpan untuk refresh. Penolakan mata kuliah
  mencabut akses; gangguan SADS sementara tidak mengonsumsi token refresh.

Perubahan sandi atau logout yang hanya terjadi di SADS belum mempunyai kanal
revocation ke backend ini. Verifikasi refresh memakai API mata kuliah yang
tersedia dan status akun lokal; backend tidak mengirim ulang password mahasiswa.
Pencabutan sesi otomatis dari SADS memerlukan kontrak notifikasi/introspeksi
tambahan dari kampus, bukan penyimpanan sandi di aplikasi ini.

Pola rotasi dan pencabutan mengikuti prinsip
[RFC 9700 bagian 4.14](https://www.rfc-editor.org/rfc/rfc9700.html#section-4.14).
Cookie mengikuti konfigurasi Secure/SameSite/Domain deployment yang sudah ada,
dengan prinsip penyimpanan cookie dari
[OWASP Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).

## Kontrak untuk frontend

Kirim kredensial ke `POST /api/auth/login`.

```json
{
  "identifier": "USERNAME",
  "password": "PASSWORD_DARI_FORM"
}
```

`login_as` dapat disertakan oleh akun yang punya pilihan mode. Field boolean
`remember_me` boleh dikirim oleh klien lama dan diabaikan saat login. Jalankan
alur refresh untuk semua sesi. Request login dan request berikutnya memakai
`credentials: "include"`.

Respons login/refresh/session-mode mempertahankan `token`, `user`, `csrf_token`,
dan menambah:

```json
{
  "expires_in": 899,
  "remember_me": true,
  "refresh_expires_at": "2099-01-04T12:00:00Z",
  "refresh_csrf_token": "HMAC_UNTUK_REFRESH"
}
```

Contoh tersebut menggambarkan login pada 1 Januari 2099 pukul 12.00 UTC.
`refresh_expires_at` adalah tenggat UTC absolut, `expires_in` adalah sisa detik
access JWT, dan `remember_me: true` menandakan sesi punya cookie refresh.

Refresh token mentah **tidak dikembalikan dalam JSON** dan tidak boleh disimpan
di localStorage. Browser menyimpan cookie `<SESSION_COOKIE_NAME>_refresh`
secara HttpOnly, dengan Path `/api/auth`. Access JWT tetap dikembalikan sebagai
`token` untuk kompatibilitas; request berbasis cookie tidak memerlukan Bearer.

Untuk memperbarui access token:

```http
POST /api/auth/refresh
X-CSRF-Token: NILAI_REFRESH_CSRF_TOKEN
```

Gunakan cookie/`credentials: "include"`, tanpa body dan **tanpa Authorization**.
Header CSRF di sini memakai `refresh_csrf_token`, bukan `csrf_token` access JWT.
Pada respons berhasil, ganti token/user dan kedua nilai CSRF secara bersamaan.
Browser menerima cookie refresh yang baru secara otomatis.

Ketika halaman/browser dibuka lagi, frontend dapat memanggil:

```http
GET /api/auth/csrf
```

Dengan cookie yang disertakan, respons menambah `refresh_csrf_token` jika
cookie refresh tersedia, termasuk ketika access cookie sudah hilang atau
JWT-nya kedaluwarsa. Endpoint ini hanya menghasilkan CSRF, bukan login/refresh.
`csrf_token` tetap mewakili access/overlay cookie untuk operasi lain.

Alur client yang disarankan:

1. Saat memulihkan halaman, panggil `/api/auth/me` dengan cookie dan tanpa
   Bearer kedaluwarsa. Jika masih valid, gunakan identitas itu.
2. Pada 401 dari request yang memerlukan login, lakukan satu proses refresh:
   ambil CSRF bila belum ada, lalu POST `/api/auth/refresh`.
3. Jika refresh sukses, simpan state terbaru dan ulangi request asal paling
   banyak sekali. Jangan membuat loop refresh pada login/refresh/logout sendiri.
4. Satukan request refresh bersamaan menjadi satu proses dan koordinasikan
   antar-tab. Dua request dengan refresh token yang sama dianggap reuse dan
   mencabut grant.
5. Pada 401 refresh atau 403 `refresh_access_denied`, kembali ke login. Pada
   429 ikuti `Retry-After`; pada 502/503 tampilkan gangguan sementara dan jangan
   menghapus state/cookie atau mengulang terus-menerus.

Jika Next.js bertindak sebagai proxy/BFF, teruskan cookie request dan **semua**
header `Set-Cookie` respons sebagai header terpisah. Login/refresh kini dapat
mengirim beberapa cookie. Browser memerlukan cookie refresh untuk memulihkan sesi.
Respons auth/CSRF menggunakan `Cache-Control: no-store`.

## Logout, mode mahasiswa, dan impersonasi

`POST /api/auth/logout` dapat memakai `refresh_csrf_token` ketika access token
sudah habis. Untuk alur ini gunakan cookie tanpa Authorization kedaluwarsa.
Logout sesi utama mencabut refresh beserta access JWT dan menghapus cookie.

Pergantian admin ke mode mahasiswa mempertahankan tenggat sesi
awal dan menerbitkan grant baru yang terkunci ke mode mahasiswa. Refresh tidak
bisa menaikkan kembali ke admin dengan menambahkan `login_as`/role ke request.
Kembali ke admin dari mode mahasiswa tetap memerlukan login seperti sebelumnya.

Impersonasi memakai JWT berumur pendek. Grant refresh berasal dari sesi admin
yang memakai cookie terpisah. Selama impersonasi
belum diakhiri, refresh memberi 403 dengan `error_code=impersonation_active`,
termasuk jika overlay cookie sudah kedaluwarsa/hilang. Jangan otomatis berpindah
role atau logout hanya karena error ini; tampilkan aksi kembali ke admin.

Gunakan aksi eksplisit `POST /api/auth/impersonation/stop` dan `session_id` yang
sudah diperoleh. Cookie refresh admin dapat memulihkan parent yang access JWT-nya
sudah kedaluwarsa. Request ini memakai cookie, tanpa Bearer kedaluwarsa, serta
CSRF overlay atau `refresh_csrf_token`. Backend memverifikasi kepemilikan admin
dan sesi impersonasi, mengakhiri overlay, lalu mengembalikan access admin baru
dan refresh yang dirotasi dengan deadline yang sama.

Logout ketika overlay masih valid tetap hanya menutup overlay seperti sebelumnya.
Jika seluruh access token telah habis, logout melalui refresh menutup sesi utama.

## Konfigurasi dan database

`REMEMBER_ACCESS_EXPIRE_MINUTES` mengatur batas access JWT pada login dan
refresh.

```env
REMEMBER_ACCESS_EXPIRE_MINUTES=15
```

Nilai maksimum pengaturan access ini 60 menit. `JWT_EXPIRE_MINUTES` menjadi
batas tambahan jika nilainya lebih pendek. Durasi refresh ditetapkan backend
ke 3 hari. Deployment dengan `REFRESH_TOKEN_EXPIRE_DAYS=30` di konfigurasi
lama tetap dapat dimulai dan memakai tenggat baru.

Startup menambahkan dua tabel PostgreSQL:

- `auth_refresh_sessions`: akun, versi sesi, fingerprint konteks autentikasi,
  mode akun, status verifikasi SADS, deadline absolut, revocation, waktu rotasi,
  dan pengikat impersonasi.
- `auth_refresh_tokens`: hash SHA-256 token acak 256 bit, referensi grant,
  waktu dibuat/dipakai. Token mentah tidak masuk database.

Startup memendekkan tenggat sesi lama menjadi maksimal 3 hari sejak login
awal, termasuk grant pengganti setelah pergantian mode. Sesi lama yang sudah
melewati tenggat meminta login ulang.

Row lock akun menyerialisasi login, logout, dan rotasi. Token yang sudah
dipakai disimpan sampai grant kedaluwarsa untuk mendeteksi replay. Grant
kedaluwarsa dibersihkan bertahap saat login. Penghapusan akun membersihkan
grant dan token terkait lewat foreign key cascade.

Refresh sukses dibatasi maksimal sekali per 60 detik (lebih pendek jika access
JWT diatur sangat singkat). Respons 429 tidak mengonsumsi token atau menghapus
cookie; ikuti `Retry-After`. Token lama yang direplay tetap mencabut grant.

## Verifikasi

Pengujian memakai data sintetis dan PostgreSQL lokal terpisah. Tidak diperlukan
kredensial kampus, PDF kampus, URL/key privat, atau akses VM untuk pengembangan.

```powershell
python -m pytest tests/test_refresh_tokens.py tests/test_refresh_sads_impersonation.py -q
```

Untuk uji PostgreSQL, isi `IMPORT_TEST_DATABASE_URL` dengan database khusus tes
yang namanya memuat `test`, lalu jalankan `tests/test_refresh_tokens_postgresql.py`.
Pengujian mencakup concurrency/reuse/logout, expiry, migrasi berulang, cascade,
CSRF, perubahan akun, pengecekan ulang mata kuliah, dan recovery impersonasi.
