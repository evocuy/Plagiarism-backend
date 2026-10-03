from app.services.similarity_service import SimilarityService
from app.services.tfidf_service import TfidfService


def test_pair_similarity_identical_text_is_high():
    service = TfidfService()

    score = service.calculate_pair_similarity(
        "sistem informasi akademik kampus",
        "sistem informasi akademik kampus",
    )

    assert score == 1.0


def test_pair_similarity_empty_text_is_zero():
    service = TfidfService()

    score = service.calculate_pair_similarity("", "sistem informasi akademik")

    assert score == 0.0


def test_find_best_matches_returns_best_corpus_index():
    service = TfidfService()

    matches = service.find_best_matches(
        query_texts=["pengolahan data akademik"],
        corpus_texts=[
            "analisis struktur beton",
            "pengolahan data akademik mahasiswa",
            "perancangan taman kota",
        ],
    )

    assert matches[0]["index"] == 1
    assert matches[0]["score"] > 0.0


def test_sentence_matching_returns_source_metadata():
    service = SimilarityService()

    matches = service.find_sentence_matches(
        target_sentences=[
            {
                "page": 3,
                "sentence": "Sistem informasi akademik digunakan untuk mengelola data mahasiswa.",
            }
        ],
        reference_sentences=[
            {
                "page": 8,
                "sentence": "Sistem informasi akademik digunakan untuk mengelola data mahasiswa.",
            }
        ],
        matched_source="Dokumen Repository A",
        threshold=0.70,
    )

    assert len(matches) == 1
    assert matches[0]["page"] == 3
    assert matches[0]["matched_source"] == "Dokumen Repository A"
    assert matches[0]["similarity"] == 100.0
