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

    def detect_skripsi_content_pages(self, doc: pymupdf.Document) -> tuple[int, int]:
        total_pages = len(doc)
        if total_pages == 0:
            return (0, 0)

        start_page = 0
        end_page = total_pages - 1

        bab1_pattern = re.compile(r'\bBAB\s+(?:1|I|SATU)\b', re.IGNORECASE)
        found_start = False

        for page_idx in range(total_pages):
            page_text = doc[page_idx].get_text()
            if not page_text or not page_text.strip():
                continue

            if self._is_table_of_contents_page(page_text):
                continue

            if bab1_pattern.search(page_text):
                start_page = page_idx
                found_start = True
                break

        closing_pattern = re.compile(
            r'^\s*(?:DAFTAR\s+PUSTAKA|DAFTAR\s+REFERENSI|DAFTAR\s+LITERATUR|BIBLIOGRAPHY|LAMPIRAN)\b',
            re.IGNORECASE | re.MULTILINE
        )

        search_end_start = start_page if found_start else 0
        for page_idx in range(search_end_start, total_pages):
            page_text = doc[page_idx].get_text()
            if not page_text:
                continue

            if self._is_table_of_contents_page(page_text):
                continue

            if closing_pattern.search(page_text):
                if page_idx > start_page:
                    end_page = page_idx - 1
                else:
                    end_page = page_idx
                break

        return (start_page, end_page)

    def extract_text_from_pdf(self, file_path: str, filter_bab: bool = True) -> Dict[str, Any]:
        resolved_path = self._resolve_path(file_path)
        if not resolved_path.exists():
            raise FileNotFoundError(f'File PDF tidak ditemukan: {resolved_path}')

        doc = pymupdf.open(str(resolved_path))
        total_pages = len(doc)

        start_page = 0
        end_page = max(0, total_pages - 1)

        if filter_bab and total_pages > 1:
            start_page, end_page = self.detect_skripsi_content_pages(doc)

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
            'pages': pages_data
        }

    def extract_sentences_with_pages(self, file_path: str, filter_bab: bool = True):
        resolved_path = self._resolve_path(file_path)
        if not resolved_path.exists():
            raise FileNotFoundError(f'File PDF tidak ditemukan: {resolved_path}')

        doc = pymupdf.open(str(resolved_path))
        total_pages = len(doc)
        start_page = 0
        end_page = max(0, total_pages - 1)

        if filter_bab and total_pages > 1:
            start_page, end_page = self.detect_skripsi_content_pages(doc)

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
