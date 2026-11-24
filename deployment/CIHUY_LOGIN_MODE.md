# Pilihan login admin dan mahasiswa untuk akun uji

Fitur backend ini dikembangkan dan diuji lokal. Deployment menunggu pengguna
push dan meminta pull/restart VM. Tidak ada perubahan file frontend lokal.

Akun lokal `cihuy` yang aktif dan role database-nya admin/super_admin bisa memilih
mode admin atau mahasiswa. Mode mahasiswa tetap menggunakan ID/identifier
`cihuy`; tidak memakai NIM mahasiswa lain dan tidak mengubah role akun di database.
Password login, profil, dan dokumen lama tetap disimpan seperti sebelumnya.
Mode akun lokal ini tersedia tanpa migrasi schema atau konfigurasi tambahan.

Kedua mode akun lokal `cihuy` memakai kata sandi lokal.
Fitur ini terpisah dari impersonasi NIM, yang tetap memerlukan password khusus.

## Tester dengan akun SADS

Di lingkungan uji yang memakai `STUDENT_AUTH_PROVIDER=sads`, isi
`SADS_TESTER_NIMS` dengan NIM yang disetujui, dipisahkan koma. Nilai default
kosong. Setelah `.env` berubah, buat ulang container API dan worker agar daftar
izin terbaca di kedua layanan.

Tester login dengan NIM dan kata sandi SADS. Setelah SADS menerima login,
tester bisa memilih mode admin atau mahasiswa tanpa verifikasi mata kuliah
aktif. Role di tabel `users` tetap `mahasiswa`, dengan hak akses ditentukan
oleh mode sesi. Mode admin memberikan akses admin pada lingkungan itu.
Mahasiswa lain tetap mengikuti syarat mata kuliah aktif.

Kosongkan daftar atau hapus NIM yang sudah selesai menguji, lalu buat ulang
container API dan worker. Sesi mode uji untuk NIM yang dikeluarkan akan ditolak.

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

Gunakan sesi Admin yang aktif. Request cookie memakai `credentials: "include"`
dan `X-CSRF-Token` dari sesi tersebut. Klien Bearer-only memakai header
`Authorization`. Endpoint menerima `login_as` admin dari sesi Admin atau dari
mode Mahasiswa milik akun yang memiliki kedua pilihan mode.

Respons tetap `{token, user, csrf_token}`. Frontend mengganti ketiganya bersama,
lalu membuka dashboard sesuai `user.role`. Cookie sesi ikut diganti oleh backend.
Pemilihan mode mencabut token lama akun, menerbitkan token dan CSRF baru, dan
mempertahankan batas kedaluwarsa sebelumnya. JWT mode mahasiswa membawa hak
akses mahasiswa.

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
bisa mengganti role melalui field ini, kecuali tester SADS yang terdaftar.
Pilihan yang tidak berwenang menghasilkan
403; nilai di luar admin/mahasiswa menghasilkan 422. Identitas tetap diverifikasi
dengan password dan pembatasan login yang sudah ada.

## Upload tanpa kuota harian

Kuota harian pemeriksaan dikecualikan untuk akun lokal `cihuy` dan tester SADS
yang terdaftar. Backend
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

Panggil endpoint yang sama dengan sesi Mahasiswa milik akun sendiri yang
masih aktif dan berhak memilih kedua mode.

```http
POST /api/auth/session-mode
Content-Type: application/json
X-CSRF-Token: CSRF_SESI_MAHASISWA

{"login_as":"admin"}
```

Setelah HTTP 200, simpan `user`, `token`, `csrf_token`, dan
`refresh_csrf_token` dari respons yang baru, lalu buka dashboard Admin.
Untuk sesi impersonasi, gunakan `POST /api/auth/impersonation/stop` dengan
kredensial admin asli dan ID sesi impersonasi.
