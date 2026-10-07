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


def test_sentence_matching_returns_partial_segment_below_sentence_threshold():
    service = SimilarityService()

    matches = service.find_sentence_matches(
        target_sentences=[
            {
                "page": 4,
                "sentence": (
                    "Sistem informasi akademik digunakan pada layanan kampus digital "
                    "untuk memantau bimbingan skripsi mahasiswa."
                ),
            }
        ],
        reference_sentences=[
            {
                "page": 9,
                "sentence": (
                    "Sistem informasi akademik digunakan untuk mengelola data nilai "
                    "jadwal pembayaran dosen kelas ruangan semester kurikulum."
                ),
            }
        ],
        matched_source="Dokumen Repository B",
        threshold=0.70,
    )

    assert len(matches) == 1
    assert matches[0]["match_type"] == "segment"
    assert matches[0]["sentence"] == "Sistem informasi akademik digunakan"
    assert matches[0]["reference_sentence"] == "Sistem informasi akademik digunakan"
    assert matches[0]["start_position"] == 0
    assert matches[0]["end_position"] == len("Sistem informasi akademik digunakan")
    assert matches[0]["similarity"] < 70.0


def test_sentence_matching_does_not_highlight_whole_sentence_for_reference_subset():
    service = SimilarityService()
    target_sentence = (
        "Sistem informasi akademik digunakan untuk mengelola data mahasiswa "
        "dengan fitur tambahan laporan nilai dan jadwal kuliah."
    )

    matches = service.find_sentence_matches(
        target_sentences=[
            {
                "page": 5,
                "sentence": target_sentence,
            }
        ],
        reference_sentences=[
            {
                "page": 12,
                "sentence": "Sistem informasi akademik digunakan untuk mengelola data mahasiswa.",
            }
        ],
        matched_source="Dokumen Repository C",
        threshold=0.70,
    )

    assert len(matches) == 1
    assert matches[0]["match_type"] == "segment"
    assert matches[0]["sentence"] != target_sentence
    assert matches[0]["sentence"] == "Sistem informasi akademik digunakan untuk mengelola data mahasiswa"


def test_sentence_matching_ignores_segment_that_only_matches_after_stopword_removal():
    service = SimilarityService()

    matches = service.find_sentence_matches(
        target_sentences=[
            {
                "page": 2,
                "sentence": "Sistem informasi akademik terintegrasi kampus digital modern.",
            }
        ],
        reference_sentences=[
            {
                "page": 4,
                "sentence": "Sistem untuk informasi bagi akademik dengan terintegrasi pada kampus digital modern.",
            }
        ],
        matched_source="Dokumen Repository D",
        threshold=0.70,
    )

    assert matches == []
