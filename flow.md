# Flow Backend Sistem Plagiarism Checker

Dokumen ini memetakan alur backend yang sedang berjalan di kode saat ini. Rujukan utamanya adalah `backend/app/main.py`, `backend/app/routes/*`, `backend/app/services/*`, dan `backend/app/models/schemas.py`.

Catatan penting: backend saat ini belum memakai tabel `document_chapters`. Hasil sumber dan matching text sudah disimpan di `similarity_results` dan `similarity_matches`. Repositori kampus direpresentasikan oleh semua data di tabel `documents`, lalu dokumen target dan dokumen milik user yang sama dikecualikan saat pengecekan repositori.

---

## 1. Arsitektur Backend Saat Ini

![Arsitektur Backend Saat Ini](docs/backend-architecture.svg)

---

## 2. Startup Backend

```mermaid
flowchart TD
    Start([Start uvicorn app.main:app]) --> LoadEnv["load_dotenv()"]
    LoadEnv --> InitDB["Create SQLAlchemy engine from DATABASE_URL"]
    InitDB --> CreateTables["Base.metadata.create_all(bind=engine)"]
    CreateTables --> EnsureColumns["ensure_runtime_columns()<br/>tambah kolom cache text jika belum ada"]
    EnsureColumns --> Seed["seed_initial_users(db)<br/>buat akun awal jika identifier belum ada"]
    Seed --> App["Create FastAPI app"]
    App --> CORS["Read CORS_ORIGINS or ALLOWED_ORIGINS<br/>register CORSMiddleware"]
    CORS --> Routers["Register routers:<br/>/api/auth, /api/documents, /api/plagiarism"]
    Routers --> Health["Register GET /api/health"]
    Health --> Ready([Backend ready])
```

---

## 3. Model Data Backend Saat Ini

```mermaid
erDiagram
    users ||--o| dosen : "profile dosen"
    users ||--o| mahasiswa : "profile mahasiswa"
    dosen ||--o{ mahasiswa : "membimbing"
    users ||--o{ documents : "owns/uploads"
    documents ||--o{ document_chunks : "has cached chunks"
    users ||--o{ plagiarism_checks : "initiates"
    documents ||--o{ plagiarism_checks : "checked"
    plagiarism_checks ||--o{ similarity_results : "stores source scores"
    documents ||--o{ similarity_results : "source document"
    similarity_results ||--o{ similarity_matches : "stores matching text"

    users {
        int id PK
        string identifier UK
        string password
        string role
    }

    dosen {
        int id PK, FK
        string nama_lengkap
    }

    mahasiswa {
        int id PK, FK
        string nama_lengkap
        string program_studi
        string fakultas
        int dosen_pembimbing_id FK
    }

    documents {
        int id PK
        int user_id FK
        string title
        string document_type
        string file_path
        text extracted_text
        text cleaned_text
        json extraction_metadata
        datetime text_extracted_at
        text text_extraction_error
        datetime created_at
    }

    document_chunks {
        int id PK
        int document_id FK
        string chunk_type
        string chapter
        int page_number
        int chunk_index
        text raw_text
        text cleaned_text
        datetime created_at
    }

    plagiarism_checks {
        int id PK
        int document_id FK
        int user_id FK
        float overall_similarity
        string status
        string approval_status
        datetime reviewed_at
        text reviewer_note
        string highlighted_file_path
        datetime created_at
    }

    similarity_results {
        int id PK
        int check_id FK
        int source_document_id FK
        string chapter
        float similarity_score
        datetime created_at
    }

    similarity_matches {
        int id PK
        int result_id FK
        text source_text
        text submitted_text
        float similarity_score
        int page_number
        int start_position
        int end_position
        datetime created_at
    }
```

Status yang dipakai saat ini:

- `PlagiarismCheck.status`: umumnya langsung `completed`.
- `approval_status`: default `belum disetujui`, lalu dapat berubah menjadi `disetujui` atau `revisi`.

---

## 4. Alur Autentikasi

