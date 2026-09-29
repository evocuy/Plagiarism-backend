import os
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes import auth, documents, plagiarism
from app.database.session import Base, SessionLocal, engine

load_dotenv()

# Buat tabel di PostgreSQL secara otomatis jika belum ada
Base.metadata.create_all(bind=engine)

# Inisialisasi akun pengguna awal
with SessionLocal() as db:
    auth.seed_initial_users(db)

app = FastAPI(title="Plagiarism Checker API", version="1.0.0")

# Baca CORS origins langsung dari .env (pisahkan koma)
cors_origins_env = os.getenv("CORS_ORIGINS", "") or os.getenv("ALLOWED_ORIGINS", "")
allow_origins = [origin.strip() for origin in cors_origins_env.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/auth", tags=["Authentication"])
app.include_router(documents.router, prefix="/api/documents", tags=["Documents"])
app.include_router(plagiarism.router, prefix="/api/plagiarism", tags=["Plagiarism"])

@app.get("/api/health")
def health_check():
    return {"status": "ok", "message": "Plagiarism Checker API is running"}