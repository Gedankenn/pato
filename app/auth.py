import os
import jwt
from datetime import datetime, timedelta
from fastapi import Header, HTTPException, Request

SECRET = os.environ.get("JWT_SECRET", "pato-dev-secret-change-in-production")
ALGO = "HS256"


def create_token(barbershop_id: int) -> str:
    payload = {
        "barbershop_id": barbershop_id,
        "exp": datetime.utcnow() + timedelta(days=30),
    }
    return jwt.encode(payload, SECRET, algorithm=ALGO)


def get_current_barbershop_id(authorization: str = Header(None)) -> int:
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, SECRET, algorithms=[ALGO])
        return payload["barbershop_id"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_barbershop_id_from_request(request: Request) -> int | None:
    token = request.cookies.get("token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET, algorithms=[ALGO])
        return payload["barbershop_id"]
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None
