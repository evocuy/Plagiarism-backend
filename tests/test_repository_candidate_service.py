from app.services.repository_candidate_service import RepositoryCandidateService


class FakeDocument:
    def __init__(self, document_id, title):
        self.id = document_id
        self.title = title


class FakeChunkService:
    def __init__(self, chunks_by_document_id):
        self.chunks_by_document_id = chunks_by_document_id
        self.requested_document_ids = []

    def get_or_build_sentence_chunks(self, document, db):
        self.requested_document_ids.append(document.id)
        return self.chunks_by_document_id.get(document.id, [])

    def to_reference_corpus(self, chunks, matched_source, source_document_id=None):
        return [
            {
                "clean": chunk["clean"],
                "sentence": chunk["sentence"],
                "matched_source": matched_source,
                "source_document_id": source_document_id,
                "source_chunk_index": chunk.get("chunk_index"),
                "source_page": chunk.get("page"),
                "chapter": chunk.get("chapter"),
            }
            for chunk in chunks
        ]


def test_find_candidates_prefers_repository_documents_with_similar_chunks():
    target = FakeDocument(1, "target.pdf")
    similar_repo = FakeDocument(2, "similar.pdf")
    unrelated_repo = FakeDocument(3, "unrelated.pdf")
    chunk_service = FakeChunkService({
        1: [
            {
                "clean": "sistem informasi akademik mengelola data mahasiswa",
                "sentence": "Sistem informasi akademik mengelola data mahasiswa.",
                "chunk_index": 0,
                "page": 1,
                "chapter": "bab_2",
            }
        ],
        2: [
            {
                "clean": "aplikasi sistem informasi akademik untuk data mahasiswa",
                "sentence": "Aplikasi sistem informasi akademik untuk data mahasiswa.",
                "chunk_index": 0,
                "page": 3,
                "chapter": "bab_2",
            }
        ],
        3: [
            {
                "clean": "analisis struktur beton jembatan dan pondasi",
                "sentence": "Analisis struktur beton jembatan dan pondasi.",
                "chunk_index": 0,
                "page": 5,
                "chapter": "bab_2",
            }
        ],
    })
    service = RepositoryCandidateService(document_chunk_service=chunk_service)

    result = service.find_candidates(
        target_document=target,
        repository_documents=[unrelated_repo, similar_repo],
        db=None,
        top_k=1,
    )

    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["document"].id == similar_repo.id
    assert result["candidates"][0]["candidate_score"] > 0.0
    assert result["candidates"][0]["chapter_matched_count"] == 1
    assert {item["source_document_id"] for item in result["reference_corpus"]} == {similar_repo.id}
    assert result["chapter_aware"] is True
    assert result["target_chapters"] == ["bab_2"]


def test_find_candidates_skips_ranking_when_candidate_limit_covers_repository():
    target = FakeDocument(1, "target.pdf")
    first_repo = FakeDocument(2, "first.pdf")
    second_repo = FakeDocument(3, "second.pdf")
    chunk_service = FakeChunkService({
        1: [{"clean": "target text", "sentence": "Target text.", "chunk_index": 0, "page": 1, "chapter": "bab_1"}],
        2: [{"clean": "first text", "sentence": "First text.", "chunk_index": 0, "page": 2, "chapter": "bab_1"}],
        3: [{"clean": "second text", "sentence": "Second text.", "chunk_index": 0, "page": 3, "chapter": "bab_2"}],
    })
    service = RepositoryCandidateService(document_chunk_service=chunk_service)
    service._rank_semantic_candidates = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("semantic ranking must not run when all documents fit the limit")
    )

    result = service.find_candidates(
        target_document=target,
        repository_documents=[first_repo, second_repo],
        db=object(),
        top_k=2,
    )

    assert result["candidate_strategy"] == "all_repository_documents"
    assert result["candidate_strategy_reason"] == "candidate_limit_covers_repository"
    assert result["semantic_search_used"] is False
    assert [candidate["document"].id for candidate in result["candidates"]] == [2, 3]
    assert {item["source_document_id"] for item in result["reference_corpus"]} == {2, 3}


