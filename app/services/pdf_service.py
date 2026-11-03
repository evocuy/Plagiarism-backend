import re
from pathlib import Path
from typing import Dict, Any, List, Optional
import pymupdf

from app.services.pdf_limits import PDFLimitExceeded, PDFProcessingLimits

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / 'uploads'
PDF_EXTRACTION_VERSION = 2

class PDFService:
    # Only whole structural lines are ignored. These words inside ordinary
    # prose remain comparison material, including the body of BAB I.
    _COMMON_HEADINGS = frozenset({
        "pendahuluan", "latar belakang", "latar belakang masalah",
        "rumusan masalah", "perumusan masalah", "batasan masalah",
        "tujuan penelitian", "manfaat penelitian", "sistematika penulisan",
        "tinjauan pustaka", "landasan teori", "kajian teori", "kajian pustaka",
        "penelitian terdahulu", "metodologi penelitian", "metode penelitian",
        "metode pengumpulan data", "teknik pengumpulan data",
        "analisis kebutuhan", "hasil dan pembahasan", "hasil penelitian",
        "pembahasan", "penutup", "kesimpulan", "simpulan", "saran",
        "kesimpulan dan saran", "simpulan dan saran",
    })

    def __init__(self, limits: Optional[PDFProcessingLimits] = None):
        self.limits = limits or PDFProcessingLimits.from_environment()

    @classmethod
    def _is_structural_heading(cls, line: str) -> bool:
        normalized = " ".join(line.split()).casefold()
        if normalized.rstrip(":") in cls._COMMON_HEADINGS:
            return True
        chapter = re.fullmatch(r"bab\s+(?:[ivx]+|\d+)[:.]?(?:\s*[-:]?\s+(.+))?", normalized)
        if chapter:
            title = chapter.group(1)
            return not title or title.rstrip(":") in cls._COMMON_HEADINGS
        # A short numbered title without sentence-ending punctuation is the
        # conventional subsection format. Do not strip numbered body sentences.
        subsection = re.fullmatch(r"\d+(?:\.\d+)+\.?\s+(.+)", normalized)
        if subsection:
            title = subsection.group(1)
            return len(title.split()) <= 12 and len(title) <= 120 and not re.search(r"[.!?;]$", title)
        return False

    @classmethod
    def _body_layout(cls, lines: list[dict]) -> tuple[str, list[dict]]:
        """Normalize body text once, optionally retaining word rectangles.

        Extraction and rendering share this representation so a stored page
        offset always identifies the same occurrence. Removed headings create
        a boundary instead of joining unrelated prose across sections.
        """
        parts = []
        positioned_words = []
        position = 0
        boundary = False
        for line_id, line in enumerate(lines):
            words = line["words"]
            line_text = " ".join(word["text"] for word in words)
            if not line_text or cls._is_structural_heading(line_text):
                boundary = True
                continue
            if parts:
                separator = "\n\n" if boundary else " "
                parts.append(separator)
                position += len(separator)
            boundary = False
            for index, word in enumerate(words):
                if index:
                    parts.append(" ")
                    position += 1
                start = position
                parts.append(word["text"])
                position += len(word["text"])
                positioned_words.append({**word, "start": start, "end": position, "line": line_id})
        return "".join(parts), positioned_words

    @classmethod
    def _body_text(cls, raw_text: str) -> str:
        lines = [
            {"words": [{"text": word} for word in line.split()]}
            for line in raw_text.splitlines()
        ]
        return cls._body_layout(lines)[0]

    @classmethod
    def _page_body_layout(cls, page) -> tuple[str, list[dict]]:
        lines = {}
        for word in page.get_text("words", sort=False):
            key = (word[5], word[6])
            lines.setdefault(key, {"words": []})["words"].append({
                "text": word[4], "rect": pymupdf.Rect(word[:4]),
            })
        return cls._body_layout(list(lines.values()))

    def _read_page_texts(self, doc: pymupdf.Document) -> List[str]:
        # Inspect page count before parsing any page, then extract each page
        # only once. Chapter detection reuses this bounded text cache.
        if len(doc) > self.limits.max_pages:
            raise PDFLimitExceeded(f"PDF melebihi batas {self.limits.max_pages} halaman.")
        if len(doc) == 0:
            raise ValueError("PDF tidak memuat halaman.")
        texts = []
        text_chars = 0
        for index in range(len(doc)):
            page_text = doc[index].get_text()
            text_chars += len(page_text)
            if text_chars > self.limits.max_text_chars:
                raise PDFLimitExceeded("Teks PDF melebihi batas pemrosesan dokumen.")
            texts.append(page_text)
        return texts

    def _resolve_path(self, file_path: str) -> Path:
        p = Path(file_path)
        if p.exists() and p.is_file():
            return p
        filename = p.name
        fallback = UPLOAD_DIR / filename
        if fallback.exists() and fallback.is_file():
            return fallback
        return p

    def _is_table_of_contents_page(self, text: str) -> bool:
        text_upper = text.upper()
        if 'DAFTAR ISI' in text_upper:
            return True
        dot_patterns = len(re.findall(r'\.{4,}\s*\d+', text))
        if dot_patterns >= 3:
            return True
        return False

    def _is_chapter_heading(self, page_text: str, chapter_roman: str, chapter_arabic: str) -> bool:
        """
        Mendeteksi apakah halaman ini memuat permulaan resmi bab terkait.
        Abaikan jika halaman tersebut adalah daftar isi atau bagian sistematika penulisan (di mana banyak bab disebut sekaligus).
        """
        if self._is_table_of_contents_page(page_text):
            return False

        lines = [line.strip() for line in page_text.split('\n') if line.strip()]
        # Jika di halaman ini muncul lebih dari 1 penyebutan BAB (misalnya pada bagian sistematika penulisan 1.6 / 1.5),
        # maka ini BUKAN halaman awal bab sesungguhnya.
        bab_lines = [l for l in lines if re.match(r'^BAB\s+[IVX0-9]+', l, re.IGNORECASE)]
        if len(bab_lines) > 1:
            return False

        chapter_pattern = re.compile(
            rf'^BAB\s+(?:{re.escape(chapter_roman)}|{re.escape(chapter_arabic)})(?=\s|[.:)\-]|$)',
            re.IGNORECASE,
        )
        for line in lines:
            if chapter_pattern.match(line.strip()):
                return True
        return False

    def detect_content_pages(self, doc: pymupdf.Document, document_type: str = "skripsi", page_texts: Optional[List[str]] = None) -> tuple[int, int]:
        """
        Mendeteksi rentang halaman yang diperiksa:
        - Skripsi: BAB 1 sampai BAB 5 / DAFTAR PUSTAKA.
        - Proposal / Sempro: BAB 1 sampai akhir BAB 3 (berhenti saat BAB 4 atau DAFTAR PUSTAKA ditemukan).
        """
        total_pages = len(doc)
        if total_pages == 0:
            return (0, 0)

        page_texts = page_texts if page_texts is not None else self._read_page_texts(doc)

        is_sempro = any(k in (document_type or "").lower() for k in ["sempro", "proposal"])
        start_page = 0
        end_page = total_pages - 1

        # Cari halaman BAB 1 yang valid
        for page_idx in range(total_pages):
            page_text = page_texts[page_idx]
            if not page_text or not page_text.strip():
                continue

            if self._is_chapter_heading(page_text, 'I', '1'):
                start_page = page_idx
                break

        # Pola penutup: Daftar Pustaka / Lampiran
        closing_pattern = re.compile(
            r'^\s*(?:DAFTAR\s+PUSTAKA|DAFTAR\s+REFERENSI|DAFTAR\s+LITERATUR|BIBLIOGRAPHY|LAMPIRAN)\b',
            re.IGNORECASE | re.MULTILINE
        )

        for page_idx in range(start_page, total_pages):
            page_text = page_texts[page_idx]
            if not page_text:
                continue

            if self._is_table_of_contents_page(page_text):
                continue

            # Jika proposal/sempro, berhenti ketika memasuki BAB 4
            if is_sempro and self._is_chapter_heading(page_text, 'IV', '4'):
                if page_idx > start_page:
                    end_page = page_idx - 1
                else:
                    end_page = page_idx
                break

            # Berhenti jika mencapai Daftar Pustaka / Lampiran (dan bukan di halaman rangkuman sistematika)
            lines = [l.strip() for l in page_text.split('\n') if l.strip()]
            bab_count = sum(1 for l in lines if re.match(r'^BAB\s+[IVX0-9]+', l, re.IGNORECASE))
            if bab_count <= 1 and closing_pattern.search(page_text):
                if page_idx > start_page:
                    end_page = page_idx - 1
                else:
                    end_page = page_idx
                break

        return (start_page, end_page)

    def detect_chapters_in_doc(self, doc: pymupdf.Document, document_type: str = "skripsi", page_texts: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Mendeteksi BAB apa saja yang ditemukan di dokumen dan memvalidasi kesesuaian jumlah BAB:
        - Skripsi: standar 5 BAB. Berikan warning jika BAB kurang dari 5.
        - Proposal / Sempro: standar 3 BAB. Berikan warning jika BAB kurang dari 3 atau lebih dari 3.
        """
        total = len(doc)
        page_texts = page_texts if page_texts is not None else self._read_page_texts(doc)
        roman_numerals = ['I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX', 'X']
        chapter_defs = [
            (number, roman, str(number))
            for number, roman in enumerate(roman_numerals, start=1)
        ]
        found_chapters = []
        for num, roman, arabic in chapter_defs:
            for page_idx in range(total):
                txt = page_texts[page_idx]
                if self._is_chapter_heading(txt, roman, arabic):
                    found_chapters.append({
                        "chapter": num,
                        "roman": roman,
                        "page": page_idx + 1
                    })
                    break

        total_detected = len(found_chapters)
        is_sempro = any(k in (document_type or "").lower() for k in ["sempro", "proposal"])
        has_warning = False
        warning_type = None
        warning_message = None
        detected_numbers = [chapter["chapter"] for chapter in found_chapters]

        if is_sempro:
            expected_chapters = [1, 2, 3]
            missing_chapters = [chapter for chapter in expected_chapters if chapter not in detected_numbers]
            unexpected_chapters = [
                chapter for chapter in detected_numbers if chapter not in expected_chapters
            ]
            if unexpected_chapters:
                has_warning = True
                warning_type = "PROPOSAL_CHAPTER_OVERFLOW"
                warning_message = (
                    "Perhatian: Anda memilih tipe 'Proposal', tetapi sistem mendeteksi "
                    f"BAB tambahan ({', '.join(f'BAB {chapter}' for chapter in unexpected_chapters)}). "
                    "Proposal hanya boleh memuat BAB 1 sampai BAB 3."
                )
            elif missing_chapters:
                has_warning = True
                warning_type = "PROPOSAL_CHAPTER_INCOMPLETE"
                warning_message = (
                    "Perhatian: Anda memilih tipe 'Proposal' (Seminar Proposal), namun "
                    f"{', '.join(f'BAB {chapter}' for chapter in missing_chapters)} belum terdeteksi."
                )
        else:
            # Mode Skripsi
            expected_chapters = [1, 2, 3, 4, 5]
            missing_chapters = [chapter for chapter in expected_chapters if chapter not in detected_numbers]
            if missing_chapters:
                has_warning = True
                warning_type = "SKRIPSI_CHAPTER_INCOMPLETE"
                warning_message = (
                    f"Perhatian: Anda memilih tipe 'Skripsi', namun dokumen hanya memuat "
                    f"{total_detected} BAB dan belum terdeteksi {', '.join(f'BAB {chapter}' for chapter in missing_chapters)}. "
                    f"Apakah Anda keliru mengunggah dokumen Proposal?"
                )

        return {
            "total_chapters_detected": total_detected,
            "detected_chapters": found_chapters,
            "has_warning": has_warning,
            "warning_type": warning_type,
            "warning_message": warning_message,
        }

    def _chapter_for_page(self, page_number: int, detected_chapters: List[Dict[str, Any]]) -> Optional[str]:
        current_chapter = None
        sorted_chapters = sorted(
            detected_chapters or [],
            key=lambda item: item.get("page") or 0,
        )

        for item in sorted_chapters:
            chapter_page = item.get("page")
            chapter_number = item.get("chapter")
            if chapter_page and chapter_number and page_number >= chapter_page:
                current_chapter = f"bab_{chapter_number}"
            elif chapter_page and page_number < chapter_page:
                break

        return current_chapter

    def detect_skripsi_content_pages(self, doc: pymupdf.Document) -> tuple[int, int]:
        """Backward-compatibility wrapper."""
        return self.detect_content_pages(doc, document_type="skripsi")

    def extract_text_from_pdf(self, file_path: str, filter_bab: bool = True, document_type: str = "skripsi") -> Dict[str, Any]:
        resolved_path = self._resolve_path(file_path)
        if not resolved_path.exists():
            raise FileNotFoundError(f'File PDF tidak ditemukan: {resolved_path}')

        with pymupdf.open(str(resolved_path)) as doc:
            total_pages = len(doc)
            page_texts = self._read_page_texts(doc)
            start_page = 0
            end_page = total_pages - 1
            if filter_bab and total_pages > 1:
                start_page, end_page = self.detect_content_pages(
                    doc, document_type=document_type, page_texts=page_texts,
                )
            chapter_validation = self.detect_chapters_in_doc(
                doc, document_type=document_type, page_texts=page_texts,
            )
            pages_data = []
            body_chars = 0
            for page_num in range(start_page, end_page + 1):
                raw_text = page_texts[page_num]
                # Use the exact same word/line representation as the renderer.
                # Plain-text extraction can insert spaces that word extraction
                # does not (font spans, justification, ligatures), making its
                # offsets unsuitable for locating annotations.
                body_text = self._page_body_layout(doc[page_num])[0] if filter_bab else None
                body_chars += len(body_text or "")
                if body_chars > self.limits.max_text_chars:
                    raise PDFLimitExceeded("Teks PDF melebihi batas pemrosesan dokumen.")
                pages_data.append({
                    'page': page_num + 1,
                    'text': body_text if filter_bab else raw_text,
                    **({'body_text': body_text} if body_text is not None else {}),
                })

        return {
            'full_text': '\n'.join(page['text'] for page in pages_data),
            'extraction_version': PDF_EXTRACTION_VERSION,
            'total_pages': total_pages,
            'checked_pages_range': {
                'start_page': start_page + 1,
                'end_page': end_page + 1,
                'total_checked_pages': len(pages_data),
            },
            'chapter_validation': chapter_validation,
            'pages': pages_data
        }

    def extract_sentences_with_pages(self, file_path: str, filter_bab: bool = True, document_type: str = "skripsi"):
        extracted = self.extract_text_from_pdf(file_path, filter_bab=filter_bab, document_type=document_type)
        chapter_validation = extracted["chapter_validation"]
        detected_chapters = chapter_validation.get("detected_chapters", [])
        sentences_info = []

        for page in extracted["pages"]:
            page_content = page.get("body_text", self._body_text(page["text"]))
            if not page_content:
                continue

            # Preserve offsets in the normalized body, and never produce a
            # sentence that spans a removed heading.
            start = 0
            separators = list(re.finditer(r'(?<=[.!?])\s+|\n\n', page_content))
            ranges = []
            for separator in separators:
                ranges.append((start, separator.start()))
                start = separator.end()
            ranges.append((start, len(page_content)))
            for start, end in ranges:
                fragment = page_content[start:end]
                s_clean = fragment.strip()
                if len(s_clean.split()) >= 4 and len(s_clean) >= 20:
                    if len(sentences_info) >= self.limits.max_chunks:
                        raise PDFLimitExceeded("Jumlah potongan teks PDF melebihi batas pemrosesan.")
                    page_number = page["page"]
                    sentences_info.append({
                        'page': page_number,
                        'chapter': self._chapter_for_page(page_number, detected_chapters),
                        'sentence': s_clean,
                        'page_start_position': start + len(fragment) - len(fragment.lstrip()),
                        'page_end_position': end - len(fragment) + len(fragment.rstrip()),
                    })

        return sentences_info

    def generate_highlighted_pdf(self, source_pdf_path: str, plagiarized_sentences: list, output_filename: str) -> str:
        resolved_path = self._resolve_path(source_pdf_path)
        if not resolved_path.exists():
            raise FileNotFoundError(f'File PDF tidak ditemukan: {resolved_path}')

        highlight_dir = UPLOAD_DIR / 'highlighted'
        highlight_dir.mkdir(parents=True, exist_ok=True)
        output_path = highlight_dir / output_filename

        with pymupdf.open(str(resolved_path)) as doc:
            self._read_page_texts(doc)
            layouts = {}
            body_chars = 0
            highlighted_ranges = set()
            for item in plagiarized_sentences:
                sentence = item.get('sentence', '').strip()
                page_target = item.get('page')
                if (not sentence or isinstance(page_target, bool)
                        or not isinstance(page_target, int) or not 1 <= page_target <= len(doc)):
                    continue
                page = doc[page_target - 1]
                if page_target not in layouts:
                    layouts[page_target] = self._page_body_layout(page)
                    body_chars += len(layouts[page_target][0])
                    if body_chars > self.limits.max_text_chars:
                        raise PDFLimitExceeded("Teks PDF melebihi batas pemrosesan dokumen.")
                body_text, words = layouts[page_target]
                start = item.get('page_start_position')
                end = item.get('page_end_position')
                if start is None and end is None:
                    # Compatibility for old callers: only one exact occurrence
                    # in body text is safe. Never search headings or other pages.
                    occurrences = list(re.finditer(re.escape(sentence), body_text))
                    if len(occurrences) != 1:
                        continue
                    start, end = occurrences[0].span()
                if (isinstance(start, bool) or isinstance(end, bool)
                        or not isinstance(start, int) or not isinstance(end, int)
                        or not 0 <= start < end <= len(body_text)
                        or body_text[start:end] != sentence
                        or '\n\n' in body_text[start:end]):
                    continue
                key = (page_target, start, end)
                if key in highlighted_ranges:
                    continue
                highlighted_ranges.add(key)
                rectangles_by_line = {}
                for word in words:
                    if word['start'] < end and start < word['end']:
                        # Word rectangles include punctuation (e.g. akademik.),
                        # while lexical match ranges end at the final letter.
                        # Include that rectangle only when all omitted chars
                        # are punctuation; never highlight unmatched letters.
                        prefix = word['text'][:max(0, start - word['start'])]
                        suffix = word['text'][max(0, end - word['start']):]
                        if re.search(r'\w', prefix + suffix):
                            continue
                        line = word['line']
                        if line in rectangles_by_line:
                            rectangles_by_line[line] |= word['rect']
                        else:
                            rectangles_by_line[line] = pymupdf.Rect(word['rect'])
                for rect in rectangles_by_line.values():
                    self._add_highlight(page, rect)
            doc.save(str(output_path))

        return str(output_path)

    def _add_highlight(self, page, rect) -> None:
        try:
            annot = page.add_highlight_annot(rect)
            annot.set_colors(stroke=[1.0, 1.0, 0.0])
            annot.update()
        except Exception:
            # Some PDFs produce invalid quad geometry for specific search rects.
            # Keep generating the report instead of failing the whole check.
            return
