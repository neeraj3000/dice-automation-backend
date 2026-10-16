import re
import ipaddress
import hashlib
from urllib.parse import urlparse, parse_qs, urlunparse
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from bson import ObjectId
from fastapi import HTTPException

from app.database import get_database
from app.schemas.linkedin import (
    StatusProvenance,
    ExternalApplicationTrackRequest,
    ExternalApplicationUpdateRequest,
    PERMITTED_EXTERNAL_STATUSES,
)
from app.schemas.application import ApplicationResponse
from app.services.application_service import format_app_doc


# Blocked SSRF targets: localhost, link-local, loopback, private ranges, metadata service
DISALLOWED_HOSTNAMES = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "169.254.169.254", # AWS/GCP/Azure instance metadata
    "metadata.google.internal",
}


def is_disallowed_host(hostname: str) -> bool:
    """Checks if hostname or IP is loopback, link-local, or private (SSRF protection)."""
    if not hostname:
        return True
    host_lower = hostname.strip().lower()
    if host_lower in DISALLOWED_HOSTNAMES:
        return True
    try:
        ip = ipaddress.ip_address(host_lower)
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return True
    except ValueError:
        pass
    return False


def normalize_external_job_url(raw_url: str) -> Tuple[str, str]:
    """
    Validates and canonicalizes an external job URL.
    Returns:
        (canonical_url, external_job_id)
    Raises:
        HTTPException(400) if URL is invalid or unsafe.
    """
    if not raw_url or not isinstance(raw_url, str):
        raise HTTPException(status_code=400, detail="Job URL is required")

    url_str = raw_url.strip()
    try:
        parsed = urlparse(url_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL format")

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Only http and https protocols are supported")

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise HTTPException(status_code=400, detail="URL must include a valid hostname")

    if is_disallowed_host(hostname):
        raise HTTPException(status_code=400, detail="The provided URL points to a restricted host")

    path = parsed.path
    query = parsed.query
    parsed_qs = parse_qs(query)

    external_job_id = ""

    # LinkedIn Specific URL Parsing & Canonicalization
    if "linkedin.com" in hostname:
        # 1. /jobs/view/1234567890/ or /jobs/view/some-title-1234567890/
        view_match = re.search(r"/jobs/view/(?:[a-zA-Z0-9\-_]+-)?(\d+)", path)
        if view_match:
            external_job_id = view_match.group(1)
            canonical_url = f"https://www.linkedin.com/jobs/view/{external_job_id}/"
            return canonical_url, external_job_id

        # 2. Query param currentJobId=1234567890
        if "currentJobId" in parsed_qs and parsed_qs["currentJobId"]:
            candidate_id = parsed_qs["currentJobId"][0].strip()
            if candidate_id.isdigit():
                external_job_id = candidate_id
                canonical_url = f"https://www.linkedin.com/jobs/view/{external_job_id}/"
                return canonical_url, external_job_id

    # Generic or Direct Company Careers URL: Strip tracking parameters
    tracking_keys = {
        "trackingid", "refid", "trk", "ref", "source", "src",
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "fbclid", "gclid", "msclkid"
    }
    clean_params = []
    for k, v in parsed_qs.items():
        if k.lower() not in tracking_keys:
            for item in v:
                clean_params.append((k, item))

    # Reconstruct clean query
    clean_query = "&".join(f"{k}={v}" for k, v in clean_params)
    clean_path = path.rstrip("/") if len(path) > 1 else path
    canonical_url = urlunparse((scheme, hostname, clean_path, "", clean_query, ""))

    if not external_job_id:
        # Generate deterministic sha256 hash prefix for non-numeric/custom URLs
        external_job_id = hashlib.sha256(canonical_url.encode()).hexdigest()[:16]

    return canonical_url, external_job_id


class LinkedInService:
    @property
    def apps_col(self):
        return get_database().applications

    @property
    def jobs_col(self):
        return get_database().jobs

    @property
    def resumes_col(self):
        return get_database().resumes

    async def track_application(
        self,
        req: ExternalApplicationTrackRequest,
        user_id: str
    ) -> ApplicationResponse:
        """
        Idempotently tracks a user-confirmed LinkedIn or external job application.
        Strictly applies provenance 'user_confirmed' and enforces user isolation.
        """
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required")

        uid_str = str(user_id)
        u_oids = [ObjectId(uid_str)] if ObjectId.is_valid(uid_str) else []
        user_filter = {"$in": u_oids + [uid_str]}

        # 1. URL validation and normalization
        canonical_url, external_job_id = normalize_external_job_url(req.job_url)

        # 2. Validate status
        status = (req.status or "APPLIED").strip().upper()
        if status not in PERMITTED_EXTERNAL_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"Status '{status}' is not permitted for user-confirmed applications"
            )

        source = (req.source or "linkedin").strip().lower()
        now = datetime.now(timezone.utc)
        confirmed_at = req.confirmed_at or now

        # 3. Deduplication Check: Look for existing application for this user
        # Matches by either (user_id, source, external_job_id) or (user_id, source, canonical_url)
        or_clauses = [
            {"job_url": canonical_url},
            {"application_url": canonical_url},
        ]
        if external_job_id:
            or_clauses.append({"external_job_id": external_job_id})

        existing_app = await self.apps_col.find_one({
            "user_id": user_filter,
            "$or": or_clauses
        })

        if existing_app:
            # Idempotent update: update notes, status (if updated), confirmed_at, and record history
            app_id = existing_app["_id"]
            previous_status = existing_app.get("status", "")
            history = list(existing_app.get("status_history") or [])

            # Only append history if status changed or notes added
            notes_str = (req.notes or "").strip()
            if status != previous_status or notes_str:
                history.append({
                    "status": status,
                    "status_provenance": StatusProvenance.USER_CONFIRMED,
                    "at": now,
                    "notes": notes_str,
                    "actor": "user"
                })

            update_set: Dict[str, Any] = {
                "status": status,
                "status_provenance": StatusProvenance.USER_CONFIRMED,
                "confirmed_at": confirmed_at,
                "status_history": history,
                "updated_at": now
            }
            if notes_str:
                update_set["notes"] = notes_str
            if req.job_title:
                update_set["job_title"] = req.job_title.strip()
            if req.company:
                update_set["company"] = req.company.strip()
            if req.location:
                update_set["location"] = req.location.strip()
            if status in ("APPLIED", "SUBMITTED") and not existing_app.get("applied_at"):
                update_set["applied_at"] = confirmed_at

            await self.apps_col.update_one({"_id": app_id}, {"$set": update_set})

            # Update associated job doc if exists
            if existing_app.get("job_id"):
                await self.jobs_col.update_one(
                    {"_id": existing_app["job_id"]},
                    {"$set": {
                        "status": status,
                        "title": update_set.get("job_title", existing_app.get("job_title", "")),
                        "company": update_set.get("company", existing_app.get("company", "")),
                        "updated_at": now
                    }}
                )

            updated_app = await self.apps_col.find_one({"_id": app_id})
            return format_app_doc(updated_app)

        # 4. Create new job entry for this user
        job_doc = {
            "external_job_id": external_job_id,
            "title": req.job_title.strip(),
            "company": req.company.strip(),
            "location": (req.location or "").strip(),
            "job_url": canonical_url,
            "application_url": canonical_url,
            "url": canonical_url,
            "board": source,
            "status": status,
            "user_id": uid_str,
            "created_at": now,
            "updated_at": now
        }
        job_res = await self.jobs_col.insert_one(job_doc)
        job_id = job_res.inserted_id

        # 5. Create new application entry
        notes_str = (req.notes or "").strip()
        app_doc = {
            "job_id": ObjectId(job_id),
            "user_id": uid_str,
            "resume_id": ObjectId(req.resume_id) if (req.resume_id and ObjectId.is_valid(req.resume_id)) else None,
            "company": req.company.strip(),
            "job_title": req.job_title.strip(),
            "location": (req.location or "").strip(),
            "application_url": canonical_url,
            "job_url": canonical_url,
            "external_job_id": external_job_id,
            "external_job_url": canonical_url,
            "board": source,
            "source": source,
            "status": status,
            "status_provenance": StatusProvenance.USER_CONFIRMED,
            "notes": notes_str,
            "confirmed_at": confirmed_at,
            "applied_at": confirmed_at if status in ("APPLIED", "SUBMITTED") else None,
            "mode": "MANUAL",
            "progress_steps": [
                f"Application recorded via user confirmation for {source} at {now.strftime('%Y-%m-%d %H:%M:%S UTC')}"
            ],
            "status_history": [{
                "status": status,
                "status_provenance": StatusProvenance.USER_CONFIRMED,
                "at": now,
                "notes": notes_str,
                "actor": "user"
            }],
            "created_at": now,
            "updated_at": now
        }

        app_res = await self.apps_col.insert_one(app_doc)
        app_doc["_id"] = app_res.inserted_id

        return format_app_doc(app_doc)

    async def get_user_external_applications(
        self,
        user_id: str,
        source: Optional[str] = "linkedin"
    ) -> List[ApplicationResponse]:
        """Retrieves external user-confirmed applications for the authenticated user."""
        if not user_id:
            return []

        uid_str = str(user_id)
        u_oids = [ObjectId(uid_str)] if ObjectId.is_valid(uid_str) else []
        query: Dict[str, Any] = {
            "user_id": {"$in": u_oids + [uid_str]},
            "board": source if source else {"$in": ["linkedin", "external"]}
        }

        cursor = self.apps_col.find(query).sort("created_at", -1)
        apps = []
        async for doc in cursor:
            apps.append(format_app_doc(doc))
        return apps

    async def get_external_application(
        self,
        app_id: str,
        user_id: str
    ) -> Optional[ApplicationResponse]:
        """Enforces ownership and retrieves a single external application."""
        if not ObjectId.is_valid(app_id) or not user_id:
            return None

        uid_str = str(user_id)
        u_oids = [ObjectId(uid_str)] if ObjectId.is_valid(uid_str) else []
        doc = await self.apps_col.find_one({
            "_id": ObjectId(app_id),
            "user_id": {"$in": u_oids + [uid_str]}
        })
        if not doc:
            return None
        return format_app_doc(doc)

    async def update_external_application(
        self,
        app_id: str,
        user_id: str,
        req: ExternalApplicationUpdateRequest
    ) -> ApplicationResponse:
        """Enforces ownership and updates permitted status, notes, or job details."""
        if not ObjectId.is_valid(app_id) or not user_id:
            raise HTTPException(status_code=404, detail="Application not found")

        uid_str = str(user_id)
        u_oids = [ObjectId(uid_str)] if ObjectId.is_valid(uid_str) else []
        existing = await self.apps_col.find_one({
            "_id": ObjectId(app_id),
            "user_id": {"$in": u_oids + [uid_str]}
        })
        if not existing:
            raise HTTPException(status_code=404, detail="Application not found")

        now = datetime.now(timezone.utc)
        update_set: Dict[str, Any] = {"updated_at": now}
        history = list(existing.get("status_history") or [])

        if req.status:
            status_val = req.status.strip().upper()
            if status_val not in PERMITTED_EXTERNAL_STATUSES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Status '{status_val}' is not permitted"
                )
            update_set["status"] = status_val
            if status_val in ("APPLIED", "SUBMITTED") and not existing.get("applied_at"):
                update_set["applied_at"] = now

            # Append to history
            history.append({
                "status": status_val,
                "status_provenance": StatusProvenance.USER_CONFIRMED,
                "at": now,
                "notes": (req.notes or "").strip(),
                "actor": "user"
            })
            update_set["status_history"] = history

        if req.notes is not None:
            update_set["notes"] = req.notes.strip()

        if req.job_title:
            update_set["job_title"] = req.job_title.strip()

        if req.company:
            update_set["company"] = req.company.strip()

        if req.location is not None:
            update_set["location"] = req.location.strip()

        await self.apps_col.update_one({"_id": ObjectId(app_id)}, {"$set": update_set})

        # Update linked job record if exists
        if existing.get("job_id"):
            job_updates = {"updated_at": now}
            if req.status:
                job_updates["status"] = req.status.strip().upper()
            if req.job_title:
                job_updates["title"] = req.job_title.strip()
            if req.company:
                job_updates["company"] = req.company.strip()
            if req.location is not None:
                job_updates["location"] = req.location.strip()
            await self.jobs_col.update_one({"_id": existing["job_id"]}, {"$set": job_updates})

        updated_doc = await self.apps_col.find_one({"_id": ObjectId(app_id)})
        return format_app_doc(updated_doc)

    async def get_application_history(
        self,
        app_id: str,
        user_id: str
    ) -> List[Dict[str, Any]]:
        """Retrieves verified status history for an external application."""
        app = await self.get_external_application(app_id, user_id)
        if not app:
            raise HTTPException(status_code=404, detail="Application not found")
        return getattr(app, "status_history", []) or []


linkedin_service = LinkedInService()
