"""
Repair jobs whose stored resume recommendation points at a resume the job's owner
does not have in their library (another user's resume, or a deleted one).

A bug in matching_service matched jobs against every user's resumes; those results
were saved on the job documents and shown as "Recommended Resume" in Jobs Explorer.

For each affected job this script:
  * backs up the job document to scripts/backups/
  * removes match_result / match and resets status MATCHED -> ANALYZED/DISCOVERED
  * deletes the cached job_matches record
  * with --rematch, re-runs matching against the owner's own resumes

Applications that already used another user's resume are only reported (they
reflect what was actually submitted); the runner now refuses foreign resumes.

Usage:
  python scripts/fix_cross_user_resume_matches.py                 # dry run on configured DB
  python scripts/fix_cross_user_resume_matches.py --apply         # write changes
  python scripts/fix_cross_user_resume_matches.py --apply --rematch
  python scripts/fix_cross_user_resume_matches.py --db apply2hire_testversion --apply
"""
import sys
import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

from bson import json_util

from app.config import settings
from app.database import get_database
from app.schemas.job import JDStructuredData


def _owner(value) -> str:
    return str(value) if value not in (None, "") else ""


def find_bad_resume_ids(job: dict, resume_owner: dict) -> list:
    """Resume ids referenced by the job's match data that the job owner does not own."""
    job_owner = _owner(job.get("user_id"))
    mr = job.get("match_result") or {}
    m = job.get("match") or {}
    ids = [mr.get("recommended_resume_id"), m.get("resume_id")]
    ids += [a.get("resume_id") for a in (mr.get("alternatives") or []) + (m.get("alternatives") or []) if isinstance(a, dict)]
    return sorted({i for i in ids if i and resume_owner.get(i) != job_owner})


async def repair(apply: bool, rematch: bool) -> dict:
    db = get_database()
    resume_owner = {str(r["_id"]): _owner(r.get("user_id")) async for r in db.resumes.find({}, {"user_id": 1})}

    affected = []
    async for job in db.jobs.find({"$or": [{"match_result": {"$exists": True}}, {"match": {"$exists": True}}]}):
        bad = find_bad_resume_ids(job, resume_owner)
        if bad:
            affected.append((job, bad))

    print(f"[fix] database={settings.DATABASE_NAME} affected_jobs={len(affected)} mode={'APPLY' if apply else 'DRY RUN'}")
    for job, bad in affected:
        reasons = ["deleted" if b not in resume_owner else f"owned by {resume_owner[b] or 'nobody'}" for b in bad]
        print(f"  job {job['_id']} user={_owner(job.get('user_id'))} '{job.get('title', '')[:50]}' -> "
              + ", ".join(f"{b} ({r})" for b, r in zip(bad, reasons)))

    foreign_apps = []
    async for a in db.applications.find({}, {"user_id": 1, "resume_id": 1, "resume_name": 1, "status": 1, "job_title": 1}):
        rid = _owner(a.get("resume_id"))
        if rid in resume_owner and resume_owner[rid] != _owner(a.get("user_id")):
            foreign_apps.append(a)
    if foreign_apps:
        print(f"[fix] {len(foreign_apps)} application(s) used another user's resume (reported only):")
        for a in foreign_apps:
            print(f"  application {a['_id']} user={_owner(a.get('user_id'))} status={a.get('status')} resume='{a.get('resume_name')}'")

    rematched = 0
    if apply and affected:
        backup_dir = backend_dir / "scripts" / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = backup_dir / f"jobs_cross_user_matches_{settings.DATABASE_NAME}_{stamp}.json"
        backup.write_text(json_util.dumps([j for j, _ in affected], indent=1), encoding="utf-8")
        print(f"[fix] backup written: {backup}")

        from app.services.matching_service import matching_service
        for job, _ in affected:
            status = job.get("status")
            if status == "MATCHED":
                status = "ANALYZED" if job.get("description_structured") else "DISCOVERED"
            await db.jobs.update_one(
                {"_id": job["_id"]},
                {"$unset": {"match_result": "", "match": ""},
                 "$set": {"status": status, "updated_at": datetime.now(timezone.utc)}},
            )
            await db.job_matches.delete_many({"job_id": str(job["_id"])})
            if rematch and job.get("description_structured") and job.get("user_id"):
                await matching_service.match_job_against_all_resumes(
                    JDStructuredData(**job["description_structured"]),
                    job_id=str(job["_id"]),
                    user_id=str(job["user_id"]),
                )
                rematched += 1
        print(f"[fix] cleared {len(affected)} job match(es); re-matched {rematched}")

    return {"affected_jobs": len(affected), "foreign_applications": len(foreign_apps), "rematched": rematched}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    parser.add_argument("--rematch", action="store_true", help="re-run matching against each owner's own resumes")
    parser.add_argument("--db", help="database name override, e.g. apply2hire_testversion")
    args = parser.parse_args()
    if args.db:
        settings.DATABASE_NAME = args.db
    asyncio.run(repair(args.apply, args.rematch))


if __name__ == "__main__":
    main()
