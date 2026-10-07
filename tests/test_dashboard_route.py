from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.models.schemas import Document, Dosen, Mahasiswa, PlagiarismCheck, User
from app.routes.dashboard import get_dashboard_summary


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session_local = sessionmaker(bind=engine)
    return session_local()


def _seed_dashboard_data(db):
    admin = User(identifier="admin", password="secret", role="admin")
    dosen_user = User(identifier="dosen", password="secret", role="dosen")
    mahasiswa_bimbingan = User(identifier="mhs-bimbingan", password="secret", role="mahasiswa")
    mahasiswa_lain = User(identifier="mhs-lain", password="secret", role="mahasiswa")
    db.add_all([admin, dosen_user, mahasiswa_bimbingan, mahasiswa_lain])
    db.flush()

    db.add(Dosen(id=dosen_user.id, nama_lengkap="Dosen Pembimbing"))
    db.add_all([
        Mahasiswa(
            id=mahasiswa_bimbingan.id,
            nama_lengkap="Mahasiswa Bimbingan",
            program_studi="Informatika",
            fakultas="Teknik",
            dosen_pembimbing_id=dosen_user.id,
        ),
        Mahasiswa(
            id=mahasiswa_lain.id,
            nama_lengkap="Mahasiswa Lain",
            program_studi="Informatika",
            fakultas="Teknik",
        ),
    ])
    db.flush()

    document_bimbingan = Document(
        user_id=mahasiswa_bimbingan.id,
        title="Bimbingan.pdf",
        document_type="proposal",
        file_path="bimbingan.pdf",
    )
    document_lain = Document(
        user_id=mahasiswa_lain.id,
        title="Lain.pdf",
        document_type="skripsi",
        file_path="lain.pdf",
    )
    db.add_all([document_bimbingan, document_lain])
    db.flush()

    db.add_all([
        PlagiarismCheck(
            document_id=document_bimbingan.id,
            user_id=mahasiswa_bimbingan.id,
            overall_similarity=0.31,
            approval_status="revisi",
            status="completed",
        ),
        PlagiarismCheck(
            document_id=document_bimbingan.id,
            user_id=mahasiswa_bimbingan.id,
            overall_similarity=0.18,
            approval_status="disetujui",
            status="completed",
        ),
        PlagiarismCheck(
            document_id=document_lain.id,
            user_id=mahasiswa_lain.id,
            overall_similarity=0.42,
            approval_status="belum disetujui",
            status="completed",
        ),
    ])
    db.commit()
    return admin, dosen_user, mahasiswa_bimbingan


def test_dashboard_summary_scopes_counts_by_role():
    db = _make_session()
    admin, dosen_user, mahasiswa = _seed_dashboard_data(db)

    mahasiswa_summary = get_dashboard_summary(current_user=mahasiswa, db=db)
    dosen_summary = get_dashboard_summary(current_user=dosen_user, db=db)
    admin_summary = get_dashboard_summary(current_user=admin, db=db)

    assert mahasiswa_summary["document_count"] == 1
    assert mahasiswa_summary["total_check_count"] == 2
    assert mahasiswa_summary["latest_check_count"] == 1
    assert mahasiswa_summary["approved_count"] == 1
    assert mahasiswa_summary["average_similarity"] == 0.18

    assert dosen_summary["supervised_student_count"] == 1
    assert dosen_summary["document_count"] == 1
    assert dosen_summary["total_check_count"] == 2
    assert dosen_summary["approved_count"] == 1

    assert admin_summary["document_count"] == 2
    assert admin_summary["total_check_count"] == 3
    assert admin_summary["latest_check_count"] == 2
    assert admin_summary["approved_count"] == 1
    assert admin_summary["pending_review_count"] == 1
