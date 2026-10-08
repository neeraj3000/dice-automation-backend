from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends
from app.schemas.application import ReviewItem, ReviewAnswerSubmit
from app.services.application_service import application_service
from app.core.deps import get_current_user_optional

router = APIRouter(prefix="/review-queue", tags=["Review Queue"])

@router.get("", response_model=List[ReviewItem])
async def get_review_queue(user: Optional[dict] = Depends(get_current_user_optional)):
    user_id = user["_id"] if user else None
    return await application_service.get_review_queue(user_id=user_id)

@router.post("/{question_id}/answer")
async def answer_review_question(question_id: str, req: ReviewAnswerSubmit, user: Optional[dict] = Depends(get_current_user_optional)):
    user_id = user["_id"] if user else None
    success = await application_service.answer_review_question(question_id, req.answer_text, user_id=user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Question not found")
    return {"success": True, "message": "Answer recorded. If all questions are answered, application is marked READY."}
