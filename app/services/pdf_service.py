import os
import re
from pathlib import Path
from typing import Dict, Any, List, Optional
import pymupdf

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / 'uploads'

class PDFService:
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

        for line in lines:
            line_clean = line.strip().upper()
            if line_clean == f'BAB {chapter_roman}' or line_clean == f'BAB {chapter_arabic}':
                return True
            if line_clean.startswith(f'BAB {chapter_roman} ') or line_clean.startswith(f'BAB {chapter_arabic} '):
                return True
        return False

    def detect_content_pages(self, doc: pymupdf.Document, document_type: str = "skripsi") -> tuple[int, int]:
        """
        Mendeteksi rentang halaman yang diperiksa:
        - Skripsi: BAB 1 sampai BAB 5 / DAFTAR PUSTAKA.
        - Proposal / Sempro: BAB 1 sampai akhir BAB 3 (berhenti saat BAB 4 atau DAFTAR PUSTAKA ditemukan).
        """
        total_pages = len(doc)
        if total_pages == 0:
            return (0, 0)

        is_sempro = any(k in (document_type or "").lower() for k in ["sempro", "proposal"])
        start_page = 0
        end_page = total_pages - 1

        # Cari halaman BAB 1 yang valid
        for page_idx in range(total_pages):
            page_text = doc[page_idx].get_text()
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
            page_text = doc[page_idx].get_text()
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

    def detect_chapters_in_doc(self, doc: pymupdf.Document, document_type: str = "skripsi") -> Dict[str, Any]:
        """
        Mendeteksi BAB apa saja yang ditemukan di dokumen dan memvalidasi kesesuaian jumlah BAB:
        - Skripsi: standar 5 BAB. Berikan warning jika BAB kurang dari 5.
        - Proposal / Sempro: standar 3 BAB. Berikan warning jika BAB kurang dari 3 atau lebih dari 3.
        """
        total = len(doc)
        chapter_defs = [
            (1, 'I', '1'),
            (2, 'II', '2'),
            (3, 'III', '3'),
            (4, 'IV', '4'),
            (5, 'V', '5'),
            (6, 'VI', '6'),
        ]
        found_chapters = []
        for num, roman, arabic in chapter_defs:
            for page_idx in range(total):
                txt = doc[page_idx].get_text()
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

        if is_sempro:
            if total_detected > 3:
                has_warning = True
                warning_type = "PROPOSAL_CHAPTER_OVERFLOW"
                warning_message = (
                    f"Perhatian: Anda mengunggah dokumen dengan tipe 'Proposal', namun sistem mendeteksi ada "
                    f"{total_detected} BAB (ditemukan hingga BAB {found_chapters[-1]['chapter']}). "
                    f"Apakah Anda keliru mengunggah draf Skripsi lengkap?"
                )
            elif total_detected < 3:
                has_warning = True
                warning_type = "PROPOSAL_CHAPTER_INCOMPLETE"
                warning_message = (
                    f"Perhatian: Anda memilih tipe 'Proposal' (Seminar Proposal), namun sistem hanya mendeteksi "
                    f"{total_detected} BAB dari standar 3 BAB."
                )
        else:
            # Mode Skripsi
            if total_detected < 5:
                has_warning = True
                warning_type = "SKRIPSI_CHAPTER_INCOMPLETE"
                warning_message = (
                    f"Perhatian: Anda memilih tipe 'Skripsi', namun dokumen hanya memuat "
                    f"{total_detected} BAB dari standar 5 BAB (ditemukan hanya hingga BAB {found_chapters[-1]['chapter'] if found_chapters else 0}). "
                    f"Apakah Anda keliru mengunggah dokumen Proposal?"
                )

        return {
            "total_chapters_detected": total_detected,
            "detected_chapters": found_chapters,
            "has_warning": has_warning,
            "warning_type": warning_type,
            "warning_message": warning_message,
        }

    def detect_skripsi_content_pages(self, doc: pymupdf.Document) -> tuple[int, int]:
        """Backward-compatibility wrapper."""
        return self.detect_content_pages(doc, document_type="skripsi")

    def extract_text_from_pdf(self, file_path: str, filter_bab: bool = True, document_type: str = "skripsi") -> Dict[str, Any]:
        resolved_path = self._resolve_path(file_path)
        if not resolved_path.exists():
            raise FileNotFoundError(f'File PDF tidak ditemukan: {resolved_path}')

        doc = pymupdf.open(str(resolved_path))
        total_pages = len(doc)

        start_page = 0
        end_page = max(0, total_pages - 1)

        if filter_bab and total_pages > 1:
            start_page, end_page = self.detect_content_pages(doc, document_type=document_type)

        chapter_validation = self.detect_chapters_in_doc(doc, document_type=document_type)

        full_text = []
        pages_data = []

        for page_num in range(start_page, end_page + 1):
            page = doc[page_num]
            text = page.get_text()
            full_text.append(text)
            pages_data.append({
                'page': page_num + 1,
                'text': text
            })

        doc.close()

        return {
            'full_text': '\n'.join(full_text),
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
        resolved_path = self._resolve_path(file_path)
        if not resolved_path.exists():
            raise FileNotFoundError(f'File PDF tidak ditemukan: {resolved_path}')

        doc = pymupdf.open(str(resolved_path))
        total_pages = len(doc)
        start_page = 0
        end_page = max(0, total_pages - 1)

        if filter_bab and total_pages > 1:
            start_page, end_page = self.detect_content_pages(doc, document_type=document_type)

        sentences_info = []

        for page_idx in range(start_page, end_page + 1):
            page = doc[page_idx]
            text = page.get_text()
            if not text:
                continue

            raw_lines = [line.strip() for line in text.split('\n') if line.strip()]
            page_content = ' '.join(raw_lines)

            splits = re.split(r'(?<=[.!?])\s+', page_content)
            for s in splits:
                s_clean = s.strip()
                if len(s_clean.split()) >= 4 and len(s_clean) >= 20:
                    sentences_info.append({
                        'page': page_idx + 1,
                        'sentence': s_clean
                    })

        doc.close()
        return sentences_info

    def generate_highlighted_pdf(self, source_pdf_path: str, plagiarized_sentences: list, output_filename: str) -> str:
        resolved_path = self._resolve_path(source_pdf_path)
        if not resolved_path.exists():
            raise FileNotFoundError(f'File PDF tidak ditemukan: {resolved_path}')

        highlight_dir = UPLOAD_DIR / 'highlighted'
        highlight_dir.mkdir(parents=True, exist_ok=True)
        output_path = highlight_dir / output_filename

        doc = pymupdf.open(str(resolved_path))

        for item in plagiarized_sentences:
            sentence = item.get('sentence', '').strip()
            page_target = item.get('page')
            if not sentence:
                continue

            pages_to_search = []
            if page_target and 1 <= page_target <= len(doc):
                pages_to_search.append(doc[page_target - 1])
            else:
                pages_to_search = list(doc)

            for page in pages_to_search:
                rects = page.search_for(sentence)
                if not rects:
                    words = sentence.split()
                    chunk_size = 5
                    for i in range(0, len(words), chunk_size):
                        chunk = ' '.join(words[i:i + chunk_size])
                        if len(chunk) >= 15:
                            sub_rects = page.search_for(chunk)
                            for r in sub_rects:
                                annot = page.add_highlight_annot(r)
                                annot.set_colors(stroke=[1.0, 1.0, 0.0])
                                annot.update()
                else:
                    for r in rects:
                        annot = page.add_highlight_annot(r)
                        annot.set_colors(stroke=[1.0, 1.0, 0.0])
                        annot.update()

        doc.save(str(output_path))
        doc.close()

        return str(output_path)
