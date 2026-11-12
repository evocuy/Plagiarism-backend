"""Keep the public profile contract while storing profiles directly on users."""
import pytest

from app.models.schemas import Document, PlagiarismCheck, User
from app.routes import auth
from test_auth_session import environment, request


@pytest.fixture
def context(environment, monkeypatch):
    monkeypatch.setenv("STUDENT_AUTH_PROVIDER", "local")
    app, db = environment
    admin = db.query(User).filter_by(identifier="admin").one()
    return app, db, f"Bearer {auth.create_access_token(admin)}"


@pytest.mark.parametrize("role", ["mahasiswa", "admin"])
def test_created_profile_survives_reload_and_login_with_existing_response_fields(context, role):
    app, db, authorization = context
    code, _, created = request(app, "POST", "/api/auth/users", authorization=authorization, body={
        "identifier": "new-user", "name": "Nama Pengguna", "password": "test-password-123",
        "role": role, "program_studi": "Prodi Uji", "fakultas": "Fakultas Uji",
    })
    assert code == 201
    db.expire_all()
    user = db.get(User, created["id"])
    assert user.nama_lengkap == "Nama Pengguna"
    assert user.program_studi == ("Prodi Uji" if role == "mahasiswa" else None)
    assert user.fakultas == ("Fakultas Uji" if role == "mahasiswa" else None)
    code, _, logged_in = request(app, "POST", "/api/auth/login", body={
        "identifier": "new-user", "password": "test-password-123",
    })
    assert code == 200
    assert logged_in["user"] == created
    code, _, profile = request(app, "GET", "/api/auth/me", authorization=f"Bearer {logged_in['token']}")
    assert code == 200
    assert profile["nama_lengkap"] == profile["name"] == "Nama Pengguna"
    assert profile["id"] == user.id
    _, _, users = request(app, "GET", "/api/auth/users", authorization=authorization)
    assert next(item for item in users if item["id"] == user.id)["name"] == "Nama Pengguna"


def test_profile_update_preserves_identity_history_and_existing_session(context):
    app, db, authorization = context
    student = db.query(User).filter_by(identifier="student").one()
    document = Document(user_id=student.id, title="Existing.pdf", file_path="existing.pdf")
    db.add(document)
    db.flush()
    check = PlagiarismCheck(user_id=student.id, document_id=document.id, overall_similarity=.42, status="completed")
    db.add(check)
    db.commit()
    student_id, document_id, check_id = student.id, document.id, check.id
    session_version = student.session_version
    token = auth.create_access_token(student)
    code, _, updated = request(app, "PUT", f"/api/auth/users/{student_id}", authorization=authorization, body={
        "nama_lengkap": "Nama Baru", "program_studi": "Prodi Baru", "fakultas": "Fakultas Baru",
    })
    assert code == 200
    assert updated["id"] == student_id
    assert updated["nama_lengkap"] == updated["name"] == "Nama Baru"
    assert updated["program_studi"] == "Prodi Baru"
    assert updated["fakultas"] == "Fakultas Baru"
    db.expire_all()
    assert db.get(User, student_id).session_version == session_version
    assert db.get(Document, document_id).user_id == student_id
    assert db.get(PlagiarismCheck, check_id).overall_similarity == .42
    code, _, profile = request(app, "GET", "/api/auth/me", authorization=f"Bearer {token}")
    assert code == 200
    assert profile["name"] == "Nama Baru"


def test_legacy_account_name_falls_back_to_identifier_and_admin_name_can_be_updated(context):
    app, db, authorization = context
    admin = db.query(User).filter_by(identifier="admin").one()
    assert admin.nama_lengkap is None
    assert request(app, "GET", "/api/auth/me", authorization=authorization)[2]["name"] == "admin"
    code, _, updated = request(app, "PUT", f"/api/auth/users/{admin.id}", authorization=authorization,
                               body={"name": "Nama Admin"})
    assert code == 200
    assert updated["nama_lengkap"] == updated["name"] == "Nama Admin"
    assert db.get(User, admin.id).nama_lengkap == "Nama Admin"


def test_student_cannot_edit_their_own_profile_through_admin_endpoint(context):
    app, db, _ = context
    student = db.query(User).filter_by(identifier="student").one()
    code, _, _ = request(app, "PUT", f"/api/auth/users/{student.id}",
                         authorization=f"Bearer {auth.create_access_token(student)}",
                         body={"nama_lengkap": "Unauthorized"})
    assert code == 403
    db.refresh(student)
    assert student.nama_lengkap is None