```mermaid
sequenceDiagram
    autonumber
    actor User as User
    participant FE as Frontend
    participant Auth as /api/auth
    participant DB as PostgreSQL

    User->>FE: Isi identifier dan password
    FE->>Auth: POST /api/auth/login
    Auth->>DB: Query users by identifier
    DB-->>Auth: User + profile relation

    alt User tidak ada atau password salah
        Auth-->>FE: 401 Unauthorized
    else Valid
        Auth->>Auth: Generate JWT HS256
        Auth-->>FE: 200 OK + token JSON + cookie plagiarism_session
        FE->>FE: Simpan token ke localStorage
    end

    FE->>Auth: GET /api/auth/me<br/>Authorization Bearer atau cookie
    Auth->>Auth: Decode JWT dan ambil user id
    Auth->>DB: Query user aktif
    DB-->>Auth: User profile flat
    Auth-->>FE: 200 OK
```

Helper auth:

- `get_current_user`: wajib login, membaca Bearer token lebih dulu lalu cookie.
- `get_optional_current_user`: mencoba membaca user, tetapi mengembalikan `None` jika tidak valid.
- `require_admin`: hanya role `admin` atau `super_admin`.

---

## 5. Alur Upload Dokumen

Endpoint: `POST /api/documents/upload`

```mermaid
sequenceDiagram
    autonumber
    actor User as Mahasiswa atau Admin
    participant FE as Frontend Upload Page
    participant Docs as /api/documents/upload
    participant FS as backend/uploads
    participant DB as PostgreSQL

    User->>FE: Pilih tipe dokumen dan file PDF
    FE->>Docs: Multipart form-data<br/>file, document_type, optional user_id
    Docs->>Docs: Validasi filename berakhir dengan ".pdf"
    Docs->>Docs: document_type = strip + lowercase
    Docs->>Docs: Tentukan effective_user_id<br/>current_user.id atau form user_id
    Docs->>FS: Simpan file ke uploads/{filename}
    Docs->>DB: Insert documents
    DB-->>Docs: Document id
    Docs->>Docs: DocumentTextService.get_or_extract_text
    Docs->>DB: Simpan extracted_text, cleaned_text,<br/>extraction_metadata, text_extracted_at
    Docs->>Docs: DocumentChunkService.get_or_build_sentence_chunks
    Docs->>DB: Simpan sentence chunks ke document_chunks
    Docs-->>FE: id, user_id, filename, document_type,<br/>file_path, text_cache_status, chunk_cache_status, message
```

Catatan implementasi saat ini:

- Validasi upload di backend baru memeriksa suffix `.pdf`.
- Nama file asli masih dipakai sebagai nama file di `uploads/`.
- Upload mencoba membuat cache teks dan chunk kalimat langsung. Jika gagal, dokumen tetap tersimpan dan status cache terkait bernilai `failed`.
- Belum ada endpoint backend `GET /api/documents/{id}` di kode saat ini.

---

## 6. Alur Cek Kemiripan Repositori

Endpoint: `POST /api/plagiarism/check-repository/{document_id}`

