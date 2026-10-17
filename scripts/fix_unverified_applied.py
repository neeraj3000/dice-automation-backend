"""
Relabels applications that were marked applied without Apply2Hire submitting them.

Older code marked an application SUBMITTED (and its job APPLIED) when the Dice job page merely
*looked* already applied before anything was submitted ("Job is verified as already applied on
Dice"). That check could match other jobs' "Applied" badges on the page, so these records may be
wrong. They are moved to SKIPPED, matching what the app records now.

Dry run by default (prints what would change). Pass --apply to write.
    python scripts/fix_unverified_applied.py [--user EMAIL] [--apply]
"""
import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser.application_browser import ALREADY_APPLIED_REASON  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import _create_client  # noqa: E402

PRECHECK_STEP = "Job is verified as already applied on Dice"
LEGACY_STEP = "Verified application submission confirmation on Dice!"


async def main(user_email: str, apply: bool):
    db = _create_client()[settings.DATABASE_NAME]
    query = {"status": {"$in": ["SUBMITTED", "APPLIED"]}, "progress_steps": PRECHECK_STEP}
    if user_email:
        user = await db.users.find_one({"email": user_email.strip().lower()}, {"_id": 1})
        if not user:
            raise SystemExit(f"No user with email {user_email}")
        uid = user["_id"]
        query["user_id"] = {"$in": [uid, str(uid)]}

    apps = await db.applications.find(query).to_list(None)
    print(f"Database: {settings.DATABASE_NAME}")
    print(f"{len(apps)} application(s) marked applied without a submission by Apply2Hire:")
    for a in apps:
        print(f"  - {a.get('job_title')} @ {a.get('company')}  ({a['_id']})")

    legacy = await db.applications.count_documents({**{k: v for k, v in query.items() if k != "progress_steps"}, "progress_steps": LEGACY_STEP})
    if legacy:
        print(f"\nNote: {legacy} older application(s) were confirmed by a text heuristic that is no longer used.")
        print("They are left unchanged; check them on Dice if in doubt.")

    if not apply:
        print("\nDry run: nothing changed. Re-run with --apply to relabel these as SKIPPED.")
        return

    now = datetime.now(timezone.utc)
    for a in apps:
        await db.applications.update_one(
            {"_id": a["_id"]},
            {
                "$set": {"status": "SKIPPED", "failure_reason": ALREADY_APPLIED_REASON, "updated_at": now},
                "$push": {"progress_steps": "Relabelled SKIPPED: this application was never submitted by Apply2Hire"},
            },
        )
        if a.get("job_id"):
            await db.jobs.update_one(
                {"_id": a["job_id"], "status": "APPLIED"},
                {"$set": {"status": "SKIPPED", "failure_reason": ALREADY_APPLIED_REASON, "updated_at": now}},
            )
    print(f"\nRelabelled {len(apps)} application(s) and their jobs as SKIPPED.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="", help="Only this user's applications (email)")
    ap.add_argument("--apply", action="store_true", help="Write the changes (default is a dry run)")
    args = ap.parse_args()
    asyncio.run(main(args.user, args.apply))
