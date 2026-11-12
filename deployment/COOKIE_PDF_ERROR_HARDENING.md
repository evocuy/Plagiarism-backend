# CSRF, kesiapan PDF highlight, dan respons error

Perubahan ini dikembangkan lokal. Frontend perlu mengirim header CSRF sebelum
backend versi ini dideploy. Endpoint utama dan skema database tetap.

## Token CSRF untuk sesi cookie

`POST /api/auth/login` tetap menerima JSON identifier/password. Respons sekarang
menambah `csrf_token` di samping `token` dan `user`. Simpan token CSRF dalam
state autentikasi; jangan menaruhnya di URL. Login memverifikasi Origin bila
header tersebut tersedia, tetapi tidak meminta token CSRF dari sesi lama.

Saat halaman dimuat ulang, panggil:

```typescript
const response = await fetch("/api/auth/csrf", { credentials: "include" });
const { csrf_token } = await response.json();
```

Endpoint ini mengembalikan `{ "csrf_token": "..." }` untuk cookie sesi efektif
atau `{ "csrf_token": null }` bila cookie tidak ada. Respons memakai `no-store`.
Endpoint ini tidak memvalidasi status login, tidak memperpanjang JWT, dan tidak
memberikan hak akses. Gunakan `/api/auth/me` untuk mengetahui status login.

Untuk setiap POST/PUT/PATCH/DELETE yang membawa cookie sesi, tambahkan:

```typescript
const response = await fetch("/api/documents/upload-and-check", {
  method: "POST",
  credentials: "include",
  headers: { "X-CSRF-Token": csrfToken },
  body: formData,
});
```

Untuk multipart, biarkan browser mengatur Content-Type beserta boundary-nya.
Untuk JSON, kirim Content-Type application/json seperti sebelumnya. Aturan ini
juga berlaku untuk logout, pengelolaan pengguna/dokumen, dan impersonasi. Adanya
Authorization Bearer tidak membebaskan request dari CSRF bila cookie sesi ikut
terkirim. Klien Bearer-only tanpa cookie tetap dapat mengakses API tanpa token
CSRF, dengan autentikasi/otorisasi normal.

Token diikat ke JWT cookie efektif dengan HMAC dan kunci terpisah yang diturunkan
dari signing key server. Token dari akun/sesi lain tidak dapat dipakai. Origin
asing ditolak walaupun token benar. Request tanpa Origin tetap membutuhkan
token CSRF bila membawa cookie, sehingga proxy yang menghapus Origin tetap
dapat bekerja.

Origin dipercaya dari CORS_ORIGINS (atau ALLOWED_ORIGINS sebagai fallback).
`CSRF_TRUSTED_ORIGINS` opsional menambah daftar origin utuh, misalnya origin
publik API untuk Swagger di balik reverse proxy. Gunakan scheme/host/port yang
tepat tanpa wildcard/path. Header forwarded host/proto tidak dipercaya untuk
mengubah origin. Untuk browser yang memanggil API lintas origin secara langsung,
allowlist CORS tetap harus mencakup origin frontend.

Penolakan memakai HTTP 403 dengan `error_code=csrf_token_invalid` atau
`csrf_origin_rejected`. Keduanya terjadi sebelum handler menjalankan perubahan.
Untuk token yang usang, frontend dapat mengambil ulang token bootstrap dan
mencoba sekali lagi. Hindari mengulang semua error 403 atau error jaringan.

## Impersonasi dan kembali ke admin

Respons start/stop impersonasi juga menambah `csrf_token`. Ganti token CSRF di
state setiap kali sesi efektif berubah, bersamaan dengan token/user yang baru.

Saat kembali ke admin:

1. Gunakan token CSRF dari sesi impersonasi efektif. Jika state hilang atau
   overlay JWT kedaluwarsa, ambil ulang melalui GET `/api/auth/csrf`.
2. POST `/api/auth/impersonation/stop` dengan `credentials: "include"`, header
   X-CSRF-Token, dan body session_id. Tetap hilangkan Authorization mahasiswa
   agar cookie admin asli dipakai, sesuai panduan impersonasi sebelumnya.
3. Simpan token/user/CSRF dari respons admin yang berhasil.

Bootstrap tetap dapat melindungi request stop saat overlay kedaluwarsa atau
rusak. Endpoint stop sendiri tetap wajib memverifikasi sesi admin asli; token
CSRF tidak menggantikan autentikasi atau mengubah mahasiswa menjadi admin.

## Download PDF highlight

GET `/api/plagiarism/check/{id}/download-highlighted` hanya melayani artefak yang
sudah ada ketika status check adalah completed. GET tidak membuat atau menulis
PDF/metadata baru.

| Kondisi | Respons |
| --- | --- |
| Pending/processing | 409, Retry-After: 5 |
| Failed atau status belum berhasil | 409 |
| Completed tetapi artefak hilang/tidak tersedia | 404 |
| Completed dan file tersedia | 200 PDF, private/no-store dan nosniff |
| Bukan pemilik/admin | 403, sebelum memeriksa kesiapan |

History/status tidak menawarkan URL PDF sebelum completed, sekalipun masih ada
path file lama. Frontend memakai status/progress untuk menonaktifkan tombol
selama proses berjalan. Untuk artefak hilang setelah completed, tampilkan pesan
bahwa hasil PDF tidak tersedia dan pengecekan perlu diulang; jangan polling
tanpa batas atau menampilkan salinan kosong sebagai hasil highlight.

## Error publik

Error pemrosesan memakai pesan tetap dan ID referensi. HTTP error internal
yang ditangani menyediakan header `X-Error-ID`; error tak terduga juga memuat
`error_id` dalam JSON. CORS mengekspos X-Error-ID dan Retry-After. Frontend dapat
menampilkan detail aman dan ID tersebut untuk diteruskan ke admin.

Field error lama tetap berupa string/null, termasuk text_extraction_error dan
error pada hasil batch. Isi exception mentah yang sudah tersimpan sebelumnya
disaring saat dibaca; tidak ada migrasi yang menghapus data historis. Error
baru yang disimpan memakai pesan aman dan ID. Log terkait mencatat operasi,
ID, dan tipe exception tanpa body vendor, kredensial, atau parameter SQL.

Respons validasi 422 tetap menyediakan detail dengan type/loc/msg, sambil
menghilangkan input dan ctx agar password/nilai request tidak dipantulkan.
Error autentikasi, batas kuota, dan pesan validasi PDF yang memang aman tetap
memakai status/pesan bisnisnya.
