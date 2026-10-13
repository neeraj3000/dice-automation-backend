import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any
from bson import ObjectId
from fastapi import UploadFile, HTTPException

from app.config import settings
from app.database import get_database, sanitize_object_ids
from app.schemas.resume import ResumeResponse, ResumeUpdate, ResumeBrief
from app.services.parser_service import extract_text
from app.services.llm_service import llm_service

def format_resume_doc(doc: Dict[str, Any], default_resume_id: Optional[str] = None) -> ResumeResponse:
    if not doc:
        return None
    data = sanitize_object_ids(dict(doc))
    data["id"] = str(data.pop("_id", data.get("id", "")))
    if default_resume_id:
        data["is_default"] = (str(data["id"]) == str(default_resume_id))
    elif "is_default" not in data:
        data["is_default"] = False
    if "user_id" in data and data["user_id"] is not None:
        data["user_id"] = str(data["user_id"])
    if not data.get("display_name"):
        base = Path(data.get("file_name", "Resume")).stem.replace("_", " ")
        data["display_name"] = base or data.get("target_role", "Resume")
    if not data.get("file_type") and data.get("file_name"):
        data["file_type"] = Path(data["file_name"]).suffix.replace(".", "") or "pdf"
    if not data.get("created_at"):
        data["created_at"] = datetime.now(timezone.utc)
    if not data.get("updated_at"):
        data["updated_at"] = data.get("created_at") or datetime.now(timezone.utc)
    return ResumeResponse(**data)

