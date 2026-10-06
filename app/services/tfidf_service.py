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

        try:
            vectorizer = TfidfVectorizer()
            corpus_matrix = vectorizer.fit_transform(corpus)
            query_matrix = vectorizer.transform(queries)
            similarity_matrix = cosine_similarity(query_matrix, corpus_matrix)
        except ValueError:
            return [[{"index": -1, "score": 0.0}] for _ in queries]

        matches = []
        limit = max(1, min(top_n, len(corpus)))
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
