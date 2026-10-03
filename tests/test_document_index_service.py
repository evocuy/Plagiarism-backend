from app.services.document_index_service import DocumentIndexService


class FakeDocument:
    id = 7
    title = "sample.pdf"


class FakeTextService:
    def __init__(self):
        self.refresh_cache = None

    def get_or_extract_text(self, document, db, refresh_cache=False):
        self.refresh_cache = refresh_cache
        return {
            "chapter_validation": {
                "has_warning": False,
            }
        }


class FakeChunkService:
    def __init__(self):
        self.refresh_cache = None

    def get_or_build_sentence_chunks(self, document, db, refresh_cache=False):
        self.refresh_cache = refresh_cache
        return [
            {
                "chapter": "bab_1",
                "sentence": "Kalimat pertama.",
            },
            {
                "chapter": "bab_2",
                "sentence": "Kalimat kedua.",
            },
        ]


def test_reindex_document_refreshes_text_and_chunks():
    text_service = FakeTextService()
    chunk_service = FakeChunkService()
    service = DocumentIndexService(
        document_text_service=text_service,
        document_chunk_service=chunk_service,
    )

    result = service.reindex_document(FakeDocument(), db=None)

    assert text_service.refresh_cache is True
    assert chunk_service.refresh_cache is True
    assert result["document_id"] == 7
    assert result["chunk_count"] == 2
    assert result["chapters"] == ["bab_1", "bab_2"]
    assert result["text_cache_status"] == "completed"
    assert result["chunk_cache_status"] == "completed"
