from typing import List, Optional, Union, Any
from fastapi import APIRouter, UploadFile, File, HTTPException, Query, Depends
from app.schemas.resume import ResumeResponse, ResumeUpdate
from app.services.resume_service import resume_service
from app.core.deps import get_current_user_optional

router = APIRouter(prefix="/resumes", tags=["Resumes"])

@router.post("/upload")
async def upload_resumes(
    files: Optional[List[UploadFile]] = File(None),
    file: Optional[UploadFile] = File(None),
    user: Optional[dict] = Depends(get_current_user_optional)
):
    user_id = str(user["_id"]) if user else "default"
    items_to_upload = []
    if files:
        items_to_upload.extend(files)
    if file:
        items_to_upload.append(file)

    if not items_to_upload:
        raise HTTPException(status_code=400, detail="No resume file provided")

    results = []
    errors = []
    for f in items_to_upload:
        try:
            res = await resume_service.save_uploaded_resume(f, user_id=user_id)
            results.append(res)
        except Exception as e:
            errors.append(f"{f.filename}: {str(e)}")

    if not results and errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    # Return single object if single 'file' was uploaded, else list
    if file and len(items_to_upload) == 1:
        return results[0]
    return results

@router.get("", response_model=List[ResumeResponse])
async def list_resumes(
    search: Optional[str] = Query(None, description="Search term across name, role, skills, or content"),
    role: Optional[str] = Query(None, description="Filter by target role"),
    user: Optional[dict] = Depends(get_current_user_optional)
):
    return await resume_service.get_resumes(search=search, role=role)

@router.get("/{resume_id}", response_model=ResumeResponse)
async def get_resume(resume_id: str):
    res = await resume_service.get_resume_by_id(resume_id)
    if not res:
        raise HTTPException(status_code=404, detail="Resume not found")
    return res

@router.put("/{resume_id}", response_model=ResumeResponse)
@router.patch("/{resume_id}", response_model=ResumeResponse)
async def update_resume(resume_id: str, update_data: ResumeUpdate):
    res = await resume_service.update_resume(resume_id, update_data)
    if not res:
        raise HTTPException(status_code=404, detail="Resume not found")
    return res

@router.post("/{resume_id}/set-default", response_model=ResumeResponse)
async def set_default_resume(resume_id: str, user: Optional[dict] = Depends(get_current_user_optional)):
    user_id = str(user["_id"]) if user else "default"
    res = await resume_service.set_default_resume(resume_id, user_id=user_id)
    if not res:
        raise HTTPException(status_code=404, detail="Resume not found")
    return res

@router.delete("/{resume_id}")
async def delete_resume(resume_id: str):
    success = await resume_service.delete_resume(resume_id)
    if not success:
        raise HTTPException(status_code=404, detail="Resume not found or could not be deleted")
    return {"success": True, "message": "Resume deleted successfully"}

@router.post("/{resume_id}/replace", response_model=ResumeResponse)
async def replace_resume(resume_id: str, file: UploadFile = File(...)):
    res = await resume_service.replace_resume_file(resume_id, file)
    if not res:
        raise HTTPException(status_code=404, detail="Resume not found")
    return res

@router.post("/{resume_id}/reparse", response_model=ResumeResponse)
async def reparse_resume(resume_id: str):
    res = await resume_service.reparse_resume(resume_id)
    if not res:
        raise HTTPException(status_code=404, detail="Resume not found")
    return res

