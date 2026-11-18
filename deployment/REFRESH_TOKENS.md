# Remember me 30 hari dan refresh token

Fitur ini dikerjakan lokal. Pengguna push dahulu, lalu meminta pull/restart VM.
Frontend dikerjakan terpisah; file frontend lokal tidak diubah.

## Perilaku

- `POST /api/auth/login` menerima `remember_me` boolean, default `false`.
- `false`/tidak dikirim: login mengikuti `JWT_EXPIRE_MINUTES` yang sudah ada.
  Backend tidak menerbitkan refresh token dan membersihkan cookie refresh lama.
- `true`: access JWT berlaku paling lama 15 menit secara default (atau durasi
  JWT yang lebih pendek). Refresh token disimpan sebagai cookie HttpOnly selama
  maksimal 30 hari sejak login awal.
- Refresh selalu merotasi token sekali pakai, tanpa menggeser tenggat 30 hari.
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

Login tetap memakai endpoint lama, dengan tambahan checkbox Remember me:

```json
{
  "identifier": "USERNAME",
  "password": "PASSWORD_DARI_FORM",
  "remember_me": true
}
```

`login_as` milik akun cihuy masih dapat disertakan seperti sebelumnya.
Request login dan request berikutnya memakai `credentials: "include"`.

Respons login/refresh/session-mode mempertahankan `token`, `user`, `csrf_token`,
dan menambah:

```json
{
  "expires_in": 899,
  "remember_me": true,
  "refresh_expires_at": "2099-01-31T12:00:00Z",
  "refresh_csrf_token": "HMAC_UNTUK_REFRESH"
}
```

Nilai di atas hanya contoh. `refresh_expires_at` adalah waktu UTC absolut;
`expires_in` adalah sisa detik access JWT. Tanpa Remember me, field refresh
bernilai `null` dan `remember_me=false`.

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
cookie Remember me tersedia, termasuk ketika access cookie sudah hilang atau
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
mengirim beberapa cookie; meneruskan hanya cookie access menghilangkan Remember me.
Respons auth/CSRF menggunakan `Cache-Control: no-store`.

## Logout, mode mahasiswa, dan impersonasi

`POST /api/auth/logout` dapat memakai `refresh_csrf_token` ketika access token
sudah habis. Untuk alur ini gunakan cookie tanpa Authorization kedaluwarsa.
Logout sesi utama mencabut refresh beserta access JWT dan menghapus cookie.

Pergantian cihuy admin ke mode mahasiswa mempertahankan tenggat Remember me
awal dan menerbitkan grant baru yang terkunci ke mode mahasiswa. Refresh tidak
bisa menaikkan kembali ke admin dengan menambahkan `login_as`/role ke request.
Kembali ke admin dari mode mahasiswa tetap memerlukan login seperti sebelumnya.

Impersonasi tetap memiliki durasi pendek dan **tidak memperoleh refresh token
mahasiswa 30 hari**. Cookie parent admin disimpan terpisah. Selama impersonasi
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

Tidak ada secret baru. Dua pengaturan opsional sudah memiliki default:

```env
REMEMBER_ACCESS_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=30
```

Durasi Remember me maksimal 30 hari, dan cap access Remember me maksimal 60
menit. `JWT_EXPIRE_MINUTES` tetap membatasi access jika nilainya lebih pendek.
Pengaturan cookie dan kunci JWT tetap memakai konfigurasi yang sudah ada.

Startup menambahkan dua tabel PostgreSQL:

- `auth_refresh_sessions`: akun, versi sesi, fingerprint konteks autentikasi,
  mode akun, status verifikasi SADS, deadline absolut, revocation, waktu rotasi,
  dan pengikat impersonasi.
- `auth_refresh_tokens`: hash SHA-256 token acak 256 bit, referensi grant,
  waktu dibuat/dipakai. Token mentah tidak masuk database.

Row lock akun menyerialisasi login/logout/rotasi. Token yang sudah dipakai
disimpan sampai grant kedaluwarsa untuk mendeteksi replay. Grant kedaluwarsa
dibersihkan bertahap saat login Remember me baru. User yang dihapus menghapus
grant/token lewat foreign key cascade. Migrasi bersifat penambahan; dokumen,
embedding, skor, dan riwayat pengecekan tidak diubah.

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