```mermaid
flowchart TD
    Start([Request check repository]) --> LoadTarget["Query target Document by id"]
    LoadTarget --> TargetFound{"Target ditemukan?"}
    TargetFound -- "Tidak" --> NotFound["404 Dokumen target tidak ditemukan"]
    TargetFound -- "Ya" --> BuildRepo["Query repository documents:<br/>Document.id != target id"]
    BuildRepo --> ExcludeOwner["Jika target_owner_id ada:<br/>exclude Document.user_id == target_owner_id"]
    ExcludeOwner --> HasRepo{"Ada dokumen repo?"}

    HasRepo -- "Tidak" --> EmptyCheck["Create PlagiarismCheck<br/>overall_similarity = 0.0<br/>status = completed"]
    EmptyCheck --> EmptyHighlight["Generate highlighted PDF kosong"]
    EmptyHighlight --> EmptyResponse["Return check_id, 0.0%, matches kosong,<br/>highlighted_pdf_available, chapter_validation"]

    HasRepo -- "Ya" --> ExtractTarget["DocumentTextService.get_or_extract_text(target)"]
    ExtractTarget --> CleanTarget["Ambil cleaned_text target<br/>dari cache atau hasil ekstraksi baru"]
    CleanTarget --> CandidateSearch["RepositoryCandidateService.find_candidates<br/>target chunks vs repo chunks<br/>prioritaskan chapter sama jika tersedia"]
    CandidateSearch --> CandidateDocs["Ambil top-k candidate docs<br/>default candidate_limit = 20"]
    CandidateDocs --> RepoSentences["Ambil candidate reference_corpus<br/>untuk sentence matching"]
    CandidateDocs --> LoopRepo["Loop candidate_docs saja"]

    LoopRepo --> ExtractRepo["DocumentTextService.get_or_extract_text(candidate_doc)"]
    ExtractRepo --> CleanRepo["Ambil cleaned_text repo<br/>dari cache atau hasil ekstraksi baru"]
    CleanRepo --> PairCheck{"Target dan repo punya clean text?"}
    PairCheck -- "Tidak" --> ScoreZero["score = 0.0"]
    PairCheck -- "Ya" --> TfidfDoc["SimilarityService.calculate_clean_text_similarity(target, repo)"]
    TfidfDoc --> CosineDoc["TfidfService + cosine similarity"]
    CosineDoc --> SaveResult["Append result dan update max_score"]
    ScoreZero --> SaveResult
    SaveResult --> MoreRepo{"Masih ada candidate_doc?"}
    MoreRepo -- "Ya" --> LoopRepo

    MoreRepo -- "Tidak" --> SortResults["Sort matches by similarity desc"]
    SortResults --> CreateCheck["Insert plagiarism_checks<br/>overall_similarity = max_score<br/>status = completed"]
    CreateCheck --> SaveResults["Insert similarity_results<br/>untuk setiap source document"]
    SaveResults --> TargetChunks["DocumentChunkService.get_or_build_sentence_chunks(target)"]
    TargetChunks --> SentenceMatch["Run sentence-level matching<br/>target chunks vs repo_sentences_all"]
    SentenceMatch --> SaveMatches["Group by source_document_id<br/>insert similarity_matches"]
    SaveMatches --> Highlight["Generate highlighted PDF<br/>uploads/highlighted/highlighted_check_{id}_*.pdf"]
    Highlight --> UpdateCheck["Update highlighted_file_path"]
    UpdateCheck --> Response["Return highest_similarity_percentage,<br/>matches, total_plagiarized_sentences,<br/>chapter_aware, target_chapters,<br/>highlighted_pdf_available, chapter_validation"]
```

Detail repository check:

- Candidate search memilih top-k dokumen sumber dari `document_chunks` sebelum full document scoring.
- Candidate search memprioritaskan chunk dari BAB yang sama jika `document_chunks.chapter` tersedia.
- Perbandingan dokumen utuh dilakukan hanya terhadap candidate docs.
- Teks dokumen memakai cache `documents.extracted_text` dan `documents.cleaned_text` jika sudah tersedia.
- Matching kalimat memakai cache `document_chunks` jika sudah tersedia.
- `overall_similarity` menyimpan skor tertinggi dari seluruh dokumen repo.
- Source scores disimpan ke `similarity_results`.
- Matching text disimpan ke `similarity_matches`.
- Proses berjalan sinkron di satu HTTP request.

---

## 7. Alur Cek Dua Dokumen Spesifik

Endpoint: `POST /api/plagiarism/check`

