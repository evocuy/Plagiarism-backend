import pymupdf

from app.services import pdf_service as pdf_module
from app.services.pdf_service import PDFService, PDF_EXTRACTION_VERSION
from app.services.similarity_service import SimilarityService


def make_pdf(path, lines):
    with pymupdf.open() as document:
        page = document.new_page()
        for y, text in lines:
            page.insert_text((72, y), text, fontsize=11)
        document.save(str(path))
    return str(path)


def annotation_rects(path):
    with pymupdf.open(path) as document:
        page = document[0]
        return [pymupdf.Rect(annotation.rect) for annotation in page.annots() or []]


def highlighted_pdf(service, path, matches, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_module, "UPLOAD_DIR", tmp_path)
    return service.generate_highlighted_pdf(path, matches, "result.pdf")


def test_identical_structural_headings_do_not_create_similarity_or_highlights(tmp_path, monkeypatch):
    headings = [(72, "BAB I"), (94, "PENDAHULUAN"), (116, "1.1 Latar Belakang")]
    target_path = make_pdf(tmp_path / "target.pdf", headings + [
        (180, "Pemupukan kebun mangga memerlukan unsur hara tanah."),
    ])
    source_path = make_pdf(tmp_path / "source.pdf", headings + [
        (180, "Router jaringan komputer mengirim paket menuju server."),
    ])
    pdf = PDFService()
    similarity = SimilarityService()
    target = pdf.extract_text_from_pdf(target_path)
    source = pdf.extract_text_from_pdf(source_path)
    assert target["extraction_version"] == PDF_EXTRACTION_VERSION
    assert "Latar Belakang" not in target["full_text"]
    assert "BAB I" not in target["full_text"]
    assert target["chapter_validation"]["total_chapters_detected"] == 1
    assert similarity.calculate_text_similarity(target["full_text"], source["full_text"]) == 0
    matches = similarity.find_sentence_matches(
        pdf.extract_sentences_with_pages(target_path),
        pdf.extract_sentences_with_pages(source_path),
        "source.pdf",
    )
    assert matches == []
    output = highlighted_pdf(pdf, target_path, matches, tmp_path, monkeypatch)
    assert annotation_rects(output) == []


def test_copied_body_under_latar_belakang_still_matches(tmp_path, monkeypatch):
    body = "Pengolahan citra digital mendeteksi kematangan buah secara otomatis."
    path = make_pdf(tmp_path / "target.pdf", [
        (72, "BAB I"), (94, "PENDAHULUAN"), (116, "1.1 Latar Belakang"), (180, body),
    ])
    service = PDFService()
    chunks = service.extract_sentences_with_pages(path)
    assert len(chunks) == 1
    assert chunks[0]["sentence"] == body
    assert chunks[0]["page_start_position"] == 0
    assert chunks[0]["page_end_position"] == len(body)
    matches = SimilarityService().find_sentence_matches(chunks, chunks, "source.pdf")
    assert len(matches) == 1
    output = highlighted_pdf(service, path, matches, tmp_path, monkeypatch)
    rectangles = annotation_rects(output)
    assert len(rectangles) == 1
    assert rectangles[0].y0 > 150


def test_phrase_in_heading_and_body_only_highlights_body(tmp_path, monkeypatch):
    path = make_pdf(tmp_path / "target.pdf", [
        (72, "Metode Pengumpulan Data"),
        (180, "Metode Pengumpulan Data dilakukan melalui observasi lapangan."),
    ])
    output = highlighted_pdf(PDFService(), path, [{
        "page": 1, "sentence": "Metode Pengumpulan Data",
        "page_start_position": 0, "page_end_position": len("Metode Pengumpulan Data"),
    }], tmp_path, monkeypatch)
    rectangles = annotation_rects(output)
    assert len(rectangles) == 1
    assert rectangles[0].y0 > 150


def test_page_positions_select_only_second_identical_body_sentence(tmp_path, monkeypatch):
    sentence = "Sistem informasi akademik mengelola data mahasiswa."
    path = make_pdf(tmp_path / "target.pdf", [(180, sentence), (220, sentence)])
    service = PDFService()
    chunks = service.extract_sentences_with_pages(path)
    assert len(chunks) == 2
    assert chunks[1]["page_start_position"] > chunks[0]["page_end_position"]
    output = highlighted_pdf(service, path, [chunks[1]], tmp_path, monkeypatch)
    rectangles = annotation_rects(output)
    assert len(rectangles) == 1
    assert rectangles[0].y0 > 200


