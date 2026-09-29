from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from app.services.preprocessing_service import PreprocessingService

def calculate_similarity(text_a: str, text_b: str):
    preprocessor = PreprocessingService()

    print("--- 1. Preprocessing Text ---")
    clean_a = preprocessor.clean_text(text_a)
    clean_b = preprocessor.clean_text(text_b)
    print(f"Hasil Clean A : {clean_a}")
    print(f"Hasil Clean B : {clean_b}\n")

    print("--- 2. TF-IDF & Cosine Similarity ---")
    vectorizer = TfidfVectorizer()
    tfidf_matrix = vectorizer.fit_transform([clean_a, clean_b])

    # Hitung Cosine Similarity antara dokumen index 0 dan 1
    score = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:2])[0][0]
    percentage = score * 100

    print(f"Tingkat Kemiripan (Similarity): {percentage:.2f}%\n")
    return percentage

if __name__ == "__main__":
    # Contoh sampel dokumen pengujian sederhana
    teks_mahasiswa = "Sistem deteksi kemiripan dokumen ini dirancang untuk memeriksa karya tulis skripsi mahasiswa secara internal."
    teks_referensi = "Aplikasi pembanding skripsi mahasiswa ini dibuat untuk menghitung tingkat kemiripan dokumen teks berbasis web."

    calculate_similarity(teks_mahasiswa, teks_referensi)