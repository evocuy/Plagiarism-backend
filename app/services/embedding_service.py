import json
import os
from typing import Any, Dict, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class EmbeddingService:
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout_seconds: int = 60,
    ):
        self.base_url = (base_url or os.getenv("EMBED_URL") or "").rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("EMBED_API_KEY", "")
        self.model = model or os.getenv("EMBED_MODEL", "")
        self.timeout_seconds = timeout_seconds

    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def embed_texts(self, texts: Sequence[str]) -> List[List[float]]:
        clean_texts = [(text or "").strip() for text in texts]
        if not clean_texts:
            return []
        if not self.is_configured():
            raise RuntimeError("Embedding config belum lengkap. Pastikan EMBED_URL, EMBED_API_KEY, dan EMBED_MODEL tersedia.")

        response = self._post_json(
            url=f"{self.base_url}/embeddings",
            payload={
                "model": self.model,
                "input": clean_texts,
            },
        )
        data = response.get("data") or []
        embeddings = [item.get("embedding") for item in data]

        if len(embeddings) != len(clean_texts):
            raise RuntimeError(
                f"Jumlah embedding tidak sesuai. Diterima {len(embeddings)} dari {len(clean_texts)} input."
            )

        return [
            [float(value) for value in embedding]
            for embedding in embeddings
        ]

    def _post_json(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        request = Request(
            url=url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "plagiarism-checker-local/1.0",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Embedding API error {exc.code}: {error_body}") from exc
        except URLError as exc:
            raise RuntimeError(f"Gagal menghubungi Embedding API: {exc.reason}") from exc