def test_ambiguous_legacy_match_is_not_highlighted(tmp_path, monkeypatch):
    sentence = "Sistem informasi akademik mengelola data mahasiswa."
    path = make_pdf(tmp_path / "target.pdf", [(180, sentence), (220, sentence)])
    output = highlighted_pdf(PDFService(), path, [{"page": 1, "sentence": sentence}], tmp_path, monkeypatch)
    assert annotation_rects(output) == []


def test_wrapped_body_sentence_keeps_positions_and_highlights_both_lines(tmp_path, monkeypatch):
    path = make_pdf(tmp_path / "target.pdf", [
        (72, "1.2 Tujuan Penelitian"),
        (180, "Sistem informasi akademik digunakan untuk mengelola"),
        (198, "data mahasiswa dan mempercepat proses bimbingan."),
    ])
    service = PDFService()
    chunks = service.extract_sentences_with_pages(path)
    assert len(chunks) == 1
    output = highlighted_pdf(service, path, chunks, tmp_path, monkeypatch)
    rectangles = annotation_rects(output)
    assert len(rectangles) == 2
    assert all(rectangle.y0 > 150 for rectangle in rectangles)


def test_removed_heading_is_a_sentence_boundary_and_prose_is_preserved(tmp_path):
    path = make_pdf(tmp_path / "target.pdf", [
        (72, "Latar belakang penelitian ini membahas kebutuhan masyarakat"),
        (116, "1.2 Kajian Teknologi Informasi"),
        (180, "BAB I menjelaskan kebutuhan sistem informasi akademik."),
    ])
    service = PDFService()
    chunks = service.extract_sentences_with_pages(path)
    assert [chunk["sentence"] for chunk in chunks] == [
        "Latar belakang penelitian ini membahas kebutuhan masyarakat",
        "BAB I menjelaskan kebutuhan sistem informasi akademik.",
    ]
    assert "1.2 Kajian" not in service.extract_text_from_pdf(path)["full_text"]
    assert "1.2 Kajian" in service.extract_text_from_pdf(path, filter_bab=False)["full_text"]


def test_invalid_offsets_do_not_fall_back_to_broad_text_search(tmp_path, monkeypatch):
    sentence = "Sistem informasi akademik mengelola data mahasiswa."
    path = make_pdf(tmp_path / "target.pdf", [(180, sentence)])
    output = highlighted_pdf(PDFService(), path, [{
        "page": 1, "sentence": sentence,
        "page_start_position": 1, "page_end_position": len(sentence) + 1,
    }], tmp_path, monkeypatch)
    assert annotation_rects(output) == []


def test_partial_match_highlights_last_word_with_trailing_punctuation(tmp_path, monkeypatch):
    path = make_pdf(tmp_path / "target.pdf", [
        (180, 'Penelitian memakai "sistem informasi akademik".'),
    ])
    service = PDFService()
    matches = SimilarityService().find_sentence_matches(
        service.extract_sentences_with_pages(path),
        [{"sentence": "Sistem informasi akademik bermanfaat untuk mahasiswa."}],
        "source.pdf",
    )
    assert len(matches) == 1
    assert matches[0]["sentence"] == "sistem informasi akademik"
    output = highlighted_pdf(service, path, matches, tmp_path, monkeypatch)
    rectangles = annotation_rects(output)
    assert len(rectangles) == 1
    with pymupdf.open(path) as document:
        page = document[0]
        words = page.get_text("words")
        first_word = next(word for word in words if word[4] == '"sistem')
        last_word = next(word for word in words if word[4] == 'akademik".')
        assert rectangles[0].x0 <= first_word[0]
        assert rectangles[0].x1 >= last_word[2]


def test_offsets_inside_unmatched_letters_do_not_highlight_whole_word(tmp_path, monkeypatch):
    path = make_pdf(tmp_path / "target.pdf", [(180, "Menggunakan sistem informasi akademiknya.")])
    service = PDFService()
    body = service.extract_text_from_pdf(path)["full_text"]
    start = body.index("sistem")
    text = "sistem informasi akademik"
    output = highlighted_pdf(service, path, [{
        "sentence": text, "page": 1,
        "page_start_position": start, "page_end_position": start + len(text),
    }], tmp_path, monkeypatch)
    rectangles = annotation_rects(output)
    assert len(rectangles) == 1
    with pymupdf.open(path) as document:
        page = document[0]
        last_word = next(word for word in page.get_text("words") if word[4] == "akademiknya.")
        last_word_start = last_word[0]
    with pymupdf.open(output) as document:
        page = document[0]
        annotation = next(page.annots())
        # Annotation.rect includes a decorative margin; vertices give the
        # actual text quadrilateral and must stop before the unmatched word.
        assert max(point[0] for point in annotation.vertices) < last_word_start
