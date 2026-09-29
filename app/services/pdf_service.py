import os
from pathlib import Path
from typing import Dict, Any
import pymupdf

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"

class PDFService:
    def _resolve_path(self, file_path: str) -> Path:
        """Mendukung path lintas platform (Docker Linux vs Windows)"""
        p = Path(file_path)
        if p.exists() and p.is_file():
            return p
        # Cari berdasarkan nama file di folder uploads
        filename = p.name
        fallback = UPLOAD_DIR / filename
        if fallback.exists() and fallback.is_file():
            return fallback
        return p

    def extract_text_from_pdf(self, file_path: str) -> Dict[str, Any]:
        resolved_path = self._resolve_path(file_path)
        if not resolved_path.exists():
            raise FileNotFoundError(f"File PDF tidak ditemukan: {resolved_path}")

        doc = pymupdf.open(str(resolved_path))
        full_text = []
        pages_data = []

        total_pages = len(doc)

        for page_num in range(total_pages):
            page = doc[page_num]
            text = page.get_text()
            full_text.append(text)
            pages_data.append({
                "page": page_num + 1,
                "text": text
            })

        doc.close()

        return {
            "full_text": "\n".join(full_text),
            "total_pages": total_pages,
            "pages": pages_data
        }

    def extract_sentences_with_pages(self, file_path: str):
        """
        Mengekstrak kalimat-kalimat dari PDF beserta nomor halamannya.
        Berguna untuk pengecekan kesamaan tingkat kalimat.
        """
        import re
        resolved_path = self._resolve_path(file_path)
        if not resolved_path.exists():
            raise FileNotFoundError(f"File PDF tidak ditemukan: {resolved_path}")

        doc = pymupdf.open(str(resolved_path))
        sentences_info = []

        for page_idx, page in enumerate(doc):
            text = page.get_text()
            if not text:
                continue

            # Bersihkan spasi/line break berlebih
            raw_lines = [line.strip() for line in text.split("\n") if line.strip()]
            page_content = " ".join(raw_lines)

            # Split berdasarkan tanda titik, tanda tanya, atau tanda seru
            splits = re.split(r'(?<=[.!?])\s+', page_content)
            for s in splits:
                s_clean = s.strip()
                # Hanya simpan kalimat yang memiliki minimal 4 kata / 20 karakter
                if len(s_clean.split()) >= 4 and len(s_clean) >= 20:
                    sentences_info.append({
                        "page": page_idx + 1,
                        "sentence": s_clean
                    })

        doc.close()
        return sentences_info

    def generate_highlighted_pdf(self, source_pdf_path: str, plagiarized_sentences: list, output_filename: str) -> str:
        """
        Membuka file PDF asli, mencari kalimat yang terdeteksi plagiat,
        memberikan highlight kuning stabilo, dan menyimpannya ke folder uploads/highlighted.
        """
        import re
        resolved_path = self._resolve_path(source_pdf_path)
        if not resolved_path.exists():
            raise FileNotFoundError(f"File PDF tidak ditemukan: {resolved_path}")

        highlight_dir = UPLOAD_DIR / "highlighted"
        highlight_dir.mkdir(parents=True, exist_ok=True)
        output_path = highlight_dir / output_filename

        doc = pymupdf.open(str(resolved_path))

        for item in plagiarized_sentences:
            sentence = item.get("sentence", "").strip()
            page_target = item.get("page")
            if not sentence:
                continue

            # Tentukan halaman yang akan dicari
            pages_to_search = []
            if page_target and 1 <= page_target <= len(doc):
                pages_to_search.append(doc[page_target - 1])
            else:
                pages_to_search = list(doc)

            # Cari dan highlight
            for page in pages_to_search:
                rects = page.search_for(sentence)
                
                # Jika pencarian kalimat lengkap tidak ketemu karena line-breaks,
                # cari per potongan frasa kata
                if not rects:
                    words = sentence.split()
                    chunk_size = 5
                    for i in range(0, len(words), chunk_size):
                        chunk = " ".join(words[i:i + chunk_size])
                        if len(chunk) >= 15:
                            sub_rects = page.search_for(chunk)
                            for r in sub_rects:
                                annot = page.add_highlight_annot(r)
                                annot.set_colors(stroke=[1.0, 1.0, 0.0])  # Kuning stabilo
                                annot.update()
                else:
                    for r in rects:
                        annot = page.add_highlight_annot(r)
                        annot.set_colors(stroke=[1.0, 1.0, 0.0])  # Kuning stabilo
                        annot.update()

        doc.save(str(output_path))
        doc.close()

        return str(output_path)