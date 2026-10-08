from fastapi import APIRouter, Cookie, Depends, Response

from app.core.deps import get_current_user, rate_limit
from app.schemas.auth import AuthOut, GoogleIn, LoginIn, RegisterIn, UserOut
from app.services import auth_service
from app.services.auth_service import REFRESH_COOKIE

router = APIRouter(prefix="/auth", tags=["auth"])
limiter = Depends(rate_limit(30, 60))


@router.post("/register", response_model=AuthOut, status_code=201, dependencies=[limiter])
async def register(body: RegisterIn, response: Response):
    return await auth_service.register(body.name, body.email, body.password, response)


@router.post("/login", response_model=AuthOut, dependencies=[limiter])
async def login(body: LoginIn, response: Response):
    return await auth_service.login(body.email, body.password, response)


@router.post("/google", response_model=AuthOut, dependencies=[limiter])
async def google(body: GoogleIn, response: Response):
    return await auth_service.google_login(body.credential, response)


@router.post("/refresh", response_model=AuthOut)
async def refresh(response: Response, a2h_refresh: str | None = Cookie(default=None)):
    return await auth_service.refresh(a2h_refresh, response)


@router.post("/logout", status_code=204)
async def logout(response: Response, a2h_refresh: str | None = Cookie(default=None)):
    await auth_service.logout(a2h_refresh, response)


@router.get("/me", response_model=UserOut)
async def me(user: dict = Depends(get_current_user)):
    return auth_service.user_out(user)