```mermaid
flowchart TD
    Start([Request document_id + reference_document_id]) --> LoadDocs["Query doc_a dan doc_b"]
    LoadDocs --> Found{"Keduanya ditemukan?"}
    Found -- "Tidak" --> Missing["404 salah satu atau kedua dokumen tidak ditemukan"]
    Found -- "Ya" --> ExtractA["Extract text doc_a"]
    ExtractA --> ExtractB["Extract text doc_b"]
    ExtractB --> CleanA["Clean text doc_a"]
    CleanA --> CleanB["Clean text doc_b"]
    CleanB --> HasText{"Clean text valid?"}
    HasText -- "Tidak" --> Zero["score = 0.0"]
    HasText -- "Ya" --> Tfidf["SimilarityService.calculate_text_similarity(doc_a, doc_b)"]
    Tfidf --> Cosine["TfidfService + cosine similarity"]
    Cosine --> InsertCheck["Insert plagiarism_checks"]
    Zero --> InsertCheck
    InsertCheck --> SaveResult["Insert similarity_results untuk doc_b"]
    SaveResult --> Sentences["Ambil sentence chunks doc_a dan doc_b"]
    Sentences --> SentenceTFIDF["SimilarityService.find_sentence_matches"]
    SentenceTFIDF --> CompareSentences["Bandingkan setiap kalimat doc_a"]
    CompareSentences --> Threshold{"max similarity >= 0.70?"}
    Threshold -- "Ya" --> AddMatch["Catat plagiarized_sentences"]
    Threshold -- "Tidak" --> Skip["Skip sentence"]
    AddMatch --> SaveMatch["Insert similarity_matches"]
    SaveMatch --> Highlight
    Skip --> Highlight
    Highlight["Generate highlighted PDF"] --> Update["Update highlighted_file_path"]
    Update --> Response["Return check_id, similarity_percentage,<br/>total_plagiarized_sentences, highlighted_pdf_available"]
```

---

## 8. Alur PDFService

```mermaid
flowchart TD
    Start([extract_text_from_pdf]) --> Resolve["Resolve path:<br/>file_path langsung atau fallback uploads/{filename}"]
    Resolve --> Open["Open PDF dengan PyMuPDF"]
    Open --> DetectRange{"filter_bab dan total_pages > 1?"}
    DetectRange -- "Ya" --> FindStart["Cari BAB I atau BAB 1 valid<br/>abaikan daftar isi dan halaman banyak heading BAB"]
    DetectRange -- "Tidak" --> FullRange["Gunakan semua halaman"]
    FindStart --> FindEnd["Cari akhir konten:<br/>BAB IV untuk proposal atau daftar pustaka/lampiran"]
    FullRange --> Validate
    FindEnd --> Validate["detect_chapters_in_doc"]
    Validate --> ExtractPages["Ambil teks halaman start_page sampai end_page"]
    ExtractPages --> Close["Close PDF"]
    Close --> Return["Return full_text, total_pages,<br/>checked_pages_range, chapter_validation, pages"]
```

Validasi BAB:

```mermaid
flowchart TD
    Start([detect_chapters_in_doc]) --> Scan["Scan BAB I sampai BAB VI"]
    Scan --> Type{"document_type berisi<br/>proposal atau sempro?"}
    Type -- "Ya" --> ProposalRule{"Jumlah BAB"}
    ProposalRule -- "> 3" --> PropOverflow["Warning PROPOSAL_CHAPTER_OVERFLOW"]
    ProposalRule -- "< 3" --> PropIncomplete["Warning PROPOSAL_CHAPTER_INCOMPLETE"]
    ProposalRule -- "== 3" --> Valid["Tidak ada warning"]

    Type -- "Tidak" --> SkripsiRule{"Jumlah BAB"}
    SkripsiRule -- "< 5" --> SkripsiIncomplete["Warning SKRIPSI_CHAPTER_INCOMPLETE"]
    SkripsiRule -- ">= 5" --> Valid

    PropOverflow --> Return
    PropIncomplete --> Return
    SkripsiIncomplete --> Return
    Valid --> Return["Return total_chapters_detected,<br/>detected_chapters, has_warning,<br/>warning_type, warning_message"]
```

---

## 9. Pipeline Preprocessing

```mermaid
flowchart LR
    Raw["Raw text dari PDF"] --> Lower["lowercase"]
    Lower --> Regex["Regex cleanup<br/>replace [^a-z\\s] with space"]
    Regex --> Spaces["Normalize whitespace<br/>replace \\s+ with single space"]
    Spaces --> Stopword["Sastrawi StopWordRemover"]
    Stopword --> Stem{"use_stemming?"}
    Stem -- "False, default saat ini" --> Clean["Clean text untuk TF-IDF"]
    Stem -- "True" --> Stemmer["Sastrawi Stemmer<br/>cache kata unik"]
    Stemmer --> Clean
```

Efek penting pipeline saat ini:

- Angka dan karakter non-alfabet dibuang oleh regex.
- Stemming tersedia, tetapi default `False` dan tidak diaktifkan pada route plagiarism saat ini.

