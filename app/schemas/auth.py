from pydantic import BaseModel, EmailStr, Field


class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class GoogleIn(BaseModel):
    credential: str = Field(min_length=10)


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    avatar: str | None = None


class AuthOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
