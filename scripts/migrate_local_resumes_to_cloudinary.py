"""
One-time migration: move resume files that only exist in the old local `resumes/`
folder to Cloudinary, so the folder can be deleted.

For every resume document without a `cloudinary_url`, the local file is uploaded to
Cloudinary (folder resumes/<user_id>), downloaded back to verify it, and the document
gets `cloudinary_url` / `cloudinary_public_id`; the obsolete `file_path` is removed.

Usage:
  python scripts/migrate_local_resumes_to_cloudinary.py            # dry run
  python scripts/migrate_local_resumes_to_cloudinary.py --apply
  python scripts/migrate_local_resumes_to_cloudinary.py --db apply2hire_testversion --apply
"""
import sys
import argparse
import asyncio
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

import httpx

from app.config import settings
from app.database import get_database
from app.services.cloudinary_service import is_cloudinary_configured, upload_resume

LEGACY_RESUMES_DIR = backend_dir / "resumes"


def legacy_path(doc: dict) -> Path | None:
    candidates = []
    if doc.get("file_path"):
        candidates += [backend_dir / doc["file_path"], LEGACY_RESUMES_DIR / Path(doc["file_path"]).name]
    if doc.get("file_name"):
        candidates.append(LEGACY_RESUMES_DIR / doc["file_name"])
    return next((p for p in candidates if p.is_file()), None)


async def migrate(apply: bool) -> dict:
    if not is_cloudinary_configured():
        raise SystemExit("Cloudinary is not configured (.env CLOUDINARY_*); aborting.")
    db = get_database()
    stats = {"total": 0, "already_on_cloudinary": 0, "migrated": 0, "missing_file": 0}
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as http:
        async for doc in db.resumes.find({}):
            stats["total"] += 1
            if doc.get("cloudinary_url"):
                stats["already_on_cloudinary"] += 1
                if apply and doc.get("file_path"):
                    await db.resumes.update_one({"_id": doc["_id"]}, {"$unset": {"file_path": ""}})
                continue
            path = legacy_path(doc)
            if not path:
                stats["missing_file"] += 1
                print(f"  MISSING  {doc['_id']} user={doc.get('user_id')} {doc.get('file_name')}")
                continue
            print(f"  {'UPLOAD' if apply else 'would upload'}  {doc['_id']} user={doc.get('user_id')} {path.name}")
            if not apply:
                continue
            data = path.read_bytes()
            info = await upload_resume(data, user_id=str(doc.get("user_id") or "default"), file_name=doc.get("file_name") or path.name)
            check = await http.get(info["cloudinary_url"])
            if check.status_code != 200 or check.content != data:
                raise SystemExit(f"Verification failed for {doc['_id']} ({info['cloudinary_url']}); stopping.")
            await db.resumes.update_one(
                {"_id": doc["_id"]},
                {"$set": info, "$unset": {"file_path": ""}},
            )
            stats["migrated"] += 1
    print(f"[migrate] database={settings.DATABASE_NAME} mode={'APPLY' if apply else 'DRY RUN'} {stats}")
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="upload and update documents (default is a dry run)")
    parser.add_argument("--db", help="database name override, e.g. apply2hire_testversion")
    args = parser.parse_args()
    if args.db:
        settings.DATABASE_NAME = args.db
    asyncio.run(migrate(args.apply))


if __name__ == "__main__":
    main()
