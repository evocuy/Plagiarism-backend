import os
from dotenv import load_dotenv

# Auth configuration is read while route modules are imported, so load the
# deployment environment before importing those modules.
load_dotenv()

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from app.routes import auth, dashboard, documents, plagiarism, support
from app.database.session import Base, SessionLocal, engine
from app.database.migrations import ensure_runtime_columns
from app.services.request_limit_service import RequestLimitMiddleware
from app.services.csrf_service import CsrfMiddleware, trusted_csrf_origins
from app.services.error_service import ErrorHandlingMiddleware, sanitized_validation_error

auth.validate_auth_configuration()

# Buat tabel di PostgreSQL secara otomatis jika belum ada
Base.metadata.create_all(bind=engine)
ensure_runtime_columns()

# Inisialisasi akun pengguna awal
with SessionLocal() as db:
    auth.seed_initial_users(db)

app = FastAPI(title="Plagiarism Checker API", version="1.0.0")
app.add_middleware(RequestLimitMiddleware)
app.add_middleware(ErrorHandlingMiddleware)
app.add_exception_handler(RequestValidationError, sanitized_validation_error)

# Baca CORS origins langsung dari .env (pisahkan koma)
cors_origins_env = os.getenv("CORS_ORIGINS", "") or os.getenv("ALLOWED_ORIGINS", "")
allow_origins = [origin.strip() for origin in cors_origins_env.split(",") if origin.strip()]
app.add_middleware(
    CsrfMiddleware,
    secret_key=auth._jwt_secret_key,
    session_cookie=auth.SESSION_COOKIE_NAME,
    impersonation_cookie=auth.IMPERSONATION_COOKIE_NAME,
    trusted_origins=trusted_csrf_origins(allow_origins),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Error-ID", "Retry-After"],
)

app.include_router(auth.router, prefix="/api/auth", tags=["Authentication"])
app.include_router(documents.router, prefix="/api/documents", tags=["Documents"])
app.include_router(plagiarism.router, prefix="/api/plagiarism", tags=["Plagiarism"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["Dashboard"])
app.include_router(support.router, prefix="/api/support", tags=["Support"])

@app.get("/api/health")
def health_check():
    return {"status": "ok", "message": "Plagiarism Checker API is running"}