def test_find_candidates_prefers_same_chapter_references_when_available():
    target = FakeDocument(1, "target.pdf")
    wrong_chapter_repo = FakeDocument(2, "wrong-chapter.pdf")
    same_chapter_repo = FakeDocument(3, "same-chapter.pdf")
    chunk_service = FakeChunkService({
        1: [
            {
                "clean": "metode penelitian sistem informasi akademik",
                "sentence": "Metode penelitian sistem informasi akademik.",
                "chunk_index": 0,
                "page": 10,
                "chapter": "bab_3",
            }
        ],
        2: [
            {
                "clean": "metode penelitian sistem informasi akademik",
                "sentence": "Metode penelitian sistem informasi akademik.",
                "chunk_index": 0,
                "page": 2,
                "chapter": "bab_1",
            }
        ],
        3: [
            {
                "clean": "metode penelitian aplikasi akademik",
                "sentence": "Metode penelitian aplikasi akademik.",
                "chunk_index": 0,
                "page": 11,
                "chapter": "bab_3",
            }
        ],
    })
    service = RepositoryCandidateService(document_chunk_service=chunk_service)

    result = service.find_candidates(
        target_document=target,
        repository_documents=[wrong_chapter_repo, same_chapter_repo],
        db=None,
        top_k=1,
    )

    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["document"].id == same_chapter_repo.id
    assert result["candidates"][0]["chapter_matched_count"] == 1
    assert result["chapter_aware"] is True


def test_find_candidates_falls_back_when_no_chunk_similarity_exists():
    target = FakeDocument(1, "target.pdf")
    repo = FakeDocument(2, "repo.pdf")
    service = RepositoryCandidateService(
        document_chunk_service=FakeChunkService({
            1: [],
            2: [],
        })
    )

    result = service.find_candidates(
        target_document=target,
        repository_documents=[repo],
        db=None,
        top_k=1,
    )

    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["document"].id == repo.id
    assert result["candidates"][0]["candidate_score"] == 0.0
    assert result["candidates"][0]["chapter_matched_count"] == 0
    assert result["chapter_aware"] is False


def test_find_candidates_uses_pgvector_semantic_candidates_when_embeddings_exist():
    target = FakeDocument(1, "target.pdf")
    semantic_repo = FakeDocument(2, "semantic.pdf")
    unrelated_repo = FakeDocument(3, "unrelated.pdf")
    chunk_service = FakeChunkService({
        1: [
            {
                "clean": "metode semantic similarity untuk skripsi",
                "sentence": "Metode semantic similarity untuk skripsi.",
                "chunk_index": 0,
                "page": 8,
                "chapter": "bab_3",
            }
        ],
        2: [
            {
                "clean": "pendekatan kemiripan semantic dokumen tugas akhir",
                "sentence": "Pendekatan kemiripan semantic dokumen tugas akhir.",
                "chunk_index": 0,
                "page": 12,
                "chapter": "bab_3",
            }
        ],
        3: [
            {
                "clean": "struktur beton dan analisis pondasi",
                "sentence": "Struktur beton dan analisis pondasi.",
                "chunk_index": 0,
                "page": 7,
                "chapter": "bab_3",
            }
        ],
    })
    service = RepositoryCandidateService(document_chunk_service=chunk_service)
    service.embedding_service.model = "test-model"
    service._fetch_embedded_target_chunks = lambda target_document, db: [
        {
            "id": 10,
            "chapter": "bab_3",
            "chunk_index": 0,
            "cleaned_text": "metode semantic similarity untuk skripsi",
            "embedding": "[1,0]",
        }
    ]
    service._count_repository_embedding_chunks = lambda document_ids, db: 2
    service._count_repository_chunks = lambda document_ids, db: 2

    def fake_query_semantic_matches(**kwargs):
        assert kwargs["chapter"] == "bab_3"
        return [
            {
                "document_id": semantic_repo.id,
                "chapter": "bab_3",
                "score": 0.91,
            },
            {
                "document_id": unrelated_repo.id,
                "chapter": "bab_3",
                "score": 0.12,
            },
        ]

    service._query_semantic_matches = fake_query_semantic_matches

    result = service.find_candidates(
        target_document=target,
        repository_documents=[unrelated_repo, semantic_repo],
        db=object(),
        top_k=1,
    )

    assert result["candidate_strategy"] == "pgvector_semantic"
    assert result["semantic_search_used"] is True
    assert result["semantic_fallback_reason"] is None
    assert result["total_target_embedding_chunks"] == 1
    assert result["total_repository_embedding_chunks"] == 2
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["document"].id == semantic_repo.id
    assert result["candidates"][0]["candidate_score"] == 0.91
    assert {item["source_document_id"] for item in result["reference_corpus"]} == {semantic_repo.id}