---

## 10. Sentence Matching dan Highlight PDF

```mermaid
flowchart TD
    Start([Mulai sentence matching]) --> TargetSent["PDFService.extract_sentences_with_pages(target)"]
    TargetSent --> RepoSent["Korpus repo_sentences_all<br/>hasil ekstraksi dari dokumen pembanding"]
    RepoSent --> CleanRepo["Clean semua kalimat repo"]
    CleanRepo --> Fit["Fit TF-IDF lewat TfidfService"]
    Fit --> LoopTarget["Loop setiap kalimat target"]
    LoopTarget --> CleanTarget["Clean kalimat target"]
    CleanTarget --> Transform["Transform kalimat target"]
    Transform --> Similarity["cosine_similarity(target sentence, all repo sentences)"]
    Similarity --> Threshold{"best similarity >= 0.70?"}
    Threshold -- "Ya" --> Add["Tambahkan ke plagiarized_sentences:<br/>page, sentence, similarity, matched_source, reference_sentence"]
    Threshold -- "Tidak" --> Next["Lanjut kalimat berikutnya"]
    Add --> More{"Masih ada kalimat target?"}
    Next --> More
    More -- "Ya" --> LoopTarget
    More -- "Tidak" --> Highlight["generate_highlighted_pdf"]
```

```mermaid
flowchart TD
    Start([generate_highlighted_pdf]) --> Open["Open target PDF"]
    Open --> Loop["Loop plagiarized_sentences"]
    Loop --> Page{"page tersedia dan valid?"}
    Page -- "Ya" --> SearchPage["Cari di halaman tersebut"]
    Page -- "Tidak" --> SearchAll["Cari di semua halaman"]
    SearchPage --> Exact{"page.search_for(sentence) menemukan rect?"}
    SearchAll --> Exact
    Exact -- "Ya" --> AddExact["add_highlight_annot(rect)<br/>warna kuning"]
    Exact -- "Tidak" --> Chunk["Pecah kalimat per 5 kata<br/>search_for(chunk) jika panjang >= 15"]
    Chunk --> AddChunk["Highlight rect chunk yang ditemukan"]
    AddExact --> Save
    AddChunk --> Save
    Save["Save ke uploads/highlighted/highlighted_check_{id}_*.pdf"] --> Return["Return highlighted_file_path"]
```

---

## 11. Riwayat, Download, dan Approval

```mermaid
flowchart TD
    HistoryStart([GET /api/plagiarism/history]) --> Role{"Role current_user"}
    Role -- "mahasiswa" --> MhsFilter["Filter check.user_id == user.id<br/>atau document.user_id == user.id"]
    Role -- "dosen" --> DosenFilter["Cari mahasiswa bimbingan<br/>filter documents.user_id in bimbingan"]
    Role -- "admin atau tanpa login" --> AllHistory["Ambil semua checks"]
    MhsFilter --> BuildHistory
    DosenFilter --> BuildHistory
    AllHistory --> BuildHistory["Build response history<br/>termasuk owner, status, approval, highlighted_pdf_url"]
```

```mermaid
flowchart TD
    Download([GET /api/plagiarism/check/{id}/download-highlighted]) --> LoadCheck["Query PlagiarismCheck"]
    LoadCheck --> Exists{"Check ada?"}
    Exists -- "Tidak" --> NotFound["404"]
    Exists -- "Ya" --> Access{"_can_access_check?"}
    Access -- "Tidak" --> Forbidden["403"]
    Access -- "Ya" --> FileExists{"highlighted_file_path ada di disk?"}
    FileExists -- "Ya" --> Stream["Return FileResponse inline PDF"]
    FileExists -- "Tidak" --> CanFallback{"Dokumen asli ada?"}
    CanFallback -- "Ya" --> GenerateEmpty["Generate highlighted PDF kosong<br/>lalu simpan path"]
    GenerateEmpty --> Stream
    CanFallback -- "Tidak" --> Missing["404 file tidak tersedia"]
```

