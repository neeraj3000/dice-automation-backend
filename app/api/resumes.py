from typing import List, Optional
from fastapi import APIRouter, UploadFile, File, HTTPException, Query
from app.schemas.resume import ResumeResponse, ResumeUpdate
from app.services.resume_service import resume_service

router = APIRouter(prefix="/resumes", tags=["Resumes"])

@router.post("/upload", response_model=List[ResumeResponse])
async def upload_resumes(files: List[UploadFile] = File(...)):
    results = []
    errors = []
    for file in files:
        try:
            res = await resume_service.save_uploaded_resume(file)
            results.append(res)
        except Exception as e:
            errors.append(f"{file.filename}: {str(e)}")
            
    if not results and errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    return results

@router.get("", response_model=List[ResumeResponse])
async def list_resumes(
    search: Optional[str] = Query(None, description="Search term across name, role, skills, or content"),
    role: Optional[str] = Query(None, description="Filter by target role")
):
    return await resume_service.get_resumes(search=search, role=role)

@router.get("/{resume_id}", response_model=ResumeResponse)
async def get_resume(resume_id: str):
    res = await resume_service.get_resume_by_id(resume_id)
    if not res:
        raise HTTPException(status_code=404, detail="Resume not found")
    return res

@router.put("/{resume_id}", response_model=ResumeResponse)
async def update_resume(resume_id: str, update_data: ResumeUpdate):
    res = await resume_service.update_resume(resume_id, update_data)
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

