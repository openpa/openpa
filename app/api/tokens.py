import os

import jwt
from datetime import datetime, timezone, timedelta

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.config.settings import BaseConfig, get_user_working_directory


async def generate_token(request: Request) -> JSONResponse:
    """Generate a signed JWT token with profile information."""
    if not BaseConfig.get_jwt_secret():
        return JSONResponse(
            {"error": "JWT authentication is not configured"},
            status_code=503,
        )

    try:
        body = await request.json()
    except Exception:
        body = {}

    subject = body.get("sub", "a2a-client")
    profile = body.get("profile", "")
    hours = body.get("expiration_hours", BaseConfig.JWT_EXPIRATION_HOURS)

    if not profile:
        return JSONResponse(
            {"error": "Profile is required"},
            status_code=400,
        )

    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "profile": profile,
        "iat": now,
        "exp": now + timedelta(hours=hours),
    }

    token = jwt.encode(payload, BaseConfig.get_jwt_secret(), algorithm="HS256")

    return JSONResponse({
        "token": token,
        "expires_at": (now + timedelta(hours=hours)).isoformat(),
        "subject": subject,
        "profile": profile,
    })


async def get_me(request: Request) -> JSONResponse:
    """Return user information extracted from the JWT token."""
    if not request.user.is_authenticated:
        return JSONResponse(
            {"error": "Authentication required"},
            status_code=401,
        )

    # Decode the full token payload to return profile info
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse({"error": "Invalid authorization header"}, status_code=401)

    token = auth_header.split("Bearer ", 1)[1]
    try:
        payload = jwt.decode(token, BaseConfig.get_jwt_secret(), algorithms=["HS256"])
        return JSONResponse({
            "sub": payload.get("sub", ""),
            "profile": payload.get("profile", ""),
            "exp": payload.get("exp"),
            "iat": payload.get("iat"),
            "working_dir": os.path.realpath(BaseConfig.OPENPA_WORKING_DIR).replace(os.sep, "/"),
            "user_working_dir": os.path.realpath(get_user_working_directory()).replace(os.sep, "/"),
        })
    except jwt.InvalidTokenError:
        return JSONResponse({"error": "Invalid token"}, status_code=401)


def get_token_routes() -> list[Route]:
    return [
        Route("/api/tokens", generate_token, methods=["POST"]),
        Route("/api/me", get_me, methods=["GET"]),
    ]
