# Pilihan login admin/mahasiswa untuk akun cihuy

Fitur backend ini dikembangkan dan diuji lokal. Deployment menunggu pengguna
push dan meminta pull/restart VM. Tidak ada perubahan file frontend lokal.

Akun lokal `cihuy` yang aktif dan role database-nya admin/super_admin bisa memilih
mode admin atau mahasiswa. Mode mahasiswa tetap menggunakan ID/identifier
`cihuy`; tidak memakai NIM mahasiswa lain dan tidak mengubah role akun di database.
Password login, profil, dan dokumen lama tetap disimpan seperti sebelumnya.
Tidak ada migrasi schema atau tambahan konfigurasi `.env` untuk fitur ini.

Mode mahasiswa akun uji ini tidak memanggil SADS login/mata kuliah. Pengecualian
hanya berlaku untuk identitas admin lokal cihuy yang diverifikasi server. Login
mahasiswa kampus dan persyaratan Seminar/Skripsi/Tugas Akhir tetap berlaku.
Fitur ini terpisah dari impersonasi NIM, yang tetap memerlukan password khusus.

## Pilihan setelah login berhasil

POST `/api/auth/login` dengan identifier/password seperti sebelumnya.
Respons login dan GET `/api/auth/me` menambah dua field pada `user`:

```json
{
  "id": 1,
  "identifier": "cihuy",
  "role": "admin",
  "available_login_modes": ["admin", "mahasiswa"],
  "unlimited_daily_checks": true
}
```

Field profil lainnya tetap ada seperti sebelumnya. Akun biasa mendapat satu
pilihan sesuai role aslinya dan `unlimited_daily_checks=false`.

Frontend menampilkan pilihan **Masuk sebagai Admin** / **Masuk sebagai Mahasiswa**
bila `user.available_login_modes` memuat keduanya. Pilihan Admin dapat langsung
memakai sesi admin hasil login awal dan membuka dashboard admin.

Untuk memilih Mahasiswa setelah login, panggil:

```http
POST /api/auth/session-mode
Content-Type: application/json
X-CSRF-Token: CSRF_SESI_ADMIN

{"login_as":"mahasiswa"}
```

Gunakan credential admin yang baru login. Request cookie memakai credentials
include dan X-CSRF-Token; klien Bearer-only tetap bisa memakai Authorization
admin. Akun lain atau sesi mahasiswa tidak boleh memanggil endpoint ini.
Endpoint juga menerima login_as admin ketika sesi yang sedang dipakai masih admin.

Respons tetap `{token, user, csrf_token}`. Frontend mengganti ketiganya bersama,
lalu membuka dashboard sesuai `user.role`. Cookie sesi ikut diganti oleh backend.
Pemilihan mode mencabut semua token lama akun cihuy, membersihkan overlay
impersonasi, dan mempertahankan batas kedaluwarsa sesi admin sebelumnya.
JWT mode mahasiswa bertanda role mahasiswa dan tidak memiliki hak endpoint admin.

GET `/api/auth/me` tetap mengembalikan role efektif mahasiswa setelah reload.
Simpan/pulihkan CSRF melalui alur yang dijelaskan di
[COOKIE_PDF_ERROR_HARDENING.md](COOKIE_PDF_ERROR_HARDENING.md).

## Alternatif: pilihan pada form login

Frontend juga dapat mengirim pilihan langsung pada satu request login:

```json
{
  "identifier": "cihuy",
  "password": "NILAI_INPUT_PASSWORD_LOGIN",
  "login_as": "mahasiswa"
}
```

`login_as` opsional: jika tidak diberikan, cihuy tetap masuk sebagai admin agar
klien lama bekerja. Isi admin untuk memilih admin secara eksplisit.
Mahasiswa/admin biasa boleh mengirim mode yang sesuai role-nya, tetapi tidak
bisa mengganti role melalui field ini. Pilihan yang tidak berwenang menghasilkan
403; nilai di luar admin/mahasiswa menghasilkan 422. Identitas tetap diverifikasi
dengan password dan pembatasan login yang sudah ada.

## Upload tanpa kuota harian

Kuota harian pemeriksaan dikecualikan untuk akun admin lokal cihuy. Backend
memeriksa identitas asli di database ketika menerima antrean, bukan flag dari
request. Mahasiswa biasa tetap mengikuti CHECK_DAILY_LIMIT (default 3 per hari).
Pengecualian melekat pada akun uji ini, termasuk bila memakai endpoint pengecekan
dari mode admin.

Frontend menggunakan `user.unlimited_daily_checks=true` untuk menampilkan
"Tidak dibatasi per hari" dan menghilangkan penonaktifan tombol upload yang hanya
didasarkan jumlah cek harian. Angka today_check_count tetap menunjukkan jumlah
aktual; jangan mengubahnya menjadi nol atau menganggap semua error 429 sebagai
kuota harian. Antrean global penuh juga dapat menghasilkan 429 + Retry-After.

Batas lain tetap berlaku: ukuran dan validasi PDF, satu pemeriksaan aktif per
akun (default), kapasitas antrean global, batas request/CPU/memori dan timeout.
Unlimited berarti jumlah upload/cek harian, bukan pekerjaan paralel tanpa batas.
Tunggu pengecekan sebelumnya selesai sebelum mengirim naskah berikutnya.

Upload memakai endpoint mahasiswa yang sama, POST `/api/documents/upload-and-check`.
Dokumen dimiliki cihuy, tetap privat (`source_origin=student`, status draft), dan
tidak masuk repositori alumni. Mode mahasiswa hanya melihat dokumen, hasil cek,
dan tiket bantuan milik akun sendiri. Impor/reindex/penghapusan dokumen dan
pengelolaan pengguna tetap memerlukan sesi admin.

## Kembali ke admin

Keluar melalui POST `/api/auth/logout`, bersihkan state token/user/CSRF, lalu login
kembali dan pilih Admin. Sesi mahasiswa tidak dapat meningkatkan dirinya menjadi
admin lewat session-mode atau endpoint stop impersonasi. Mode ini tidak membuat
impersonation_session_id, sehingga jangan memakai alur stop impersonasi untuknya.
Perubahan role/identifier/provider, penonaktifan akun, login baru, reset password,
dan logout mencabut/menolak sesi sesuai aturan autentikasi backend.
