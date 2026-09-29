from pathlib import Path
from app.services.pdf_service import PDFService
from app.services.preprocessing_service import PreprocessingService
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

def run_pdf_similarity_test(pdf_path_a: str, pdf_path_b: str):
    pdf_service = PDFService()
    preprocessor = PreprocessingService()

    print("--- 1. Extracting PDF Text ---")
    data_a = pdf_service.extract_text_from_pdf(pdf_path_a)
    data_b = pdf_service.extract_text_from_pdf(pdf_path_b)
    
    print(f"PDF A: {data_a['total_pages']} halaman berhasil diekstrak.")
    print(f"PDF B: {data_b['total_pages']} halaman berhasil diekstrak.\n")

    print("--- 2. Preprocessing Text ---")
    clean_a = preprocessor.clean_text(data_a['full_text'])
    clean_b = preprocessor.clean_text(data_b['full_text'])

    print("--- 3. TF-IDF & Cosine Similarity ---")
    vectorizer = TfidfVectorizer()
    tfidf_matrix = vectorizer.fit_transform([clean_a, clean_b])

    score = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:2])[0][0]
    percentage = score * 100

    print(f"Tingkat Kemiripan Dokumen PDF: {percentage:.2f}%\n")
    return percentage

if __name__ == "__main__":
    # Mengambil path absolut direktori 'backend'
    BASE_DIR = Path(__file__).resolve().parent

    # Jika folder 'documents' sejajar dengan folder 'backend' (di root project):
    DOCS_DIR = BASE_DIR.parent / "documents"

    # Catatan: Jika folder 'documents' ada DI DALAM folder 'backend', pakai baris berikut:
    # DOCS_DIR = BASE_DIR / "documents"

    pdf_1 = str(DOCS_DIR / "SKRIPSI_FINAL.pdf")
    pdf_2 = str(DOCS_DIR / "Seminar_Proposal_I_Nyoman_Bagus_Aditya_Adnyana_2301010347_REVISI.pdf")

    run_pdf_similarity_test(pdf_1, pdf_2)