from __future__ import annotations

import uuid
from dataclasses import dataclass

import jwt
from fastapi import Cookie, HTTPException, status

from app.security import decode_token


@dataclass(frozen=True)
class AdminPrincipal:
    user_id: uuid.UUID
    username: str


@dataclass(frozen=True)
class StudentPrincipal:
    quiz_id: uuid.UUID
    student_number: str
    session_id: str


def require_admin(admin_session: str | None = Cookie(default=None)) -> AdminPrincipal:
    if not admin_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    try:
        payload = decode_token(admin_session, "admin")
        return AdminPrincipal(user_id=uuid.UUID(payload["sub"]), username=payload["username"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session 无效或已过期") from exc


def require_student(student_session: str | None = Cookie(default=None)) -> StudentPrincipal:
    if not student_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    try:
        payload = decode_token(student_session, "student")
        return StudentPrincipal(
            quiz_id=uuid.UUID(payload["quiz_id"]),
            student_number=payload["sub"],
            session_id=payload["sid"],
        )
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session 无效或已过期") from exc


def require_student_result(student_session: str | None = Cookie(default=None)) -> StudentPrincipal:
    if not student_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    try:
        try:
            payload = decode_token(student_session, "student_result")
        except jwt.PyJWTError:
            payload = decode_token(student_session, "student")
        return StudentPrincipal(quiz_id=uuid.UUID(payload["quiz_id"]),
            student_number=payload["sub"], session_id=payload.get("sid", ""))
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session 无效或已过期") from exc
