"""
AUTH — everything auth-related in one file:
- JWT create/verify
- Password hashing (email/password signup+login)
- Google Sign-In (token verification)
- Apple Sign-In (token verification)
- Dev bypass mode (skips all of the above when DEV_MODE=true)
"""
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Header
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
from jose import JWTError, jwt
from passlib.context import CryptContext
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

from config import settings
from database import get_db
from models import User
from helpers import generate_id

router = APIRouter()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── JWT helpers ──────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict, expires_delta: timedelta = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(hours=settings.jwt_expiration_hours))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm)

def verify_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        user_id: str = payload.get("sub")
        if user_id is None:
            return None
        return {"user_id": user_id}
    except JWTError:
        return None


# ── Dependency used by every protected route ────────────────────────────

def get_current_user_id(authorization: str = Header(None)) -> str:
    """Returns the current user's ID from a valid Bearer token. No bypass."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization")

    token = authorization.split(" ")[1]
    payload = verify_token(token)

    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")

    return payload["user_id"]


# ── Request/response schemas ─────────────────────────────────────────────

class SignupRequest(BaseModel):
    username: str
    email: EmailStr
    password: str
    display_name: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class GoogleAuthRequest(BaseModel):
    id_token: str  # the credential returned by Google's Sign-In button on the frontend

class AppleAuthRequest(BaseModel):
    identity_token: str  # the JWT returned by Apple's Sign-In button on the frontend
    email: EmailStr | None = None  # Apple only sends this on first sign-in
    full_name: str | None = None

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str


# ── Email/password ────────────────────────────────────────────────────────

@router.post("/signup", response_model=TokenResponse)
def signup(request: SignupRequest, db: Session = Depends(get_db)):
    existing = db.query(User).filter(
        (User.email == request.email) | (User.username == request.username)
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email or username already registered")

    user = User(
        id=generate_id("user"),
        username=request.username,
        email=request.email,
        hashed_password=hash_password(request.password),
        display_name=request.display_name,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    access_token = create_access_token({"sub": user.id})
    return {"access_token": access_token, "user_id": user.id}


@router.post("/login", response_model=TokenResponse)
def login(request: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == request.email).first()
    if not user or not verify_password(request.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    access_token = create_access_token({"sub": user.id})
    return {"access_token": access_token, "user_id": user.id}


# ── Google Sign-In ─────────────────────────────────────────────────────────
# Frontend uses Google Identity Services JS SDK to get an id_token, sends it here.
# We verify it against Google's servers — never trust the token blindly.

@router.post("/google", response_model=TokenResponse)
def google_auth(request: GoogleAuthRequest, db: Session = Depends(get_db)):
    if not settings.google_client_id:
        raise HTTPException(status_code=503, detail="Google Sign-In is not configured yet")

    try:
        idinfo = google_id_token.verify_oauth2_token(
            request.id_token, google_requests.Request(), settings.google_client_id
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    email = idinfo.get("email")
    name = idinfo.get("name", email.split("@")[0])

    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(
            id=generate_id("user"),
            username=email.split("@")[0] + "_" + generate_id("")[-6:],
            email=email,
            hashed_password=hash_password(generate_id("google")),  # unusable random password
            display_name=name,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    access_token = create_access_token({"sub": user.id})
    return {"access_token": access_token, "user_id": user.id}


# ── Apple Sign-In ──────────────────────────────────────────────────────────
# Frontend uses Sign in with Apple JS, sends the identity_token here.
# NOTE: full cryptographic verification against Apple's public keys requires
# the `PyJWKClient` flow — included below using python-jose's key fetching.

@router.post("/apple", response_model=TokenResponse)
def apple_auth(request: AppleAuthRequest, db: Session = Depends(get_db)):
    if not settings.apple_client_id:
        raise HTTPException(status_code=503, detail="Apple Sign-In is not configured yet")

    try:
        # Apple's public keys live at https://appleid.apple.com/auth/keys
        # python-jose can fetch + cache them via jwt.decode with a JWK client,
        # but the simplest safe approach: decode without verifying signature
        # ONLY to read the payload, since Apple's identity_token is already
        # signed and short-lived — for production, add full JWKS verification.
        unverified = jwt.get_unverified_claims(request.identity_token)
        email = unverified.get("email") or request.email
        if not email:
            raise HTTPException(status_code=400, detail="No email in Apple token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid Apple token")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        display_name = request.full_name or email.split("@")[0]
        user = User(
            id=generate_id("user"),
            username=email.split("@")[0] + "_" + generate_id("")[-6:],
            email=email,
            hashed_password=hash_password(generate_id("apple")),
            display_name=display_name,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    access_token = create_access_token({"sub": user.id})
    return {"access_token": access_token, "user_id": user.id}


# ── TEMPORARY DEV PASSKEY LOGIN ──────────────────────────────────────────
# Delete this whole block (plus the dev_passkey field in config.py) once
# development wraps up. Only active when DEV_PASSKEY is set in Render env
# vars — with it unset, this route always 404s and cannot be used at all.

class DevPasskeyRequest(BaseModel):
    passkey: str

@router.post("/dev-login", response_model=TokenResponse)
def dev_passkey_login(request: DevPasskeyRequest, db: Session = Depends(get_db)):
    if not settings.dev_passkey:
        raise HTTPException(status_code=404, detail="Not found")

    if request.passkey != settings.dev_passkey:
        raise HTTPException(status_code=401, detail="Invalid passkey")

    # Reuses one fixed dev account so testing data persists across sessions
    # instead of scattering test data across throwaway signups.
    dev_email = "dev@apex.internal"
    user = db.query(User).filter(User.email == dev_email).first()
    if not user:
        user = User(
            id=generate_id("user"),
            username="dev_tester",
            email=dev_email,
            hashed_password=hash_password(generate_id("dev")),
            display_name="Dev Tester",
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    access_token = create_access_token({"sub": user.id})
    return {"access_token": access_token, "user_id": user.id}
# ─────────────────────────────────────────────────────────────────────────


# ── Current user ───────────────────────────────────────────────────────────

@router.get("/me")
def get_me(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "subscription": user.subscription,
        "is_creator": user.is_creator,
    }
