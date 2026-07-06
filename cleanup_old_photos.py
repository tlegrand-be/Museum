"""One-time cleanup: delete roster photos already saved to the ledger.

New uploads are deleted automatically right after saving (see app.py's
/review route). This script catches photos uploaded before that behavior
existed, freeing disk space on PythonAnywhere's free tier. Only deletes
files tied to shifts already recorded in the database — anything from an
in-progress (not yet saved) review is left untouched.

Run once from the project root: python cleanup_old_photos.py
"""

from pathlib import Path

import database

UPLOAD_DIR = Path(__file__).parent / "uploads"


def main():
    conn = database.get_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_image FROM shifts WHERE source_image IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    deleted = 0
    freed_bytes = 0
    for row in rows:
        filepath = UPLOAD_DIR / row["source_image"]
        if filepath.is_file():
            freed_bytes += filepath.stat().st_size
            filepath.unlink()
            deleted += 1

    print(f"Deleted {deleted} photo(s), freeing {freed_bytes / 1024 / 1024:.1f} MB.")


if __name__ == "__main__":
    main()
