import re
from Sastrawi.StopWordRemover.StopWordRemoverFactory import StopWordRemoverFactory
from Sastrawi.Stemmer.StemmerFactory import StemmerFactory

class PreprocessingService:
    def __init__(self):
        self.stopword_remover = StopWordRemoverFactory().create_stop_word_remover()
        self.stemmer = StemmerFactory().create_stemmer()

    def clean_text(self, text: str, use_stemming: bool = False) -> str:
        # 1. Lowercase
        text = text.lower()
        # 2. Hapus angka & karakter non-alfabet
        text = re.sub(r'[^a-z\s]', ' ', text)
        # 3. Normalisasi spasi
        text = re.sub(r'\s+', ' ', text).strip()
        # 4. Hapus stopword Bahasa Indonesia
        text = self.stopword_remover.remove(text)

        # 5. Stemming (hanya dijalankan jika use_stemming=True)
        if use_stemming:
            words = text.split()
            unique_words = set(words)
            stem_cache = {word: self.stemmer.stem(word) for word in unique_words}
            clean_words = [stem_cache[word] for word in words]
            return " ".join(clean_words)

        return text