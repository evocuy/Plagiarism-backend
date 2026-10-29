from typing import Dict, List, Sequence

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app.services.pdf_limits import PDFLimitExceeded, positive_int_setting


class TfidfService:
    def calculate_pair_similarity(self, text_a: str, text_b: str) -> float:
        text_a = (text_a or "").strip()
        text_b = (text_b or "").strip()

        if not text_a or not text_b:
            return 0.0

        try:
            vectorizer = TfidfVectorizer()
            tfidf_matrix = vectorizer.fit_transform([text_a, text_b])
            return float(cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:2])[0][0])
        except ValueError:
            return 0.0

    def find_best_matches(self, query_texts: Sequence[str], corpus_texts: Sequence[str]) -> List[Dict[str, float]]:
        top_matches = self.find_top_matches(query_texts, corpus_texts, top_n=1)
        return [
            matches[0] if matches else {"index": -1, "score": 0.0}
            for matches in top_matches
        ]

    def find_top_matches(
        self,
        query_texts: Sequence[str],
        corpus_texts: Sequence[str],
        top_n: int = 3,
    ) -> List[List[Dict[str, float]]]:
        queries = [(text or "").strip() for text in query_texts]
        corpus = [(text or "").strip() for text in corpus_texts]

        if not queries or not corpus:
            return []

        max_cells = positive_int_setting("TFIDF_SIMILARITY_BATCH_CELLS", 1_000_000)
        if len(corpus) > max_cells:
            raise PDFLimitExceeded("Jumlah potongan repositori melebihi batas perbandingan.")

        try:
            vectorizer = TfidfVectorizer()
            corpus_matrix = vectorizer.fit_transform(corpus)
            query_matrix = vectorizer.transform(queries)
        except ValueError:
            return [[{"index": -1, "score": 0.0}] for _ in queries]

        matches = []
        limit = max(1, min(top_n, len(corpus)))
        # Preserve the fitted corpus and exact cosine values while bounding
        # each dense query-by-corpus allocation (8 MB at the default limit).
        batch_size = max(1, min(128, max_cells // len(corpus)))
        for start in range(0, query_matrix.shape[0], batch_size):
            similarity_matrix = cosine_similarity(query_matrix[start:start + batch_size], corpus_matrix)
            for row in similarity_matrix:
                if len(row) == 0:
                    matches.append([{"index": -1, "score": 0.0}])
                    continue

                top_indices = row.argsort()[::-1][:limit]
                matches.append([
                    {
                        "index": int(index),
                        "score": float(row[index]),
                    }
                    for index in top_indices
                ])

        return matches
