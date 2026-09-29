"""Migration script to add approval columns to plagiarism_checks table"""
from app.database.session import engine
from sqlalchemy import text

def migrate():
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS approval_status VARCHAR DEFAULT 'belum disetujui'"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE plagiarism_checks ADD COLUMN IF NOT EXISTS reviewer_note TEXT"))
        conn.commit()
    print("Migration completed: approval_status, reviewed_at, reviewer_note columns added to plagiarism_checks")

if __name__ == "__main__":
    migrate()