def test_find_semantic_sentence_matches_returns_highlight_items():
    target = FakeDocument(1, "target.pdf")
    semantic_repo = FakeDocument(2, "semantic.pdf")
    service = RepositoryCandidateService()
    service.embedding_service.model = "test-model"
    service._fetch_embedded_target_chunks = lambda target_document, db: [
        {
            "id": 10,
            "chapter": "bab_3",
            "page_number": 8,
            "chunk_index": 0,
            "raw_text": "Aplikasi kampus membantu administrasi data peserta didik.",
            "cleaned_text": "aplikasi kampus membantu administrasi data peserta didik",
            "embedding": "[1,0]",
        }
    ]

    def fake_query_semantic_matches(**kwargs):
        assert kwargs["chapter"] == "bab_3"
        return [
            {
                "document_id": semantic_repo.id,
                "chapter": "bab_3",
                "page_number": 12,
                "chunk_index": 4,
                "raw_text": "Sistem informasi akademik digunakan untuk mengelola data mahasiswa.",
                "cleaned_text": "sistem informasi akademik digunakan mengelola data mahasiswa",
                "score": 0.84,
            }
        ]

    service._query_semantic_matches = fake_query_semantic_matches

    matches = service.find_semantic_sentence_matches(
        target_document=target,
        repository_documents=[semantic_repo],
        db=object(),
        min_score=0.78,
    )

    assert len(matches) == 1
    assert matches[0]["match_type"] == "semantic_sentence"
    assert matches[0]["page"] == 8
    assert matches[0]["sentence"] == "Aplikasi kampus membantu administrasi data peserta didik."
    assert matches[0]["reference_sentence"] == "Sistem informasi akademik digunakan untuk mengelola data mahasiswa."
    assert matches[0]["matched_source"] == "semantic.pdf"
    assert matches[0]["similarity"] == 84.0


def test_find_semantic_sentence_matches_ignores_low_scores():
    target = FakeDocument(1, "target.pdf")
    semantic_repo = FakeDocument(2, "semantic.pdf")
    service = RepositoryCandidateService()
    service.embedding_service.model = "test-model"
    service._fetch_embedded_target_chunks = lambda target_document, db: [
        {
            "id": 10,
            "chapter": None,
            "page_number": 8,
            "chunk_index": 0,
            "raw_text": "Kalimat target.",
            "cleaned_text": "kalimat target",
            "embedding": "[1,0]",
        }
    ]
    service._query_semantic_matches = lambda **kwargs: [
        {
            "document_id": semantic_repo.id,
            "page_number": 12,
            "chunk_index": 4,
            "raw_text": "Kalimat referensi.",
            "score": 0.61,
        }
    ]

    matches = service.find_semantic_sentence_matches(
        target_document=target,
        repository_documents=[semantic_repo],
        db=object(),
        min_score=0.78,
    )

    assert matches == []


def test_find_candidates_falls_back_when_repository_embedding_coverage_is_low():
    target = FakeDocument(1, "target.pdf")
    similar_repo = FakeDocument(2, "similar.pdf")
    other_repo = FakeDocument(3, "other.pdf")
    chunk_service = FakeChunkService({
        1: [
            {
                "clean": "sistem rekomendasi kemiripan dokumen",
                "sentence": "Sistem rekomendasi kemiripan dokumen.",
                "chunk_index": 0,
                "page": 1,
                "chapter": "bab_2",
            }
        ],
        2: [
            {
                "clean": "sistem rekomendasi kemiripan dokumen kampus",
                "sentence": "Sistem rekomendasi kemiripan dokumen kampus.",
                "chunk_index": 0,
                "page": 4,
                "chapter": "bab_2",
            }
        ],
        3: [
            {
                "clean": "analisis kualitas air sungai",
                "sentence": "Analisis kualitas air sungai.",
                "chunk_index": 0,
                "page": 9,
                "chapter": "bab_2",
            }
        ],
    })
    service = RepositoryCandidateService(document_chunk_service=chunk_service)
    service.embedding_service.model = "test-model"
    service._fetch_embedded_target_chunks = lambda target_document, db: [
        {
            "id": 10,
            "chapter": "bab_2",
            "chunk_index": 0,
            "cleaned_text": "sistem rekomendasi kemiripan dokumen",
            "embedding": "[1,0]",
        }
    ]
    service._count_repository_embedding_chunks = lambda document_ids, db: 1
    service._count_repository_chunks = lambda document_ids, db: 2

    result = service.find_candidates(
        target_document=target,
        repository_documents=[other_repo, similar_repo],
        db=object(),
        top_k=1,
    )

    assert result["candidate_strategy"] == "tfidf_chunks"
    assert result["semantic_search_used"] is False
    assert result["semantic_fallback_reason"] == "repository_embedding_coverage_low"
    assert result["candidates"][0]["document"].id == similar_repo.id
