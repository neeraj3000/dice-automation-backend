from typing import List
from fastapi import APIRouter, HTTPException
from app.schemas.application import ReviewItem, ReviewAnswerSubmit
from app.services.application_service import application_service

router = APIRouter(prefix="/review-queue", tags=["Review Queue"])

@router.get("", response_model=List[ReviewItem])
async def get_review_queue():
    return await application_service.get_review_queue()

@router.post("/{question_id}/answer")
async def answer_review_question(question_id: str, req: ReviewAnswerSubmit):
    success = await application_service.answer_review_question(question_id, req.answer_text)
    if not success:
        raise HTTPException(status_code=404, detail="Question not found")
    return {"success": True, "message": "Answer recorded. If all questions are answered, application is marked READY."}
