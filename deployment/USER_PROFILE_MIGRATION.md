# Penggabungan profil mahasiswa ke users

Profil mahasiswa disimpan langsung di tabel `users` agar tidak memerlukan tabel
ekstensi satu-ke-satu setelah alur pembimbing dihapus. Model ORM `Mahasiswa`
dan relasi `User.mahasiswa` dihapus. Database aplikasi tetap PostgreSQL.

Kolom profil di `users`:

| Kolom | Tipe | Keterangan |
| --- | --- | --- |
| `nama_lengkap` | `VARCHAR(255) NULL` | Nama dari SADS atau pengelolaan pengguna oleh admin |
| `program_studi` | `VARCHAR(255) NULL` | Prodi mahasiswa; SADS menyediakan kode jurusan |
| `fakultas` | `VARCHAR(255) NULL` | Data lama dipertahankan; SADS belum menyediakan nilainya |

NIM tetap berada di `users.identifier`. Akun tanpa nama tetap ditampilkan
menggunakan identifier melalui `User.name`. Kolom prodi/fakultas admin boleh
kosong. Login SADS memperbarui nama/prodi dan mempertahankan fakultas lama;
fakultas mahasiswa SADS baru bernilai string kosong seperti sebelumnya.

## Migrasi database yang sudah ada

Migrasi berjalan melalui `ensure_runtime_columns()` saat startup API, sebelum
aplikasi mulai melayani permintaan. Worker Compose menunggu API sehat sebelum
mulai. Migrasi runtime diserialisasi dengan transaction advisory lock PostgreSQL
agar beberapa proses API yang mulai bersamaan tidak berebut perubahan skema.

Dalam satu transaksi, backend:

1. Menjalankan migrasi workflow pembimbing lama lebih dahulu bila diperlukan.
2. Menambahkan ketiga kolom profil yang belum ada di `users`.
3. Memastikan setiap profil lama mempunyai akun pemilik.
4. Mengarsipkan baris profil lama ke `legacy_workflow_archive` dengan
   `entity_type='student_profile'`, `entity_id=mahasiswa.id`, dan payload JSONB.
5. Menyalin profil ke akun dengan ID yang sama. Kolom `users` yang sudah terisi
   dipertahankan; hanya nilai NULL yang diisi dari profil lama.
6. Menghapus tabel `mahasiswa` tanpa `CASCADE` setelah penyalinan berhasil.

ID akun, identifier, hash password, provider, status aktif, versi sesi,
kepemilikan dokumen, dan riwayat skor dipertahankan. Akun mahasiswa nonaktif
juga tetap dipertahankan beserta profilnya. Arsip hanya menyimpan snapshot
profil sebelum penggabungan, bukan pembaruan profil berikutnya.

Jika ditemukan profil tanpa akun atau dependensi tak terduga yang menghalangi
penghapusan tabel, transaksi dibatalkan seluruhnya. Migrasi dapat diulang:
database yang sudah digabung tidak membuat kembali tabel `mahasiswa`, tidak
menimpa perubahan profil terbaru, dan tidak menggandakan arsip.

## Dampak API dan deployment

Endpoint login, `/api/auth/me`, CRUD pengguna, dan impersonasi tetap memakai
field profil yang sama: `nama_lengkap`, alias `name`, `program_studi`, serta
`fakultas`. Format JWT dan ID akun tetap. Frontend tidak perlu mengganti
endpoint atau struktur request/response untuk penggabungan ini. Tidak ada
variabel `.env` aplikasi baru.

Perubahan dikembangkan dan diuji lokal. Deployment dilakukan setelah pengguna
push dan meminta pembaruan VM. Saat deployment, hentikan proses versi lama dan
buat backup PostgreSQL sebelum menjalankan versi ini, karena skema lama akan
digabung. Kembali ke kode yang masih memakai `mahasiswa` memerlukan pemulihan
skema/data yang sesuai; pergantian branch saja tidak mengembalikan tabel tersebut.

## Pengujian

`tests/test_user_profile_migration.py` menjalankan SQL migrasi asli pada schema
acak di database PostgreSQL khusus tes. Isi
`PROFILE_MIGRATION_TEST_DATABASE_URL` pada proses pengujian dengan URL database
PostgreSQL yang namanya mengandung `test`; jangan arahkan ke database aplikasi.
Variabel ini hanya dipakai pengujian, bukan konfigurasi deployment.

```powershell
python -m pytest -q tests/test_user_profile_migration.py tests/test_user_profiles.py
```

Tes mencakup preservasi akun/profil/skor, akun tanpa profil, akun nonaktif,
nilai fakultas kosong, migrasi parsial, pengulangan, pembatalan transaksi,
database baru, serta startup bersamaan. Tes API juga memeriksa kontrak profil,
login, pembaruan profil, dan pembatasan akses pengelolaan pengguna.
