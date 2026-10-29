from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.models.schemas import Document, PlagiarismCheck, User
from app.workers.retire_demo_accounts import retire_demo_accounts


def test_retirement_keeps_admin_and_real_users_and_preserves_check_history():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        admin = User(identifier="cihuy", password="unchanged-admin-hash", role="admin")
        sample = User(identifier="20210801142", password="old-sample-password", role="mahasiswa")
        real = User(identifier="actual-student", password="unchanged-real-hash", role="mahasiswa")
        db.add_all([admin, sample, real])
        db.flush()
        document = Document(user_id=sample.id, title="Retained.pdf", file_path="retained.pdf")
        db.add(document)
        db.flush()
        check = PlagiarismCheck(document_id=document.id, user_id=sample.id, overall_similarity=0.2)
        db.add(check)
        db.commit()

        preview = retire_demo_accounts(db)
        db.refresh(sample)
        assert preview["accounts_to_retire"] == 1
        assert sample.is_active is True
        assert sample.password == "old-sample-password"

        report = retire_demo_accounts(db, apply=True)
        db.refresh(sample)
        assert sample.is_active is False
        assert sample.session_version == 1
        assert sample.password != "old-sample-password"
        assert report["documents_retained"] == 1
        assert report["checks_retained"] == 1
        assert db.query(Document).count() == 1
        assert db.query(PlagiarismCheck).count() == 1
        assert admin.password == "unchanged-admin-hash"
        assert admin.is_active is True
        assert real.is_active is True
        assert retire_demo_accounts(db, apply=True)["accounts_to_retire"] == 0
        assert sample.session_version == 1
    engine.dispose()
