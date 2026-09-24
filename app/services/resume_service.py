import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any
from bson import ObjectId
from fastapi import UploadFile, HTTPException

from app.config import settings
from app.database import get_database
from app.schemas.resume import ResumeResponse, ResumeUpdate, ResumeBrief
from app.services.parser_service import extract_text
from app.services.llm_service import llm_service

def format_resume_doc(doc: Dict[str, Any]) -> ResumeResponse:
    if not doc:
        return None
    data = dict(doc)
    data["id"] = str(data.pop("_id"))
    return ResumeResponse(**data)

class ResumeService:
    @property
    def collection(self):
        db = get_database()
        return db.resumes

    async def save_uploaded_resume(self, file: UploadFile) -> ResumeResponse:
        original_name = file.filename or "unnamed_resume"
        ext = Path(original_name).suffix.lower()
        if ext not in settings.ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file format '{ext}'. Allowed: {', '.join(settings.ALLOWED_EXTENSIONS)}"
            )

        # Sanitize filename
        base_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', Path(original_name).stem)
        target_name = f"{base_name}{ext}"
        target_path = settings.RESUMES_DIR / target_name

        # If file already exists, make filename unique with timestamp
        if target_path.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            target_name = f"{base_name}_{ts}{ext}"
            target_path = settings.RESUMES_DIR / target_name

        # Read and write content
        content = await file.read()
        file_size = len(content)
        if file_size > settings.MAX_FILE_SIZE_MB * 1024 * 1024:
            raise HTTPException(
                status_code=400,
                detail=f"File exceeds maximum allowed size of {settings.MAX_FILE_SIZE_MB}MB"
            )

        with open(target_path, "wb") as f:
            f.write(content)

        # Extract text
        try:
            raw_text = extract_text(target_path, ext)
        except Exception as e:
            if target_path.exists():
                os.remove(target_path)
            raise HTTPException(status_code=500, detail=f"Failed to extract text from document: {str(e)}")

        # Extract metadata (LLM or Heuristic)
        metadata = await llm_service.parse_resume_metadata(raw_text, target_name)
        now = datetime.now(timezone.utc)

        doc = {
            "file_name": target_name,
            "file_type": ext.replace(".", ""),
            "file_size": file_size,
            "file_path": str(target_path.relative_to(settings.RESUMES_DIR.parent)),
            "display_name": metadata.display_name or base_name.replace("_", " "),
            "target_role": metadata.target_role,
            "skills": metadata.skills,
            "experience_years": metadata.experience_years,
            "summary": metadata.summary,
            "raw_text": raw_text,
            "custom_fields": {},
            "created_at": now,
            "updated_at": now
        }

        result = await self.collection.insert_one(doc)
        doc["_id"] = result.inserted_id
        return format_resume_doc(doc)

    async def get_resumes(self, search: Optional[str] = None, role: Optional[str] = None) -> List[ResumeResponse]:
        query = {}
        if search:
            regex = {"$regex": re.escape(search), "$options": "i"}
            query["$or"] = [
                {"display_name": regex},
                {"target_role": regex},
                {"skills": regex},
                {"file_name": regex},
                {"summary": regex}
            ]
        if role:
            query["target_role"] = {"$regex": re.escape(role), "$options": "i"}

        cursor = self.collection.find(query).sort("created_at", -1)
        resumes = []
        async for doc in cursor:
            resumes.append(format_resume_doc(doc))
        return resumes

    async def get_resume_by_id(self, resume_id: str) -> Optional[ResumeResponse]:
        if not ObjectId.is_valid(resume_id):
            return None
        doc = await self.collection.find_one({"_id": ObjectId(resume_id)})
        return format_resume_doc(doc)

    async def update_resume(self, resume_id: str, update_data: ResumeUpdate) -> Optional[ResumeResponse]:
        if not ObjectId.is_valid(resume_id):
            return None

        update_dict = update_data.model_dump(exclude_unset=True)
        if not update_dict:
            return await self.get_resume_by_id(resume_id)

        update_dict["updated_at"] = datetime.now(timezone.utc)
        result = await self.collection.find_one_and_update(
            {"_id": ObjectId(resume_id)},
            {"$set": update_dict},
            return_document=True
        )
        return format_resume_doc(result)

    async def replace_resume_file(self, resume_id: str, file: UploadFile) -> Optional[ResumeResponse]:
        if not ObjectId.is_valid(resume_id):
            return None
        existing_doc = await self.collection.find_one({"_id": ObjectId(resume_id)})
        if not existing_doc:
            return None

        original_name = file.filename or "unnamed_resume"
        ext = Path(original_name).suffix.lower()
        if ext not in settings.ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file format '{ext}'. Allowed: {', '.join(settings.ALLOWED_EXTENSIONS)}"
            )

        content = await file.read()
        file_size = len(content)
        if file_size > settings.MAX_FILE_SIZE_MB * 1024 * 1024:
            raise HTTPException(
                status_code=400,
                detail=f"File exceeds maximum allowed size of {settings.MAX_FILE_SIZE_MB}MB"
            )

        # Sanitize filename
        base_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', Path(original_name).stem)
        target_name = f"{base_name}{ext}"
        target_path = settings.RESUMES_DIR / target_name

        # Clean up old file if filename changed
        old_rel_path = existing_doc.get("file_path", "")
        if old_rel_path:
            try:
                old_full = settings.RESUMES_DIR.parent / old_rel_path
                if old_full.exists() and old_full != target_path:
                    os.remove(old_full)
            except Exception:
                pass

        with open(target_path, "wb") as f:
            f.write(content)

        # Extract text
        try:
            raw_text = extract_text(target_path, ext)
        except Exception as e:
            if target_path.exists():
                os.remove(target_path)
            raise HTTPException(status_code=500, detail=f"Failed to extract text from document: {str(e)}")

        # Extract metadata (LLM or Heuristic)
        metadata = await llm_service.parse_resume_metadata(raw_text, target_name)
        now = datetime.now(timezone.utc)

        update_dict = {
            "file_name": target_name,
            "file_type": ext.replace(".", ""),
            "file_size": file_size,
            "file_path": str(target_path.relative_to(settings.RESUMES_DIR.parent)),
            "display_name": metadata.display_name or base_name.replace("_", " "),
            "target_role": metadata.target_role,
            "skills": metadata.skills,
            "experience_years": metadata.experience_years,
            "summary": metadata.summary,
            "raw_text": raw_text,
            "updated_at": now
        }

        updated = await self.collection.find_one_and_update(
            {"_id": ObjectId(resume_id)},
            {"$set": update_dict},
            return_document=True
        )
        return format_resume_doc(updated)

    async def delete_resume(self, resume_id: str) -> bool:
        if not ObjectId.is_valid(resume_id):
            return False

        doc = await self.collection.find_one({"_id": ObjectId(resume_id)})
        if not doc:
            return False

        # Remove local file if present
        try:
            rel_path = doc.get("file_path", "")
            if rel_path:
                full_path = settings.RESUMES_DIR.parent / rel_path
                if full_path.exists():
                    os.remove(full_path)
        except Exception:
            pass

        # Gracefully handle application references
        try:
            db = get_database()
            await db.applications.update_many(
                {"resume_id": ObjectId(resume_id)},
                {"$set": {"resume_name": f"{doc.get('display_name', 'Resume')} [Deleted]"}}
            )
        except Exception:
            pass

        await self.collection.delete_one({"_id": ObjectId(resume_id)})
        return True

    async def reparse_resume(self, resume_id: str) -> Optional[ResumeResponse]:
        if not ObjectId.is_valid(resume_id):
            return None
        doc = await self.collection.find_one({"_id": ObjectId(resume_id)})
        if not doc:
            return None

        metadata = await llm_service.parse_resume_metadata(doc["raw_text"], doc["file_name"])
        now = datetime.now(timezone.utc)
        updated = await self.collection.find_one_and_update(
            {"_id": ObjectId(resume_id)},
            {
                "$set": {
                    "display_name": metadata.display_name or doc.get("display_name"),
                    "target_role": metadata.target_role,
                    "skills": metadata.skills,
                    "experience_years": metadata.experience_years,
                    "summary": metadata.summary,
                    "updated_at": now
                }
            },
            return_document=True
        )
        return format_resume_doc(updated)

resume_service = ResumeService()
