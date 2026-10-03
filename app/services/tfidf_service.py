from typing import Dict, List, Sequence

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


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
        queries = [(text or "").strip() for text in query_texts]
        corpus = [(text or "").strip() for text in corpus_texts]

        if not queries or not corpus:
            return []

        try:
            vectorizer = TfidfVectorizer()
            corpus_matrix = vectorizer.fit_transform(corpus)
            query_matrix = vectorizer.transform(queries)
            similarity_matrix = cosine_similarity(query_matrix, corpus_matrix)
        except ValueError:
            return [{"index": -1, "score": 0.0} for _ in queries]

        matches = []
        for row in similarity_matrix:
            if len(row) == 0:
                matches.append({"index": -1, "score": 0.0})
                continue

            best_index = int(row.argmax())
            matches.append({
                "index": best_index,
                "score": float(row[best_index]),
            })

        return matches
