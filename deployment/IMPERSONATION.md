# Impersonasi mahasiswa oleh admin

Admin yang sudah login dapat menguji aplikasi sebagai mahasiswa dengan
memasukkan NIM, tanpa password mahasiswa. Backend memeriksa NIM dan mata kuliah
melalui API mata kuliah aktif SADS. Tetap wajib mengambil `Seminar` atau
`Skripsi`; konfigurasi API yang belum lengkap, gangguan API, atau data identitas
yang tidak cocok tidak memberi akses. API login SADS tidak dipanggil untuk
impersonasi dan nilai header `secret` tetap di backend.

Akun mahasiswa nonaktif dan role selain mahasiswa tidak dapat dijadikan target.
NIM yang belum pernah login dibuat sebagai akun SADS dari `mahasiswa.nama` dan
`mahasiswa.prodi_kode` yang telah diverifikasi oleh API mata kuliah. Akun yang
sudah ada mempertahankan password, provider, profil, dokumen, dan versi sesi.

## Mulai impersonasi

Gunakan credential admin biasa dan endpoint berikut:

```http
POST /api/auth/impersonation
Content-Type: application/json

{"nim":"NIM_MAHASISWA"}
```

Respons 200:

```json
{
  "token": "JWT_SESI_IMPERSONASI",
  "user": {"id": 123, "identifier": "NIM_MAHASISWA", "role": "mahasiswa"},
  "impersonation": {
    "active": true,
    "session_id": "ID_SESI",
    "actor": {"id": 1, "identifier": "ADMIN"},
    "target": {"id": 123, "identifier": "NIM_MAHASISWA"},
    "started_at": "2026-11-10T00:00:00Z",
    "expires_at": "2026-11-10T00:15:00Z"
  }
}
```

`user` mengikuti bentuk profil dari endpoint login. Frontend mengganti token
dan profil aktif dengan respons tersebut, menyimpan `session_id`, lalu membuka
dashboard mahasiswa. Tampilkan banner "Mode impersonasi: NIM ..." beserta
tombol kembali ke admin. Halaman frontend lokal tidak diubah oleh pekerjaan ini.

Backend memasang cookie HttpOnly terpisah bernama
`<SESSION_COOKIE_NAME>_impersonation`. Cookie sesi admin asli dipertahankan.
Request biasa menggunakan token impersonasi atau cookie tersebut, dengan izin
mahasiswa dan pembatasan kepemilikan yang sama. Jika mengirim Bearer, isinya
harus sesuai sesi impersonasi aktif agar tidak konflik dengan cookie.
Impersonasi bertingkat dan akses endpoint admin memakai token mahasiswa ditolak.

`GET /api/auth/me` menampilkan profil mahasiswa. Gunakan
`GET /api/auth/impersonation` untuk memulihkan metadata banner setelah reload;
responsnya sama dengan object `impersonation` di atas saat aktif, atau
`{"active":false}` pada sesi biasa. Semua endpoint ini memakai `no-store`.

## Kembali ke admin

```http
POST /api/auth/impersonation/stop
Content-Type: application/json

{"session_id":"ID_SESI"}
```

Endpoint ini wajib memakai **credential admin asli**. Token impersonasi sendiri
tidak dapat ditukar menjadi token admin. Untuk browser yang sudah menyimpan
cookie admin, kirim request dengan `credentials: "include"` dan **tanpa header
Authorization mahasiswa**. Contoh melalui proxy frontend:

```javascript
const response = await fetch("/api/auth/impersonation/stop", {
  method: "POST",
  credentials: "include",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ session_id: impersonation.session_id }),
});
```

Jika API client otomatis menambahkan Bearer aktif, buat opsi untuk meniadakan
header itu pada request stop, atau pakai Bearer admin asli yang telah disimpan
sebelum berpindah sesi. Dengan Bearer admin asli, cookie admin juga harus berasal
dari akun/sesi yang sama. Admin lain tidak dapat memulihkan sesi milik aktor lain.

Respons 200 kembali berisi `token`, `user` admin, dan
`impersonation: {"active":false}`. Frontend mengganti token/profil, menghapus
metadata impersonasi, lalu membuka dashboard admin. Backend menghapus cookie
impersonasi dan mencabut token impersonasi di database. Token admin yang
dikembalikan adalah credential asli dengan batas waktu yang sama.

## Masa sesi dan hasil pengujian

Default impersonasi 15 menit, dapat diatur lewat
`IMPERSONATION_EXPIRE_MINUTES=15` dengan rentang 1–60. Batasnya selalu mengikuti
sisa masa JWT admin jika lebih pendek. Pengecekan mata kuliah dilakukan saat
memulai sesi, bukan pada setiap polling. Dalam satu akun admin hanya ada satu
sesi impersonasi aktif; memulai sesi baru mengakhiri sesi sebelumnya.

Sesi yang kedaluwarsa menghasilkan 401, tanpa beralih diam-diam ke izin admin.
Tombol stop tetap dapat memakai cookie admin yang masih valid. Jika sesi admin
juga sudah kedaluwarsa, pengguna perlu login admin lagi. Login biasa yang
berhasil membersihkan cookie impersonasi. Logout saat impersonasi hanya
mengakhiri sesi impersonasi dan tidak mencabut sesi mahasiswa pemilik akun.

Perubahan versi sesi admin, penonaktifan/penghapusan/demosi admin, perubahan
versi sesi mahasiswa, serta penonaktifan/penghapusan/perubahan role target
menolak token impersonasi. Menghentikan impersonasi tidak mengubah versi sesi
mahasiswa sehingga pemilik akun tetap bisa bekerja.

Upload dan pengecekan yang dilakukan selama impersonasi adalah tindakan nyata
atas akun mahasiswa target dan mengikuti aturan dokumen privat biasa. Riwayat
sesi disimpan di tabel `impersonation_sessions`: ID serta identifier aktor dan
target, versi sesi admin, waktu mulai, batas waktu, waktu selesai, dan alasan
selesai. JWT, password, header `secret`, serta payload SADS tidak disimpan di
tabel audit. Identifier snapshot dipertahankan bila akun dihapus.

Tabel baru dibuat otomatis oleh `Base.metadata.create_all` saat startup API.
Tidak ada penghapusan data historis. Pengembangan dan tes tetap lokal; deployment
menunggu pengguna push dan meminta pull/restart VM.