```mermaid
stateDiagram-v2
    [*] --> BelumDisetujui: check selesai dibuat
    BelumDisetujui --> Disetujui: PATCH approval_status = disetujui
    BelumDisetujui --> Revisi: PATCH approval_status = revisi
    Revisi --> Disetujui: PATCH approval_status = disetujui
    Disetujui --> Revisi: PATCH approval_status = revisi
```

Endpoint approval: `PATCH /api/plagiarism/check/{check_id}/approval`

- Wajib login.
- Body menerima `approval_status` bernilai `disetujui` atau `revisi`, dengan `reviewer_note` opsional.
- Role `dosen` hanya boleh mereview dokumen mahasiswa bimbingannya.
- Role `admin` dan `super_admin` boleh mereview semua.
- Role lain ditolak.

---

## 12. Endpoint API Aktual

| Method | Endpoint | Auth di Kode | Fungsi |
|---|---|---|---|
| `GET` | `/api/health` | Publik | Health check backend |
| `POST` | `/api/auth/login` | Publik | Login, buat JWT, set cookie `plagiarism_session` |
| `GET` | `/api/auth/me` | Wajib login | Ambil profil user aktif |
| `POST` | `/api/auth/logout` | Publik | Hapus cookie session |
| `GET` | `/api/auth/users` | Admin | List user |
| `GET` | `/api/auth/users/dosen` | Admin | List dosen untuk dropdown |
| `POST` | `/api/auth/users` | Admin | Buat user baru |
| `PUT` | `/api/auth/users/{user_id}` | Admin | Update user |
| `DELETE` | `/api/auth/users/{user_id}` | Admin | Hapus user |
| `POST` | `/api/documents/upload` | Opsional | Upload PDF dan insert metadata dokumen |
| `GET` | `/api/documents/` | Opsional | List dokumen, difilter jika user mahasiswa/dosen |
| `POST` | `/api/documents/reindex` | Opsional + role-aware | Batch rebuild text cache dan sentence chunks |
| `POST` | `/api/documents/{document_id}/reindex` | Opsional + access helper | Rebuild text cache dan sentence chunks satu dokumen |
| `POST` | `/api/documents/embeddings` | Opsional + role-aware | Batch generate dan simpan chunk embeddings |
| `POST` | `/api/documents/{document_id}/embeddings` | Opsional + access helper | Generate dan simpan chunk embeddings satu dokumen |
| `DELETE` | `/api/documents/clear` | Publik di kode | Truncate `documents` dan `plagiarism_checks`, hapus PDF di uploads root |
| `POST` | `/api/plagiarism/check` | Opsional | Cek dua dokumen spesifik |
| `POST` | `/api/plagiarism/check-repository/{document_id}` | Opsional | Cek satu dokumen terhadap repository documents |
| `GET` | `/api/plagiarism/history` | Opsional | Riwayat check, difilter jika user mahasiswa/dosen |
| `GET` | `/api/plagiarism/check/{check_id}/matches` | Opsional + access helper | Detail source score dan matching text tersimpan |
| `GET` | `/api/plagiarism/check/{check_id}/download-highlighted` | Opsional + access helper | Stream PDF hasil highlight |
| `PATCH` | `/api/plagiarism/check/{check_id}/approval` | Wajib login | Dosen/admin mengubah status approval |

Endpoint yang belum ada di backend saat ini:

- `GET /api/documents/{id}`
- `GET /api/plagiarism/check/{id}`
- `GET /api/repository/documents`
- `POST /api/repository/documents`

---

## 13. Batasan Alur Saat Ini

```mermaid
flowchart TD
    A["Processing masih sinkron<br/>di request HTTP"] --> B["Tidak ada queue/background worker"]
    C["TF-IDF dan sentence matching<br/>sudah dipisah ke service"] --> D["Candidate search masih TF-IDF in-process"]
    E["Repository memakai tabel documents"] --> F["Belum ada tabel document_chapters khusus"]
    G["Candidate search belum memakai persistent vector index"] --> H["Belum ada pgvector/FAISS top-k"]
    I["Upload memakai filename asli"] --> J["Perlu sanitasi filename dan validasi MIME/size untuk hardening"]
```

Dokumen ini sengaja menggambarkan kondisi backend yang ada sekarang, bukan rancangan ideal fase berikutnya.