class ResumeService:
    @property
    def collection(self):
        db = get_database()
        return db.resumes

    async def _extract_text_from_bytes(self, content: bytes, file_name: str, ext: str) -> str:
        from app.services.cloudinary_service import temp_file_from_bytes
        try:
            async with temp_file_from_bytes(content, file_name) as tmp_path:
                return extract_text(tmp_path, ext)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to extract text from document: {str(e)}")

    async def _store_in_cloudinary(self, content: bytes, user_id: Optional[Any], file_name: str) -> Dict[str, Any]:
        from app.services.cloudinary_service import is_cloudinary_configured, upload_resume
        if not is_cloudinary_configured():
            raise HTTPException(status_code=503, detail="Resume storage (Cloudinary) is not configured on the server.")
        try:
            info = await upload_resume(content, user_id=str(user_id or "default"), file_name=file_name)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to upload resume to Cloudinary: {str(e)}")
        if not info.get("cloudinary_url"):
            raise HTTPException(status_code=502, detail="Failed to upload resume to Cloudinary.")
        return info

    async def save_uploaded_resume(self, file: UploadFile, user_id: Optional[str] = None) -> ResumeResponse:
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

        content = await file.read()
        file_size = len(content)
        if file_size > settings.MAX_FILE_SIZE_MB * 1024 * 1024:
            raise HTTPException(
                status_code=400,
                detail=f"File exceeds maximum allowed size of {settings.MAX_FILE_SIZE_MB}MB"
            )

        # Extract text from a short-lived temp copy; nothing is kept on local disk
        raw_text = await self._extract_text_from_bytes(content, target_name, ext)

        # Extract metadata (LLM or Heuristic)
        metadata = await llm_service.parse_resume_metadata(raw_text, target_name)
        now = datetime.now(timezone.utc)

        # Cloudinary is the only place the resume file is stored
        cloudinary_info = await self._store_in_cloudinary(content, user_id, target_name)

        db = get_database()
        u_oids = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
        u_q = {"_id": {"$in": u_oids + [str(user_id)]}} if user_id else None
        user_doc = await db.users.find_one(u_q) if u_q else None
        is_first = bool(user_doc and not user_doc.get("default_resume_id"))

        doc = {
            "user_id": user_id,
            "file_name": target_name,
            "file_type": ext.replace(".", ""),
            "file_size": file_size,
            "cloudinary_url": cloudinary_info.get("cloudinary_url"),
            "cloudinary_public_id": cloudinary_info.get("cloudinary_public_id"),
            "display_name": metadata.display_name or base_name.replace("_", " "),
            "target_role": metadata.target_role,
            "skills": metadata.skills,
            "experience_years": metadata.experience_years,
            "summary": metadata.summary,
            "raw_text": raw_text,
            "is_default": is_first,
            "custom_fields": {},
            "created_at": now,
            "updated_at": now
        }

        result = await self.collection.insert_one(doc)
        doc["_id"] = result.inserted_id
        new_id_str = str(result.inserted_id)

        if is_first and u_q:
            await db.users.update_one(u_q, {"$set": {"default_resume_id": new_id_str, "updated_at": now}})

        def_id = new_id_str if is_first else (user_doc.get("default_resume_id") if user_doc else None)
        return format_resume_doc(doc, default_resume_id=def_id)

    async def get_resumes(self, search: Optional[str] = None, role: Optional[str] = None, user_id: Optional[Any] = None) -> List[ResumeResponse]:
        if not user_id:
            return []
        u_oids = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
        query = {"user_id": {"$in": u_oids + [str(user_id)]}}
        if search:
            regex = {"$regex": re.escape(search), "$options": "i"}
            search_clause = [
                {"display_name": regex},
                {"target_role": regex},
                {"skills": regex},
                {"file_name": regex},
                {"summary": regex}
            ]
            if "$or" in query:
                query["$and"] = [{"$or": query.pop("$or")}, {"$or": search_clause}]
            else:
                query["$or"] = search_clause
        if role:
            query["target_role"] = {"$regex": re.escape(role), "$options": "i"}

        db = get_database()
        user_doc = await db.users.find_one({"_id": {"$in": u_oids + [str(user_id)]}})
        def_id = user_doc.get("default_resume_id") if user_doc else None

        cursor = self.collection.find(query).sort("created_at", -1)
        resumes_raw = []
        async for doc in cursor:
            resumes_raw.append(doc)

        # Validate def_id against actual user's resumes
        valid_def_id = def_id if (def_id and any(str(r["_id"]) == str(def_id) for r in resumes_raw)) else None
        if not valid_def_id and resumes_raw:
            legacy_default = next((r for r in resumes_raw if r.get("is_default")), None)
            chosen = legacy_default or resumes_raw[0]
            valid_def_id = str(chosen["_id"])
            try:
                await db.users.update_one(
                    {"_id": {"$in": u_oids + [str(user_id)]}},
                    {"$set": {"default_resume_id": valid_def_id}}
                )
            except Exception:
                pass

        return [format_resume_doc(doc, default_resume_id=valid_def_id) for doc in resumes_raw]

    async def get_resume_by_id(self, resume_id: str, user_id: Optional[Any] = None) -> Optional[ResumeResponse]:
        if not ObjectId.is_valid(resume_id):
            return None
        query = {"_id": ObjectId(resume_id)}
        def_id = None
        if user_id:
            u_oids = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
            query["user_id"] = {"$in": u_oids + [str(user_id)]}
            db = get_database()
            user_doc = await db.users.find_one({"_id": {"$in": u_oids + [str(user_id)]}})
            def_id = user_doc.get("default_resume_id") if user_doc else None
        doc = await self.collection.find_one(query)
        return format_resume_doc(doc, default_resume_id=def_id) if doc else None

    async def update_resume(self, resume_id: str, update_data: ResumeUpdate, user_id: Optional[Any] = None) -> Optional[ResumeResponse]:
        if not ObjectId.is_valid(resume_id) or not user_id:
            return None

        u_oids = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
        query = {"_id": ObjectId(resume_id), "user_id": {"$in": u_oids + [str(user_id)]}}
        existing = await self.collection.find_one(query)
        if not existing:
            return None

        update_dict = update_data.model_dump(exclude_unset=True)
        if not update_dict:
            return format_resume_doc(existing)

        update_dict["updated_at"] = datetime.now(timezone.utc)
        result = await self.collection.find_one_and_update(
            query,
            {"$set": update_dict},
            return_document=True
        )
        return format_resume_doc(result)

    async def replace_resume_file(self, resume_id: str, file: UploadFile, user_id: Optional[Any] = None) -> Optional[ResumeResponse]:
        if not ObjectId.is_valid(resume_id) or not user_id:
            return None
        u_oids = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
        query = {"_id": ObjectId(resume_id), "user_id": {"$in": u_oids + [str(user_id)]}}
        existing_doc = await self.collection.find_one(query)
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

        raw_text = await self._extract_text_from_bytes(content, target_name, ext)

        # Extract metadata (LLM or Heuristic)
        metadata = await llm_service.parse_resume_metadata(raw_text, target_name)
        now = datetime.now(timezone.utc)

        cloudinary_info = await self._store_in_cloudinary(content, existing_doc.get("user_id"), target_name)
        if existing_doc.get("cloudinary_public_id"):
            from app.services.cloudinary_service import delete_resume
            await delete_resume(existing_doc["cloudinary_public_id"])

        update_dict = {
            "file_name": target_name,
            "file_type": ext.replace(".", ""),
            "file_size": file_size,
            "cloudinary_url": cloudinary_info.get("cloudinary_url"),
            "cloudinary_public_id": cloudinary_info.get("cloudinary_public_id"),
            "display_name": metadata.display_name or base_name.replace("_", " "),
            "target_role": metadata.target_role,
            "skills": metadata.skills,
            "experience_years": metadata.experience_years,
            "summary": metadata.summary,
            "raw_text": raw_text,
            "updated_at": now
        }

        updated = await self.collection.find_one_and_update(
            query,
            {"$set": update_dict, "$unset": {"file_path": ""}},
            return_document=True
        )
        return format_resume_doc(updated)

    async def delete_resume(self, resume_id: str, user_id: Optional[Any] = None) -> bool:
        if not ObjectId.is_valid(resume_id) or not user_id:
            return False

        u_oids = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
        query = {"_id": ObjectId(resume_id), "user_id": {"$in": u_oids + [str(user_id)]}}
        doc = await self.collection.find_one(query)
        if not doc:
            return False

        # Remove from Cloudinary if hosted remotely
        if doc.get("cloudinary_public_id"):
            try:
                from app.services.cloudinary_service import delete_resume
                await delete_resume(doc["cloudinary_public_id"])
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

        del_res = await self.collection.delete_one(query)
        if del_res.deleted_count == 0:
            return False

        # Clean up authoritative pointer if this was the default resume
        try:
            db = get_database()
            user_query = {"_id": {"$in": u_oids + [str(user_id)]}}
            user_doc = await db.users.find_one(user_query)
            is_user_default = (user_doc and str(user_doc.get("default_resume_id")) == str(resume_id)) or (doc.get("is_default") is True)
            if is_user_default:
                # Find a fallback resume for this user
                remaining = await self.collection.find(
                    {"user_id": {"$in": u_oids + [str(user_id)]}}
                ).sort("created_at", -1).to_list(1)

                if remaining:
                    new_def_id = str(remaining[0]["_id"])
                    await db.users.update_one(
                        user_query,
                        {"$set": {"default_resume_id": new_def_id, "updated_at": datetime.now(timezone.utc)}}
                    )
                    await self.collection.update_one(
                        {"_id": remaining[0]["_id"]},
                        {"$set": {"is_default": True}}
                    )
                else:
                    await db.users.update_one(
                        user_query,
                        {"$unset": {"default_resume_id": ""}, "$set": {"updated_at": datetime.now(timezone.utc)}}
                    )
        except Exception:
            pass

        return True

    async def reparse_resume(self, resume_id: str, user_id: Optional[Any] = None) -> Optional[ResumeResponse]:
        if not ObjectId.is_valid(resume_id) or not user_id:
            return None
        u_oids = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
        query = {"_id": ObjectId(resume_id), "user_id": {"$in": u_oids + [str(user_id)]}}
        doc = await self.collection.find_one(query)
        if not doc:
            return None

        metadata = await llm_service.parse_resume_metadata(doc["raw_text"], doc["file_name"])
        now = datetime.now(timezone.utc)
        updated = await self.collection.find_one_and_update(
            query,
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
        db = get_database()
        user_doc = await db.users.find_one({"_id": {"$in": u_oids + [str(user_id)]}})
        def_id = user_doc.get("default_resume_id") if user_doc else None
        return format_resume_doc(updated, default_resume_id=def_id)

    async def set_default_resume(self, resume_id: str, user_id: Optional[Any] = None) -> Optional[ResumeResponse]:
        if not ObjectId.is_valid(resume_id) or not user_id:
            return None
        u_oids = [ObjectId(user_id)] if ObjectId.is_valid(user_id) else []
        target_query = {"_id": ObjectId(resume_id), "user_id": {"$in": u_oids + [str(user_id)]}}
        target = await self.collection.find_one(target_query)
        if not target:
            return None

        db = get_database()
        user_query = {"_id": {"$in": u_oids + [str(user_id)]}}
        now = datetime.now(timezone.utc)

        # 1. Authoritative atomic update on user document
        await db.users.update_one(
            user_query,
            {"$set": {"default_resume_id": str(resume_id), "updated_at": now}}
        )

        # 2. Synchronize legacy is_default markers on resumes collection
        await self.collection.update_many(
            {"user_id": {"$in": u_oids + [str(user_id)]}, "_id": {"$ne": ObjectId(resume_id)}},
            {"$set": {"is_default": False}}
        )
        updated = await self.collection.find_one_and_update(
            target_query,
            {"$set": {"is_default": True, "updated_at": now}},
            return_document=True
        )
        return format_resume_doc(updated, default_resume_id=str(resume_id))

resume_service = ResumeService()
