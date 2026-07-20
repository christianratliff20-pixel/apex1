"""
ROUTES — every non-auth endpoint in one file:
feed, coach, macros, workouts, vitals, community.
All mounted under different prefixes from main.py.
"""
from fastapi import APIRouter, Depends, HTTPException, Form, UploadFile, File
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timedelta
from anthropic import Anthropic
import mux_python

from database import get_db
from models import (
    FeedItem, User, MacroLog, WorkoutLog, VitalLog,
    Group, GroupMessage, Challenge, Comment, Collection, BodyMetrics, CoachMessage,
    Supplement, SupplementLog, CustomVitalType, CustomVitalLog, Question, Answer,
    WorkoutTemplate, CustomFood, MealPlan,
)
from auth import get_current_user_id
from helpers import generate_id
from moderation import run_moderation_pipeline
from config import settings

client = Anthropic()

# ── Mux client setup ──────────────────────────────────────────────────────
_mux_config = mux_python.Configuration()
_mux_config.username = settings.mux_token_id
_mux_config.password = settings.mux_token_secret
_mux_api_client = mux_python.ApiClient(_mux_config)
mux_uploads_api = mux_python.DirectUploadsApi(_mux_api_client)
mux_assets_api = mux_python.AssetsApi(_mux_api_client)

def mux_configured() -> bool:
    return bool(settings.mux_token_id and settings.mux_token_secret)

# One router per feature — main.py mounts each at its own prefix
feed_router = APIRouter()
coach_router = APIRouter()
macros_router = APIRouter()
workouts_router = APIRouter()
vitals_router = APIRouter()
body_router = APIRouter()
history_router = APIRouter()
food_router = APIRouter()
supplements_router = APIRouter()
community_router = APIRouter()


def _resolve_user(db: Session, user_id: str) -> User:
    """Looks up the authenticated user or 404s. No bypass."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


# ═══════════════════════════════════════════════════════════════════════
# FEED
# ═══════════════════════════════════════════════════════════════════════

@feed_router.get("")
def get_feed(category: str = None, limit: int = 20, offset: int = 0, db: Session = Depends(get_db)):
    query = db.query(FeedItem).filter(FeedItem.moderation_status == "live")
    if category:
        query = query.filter(FeedItem.category == category)

    total = query.count()
    items = query.order_by(FeedItem.created_at.desc()).limit(limit).offset(offset).all()

    result = []
    for item in items:
        author = db.query(User).filter(User.id == item.user_id).first()
        result.append({
            "id": item.id,
            "type": item.type,
            "title": item.title,
            "description": item.description,
            "category": item.category,
            "author": {
                "id": author.id, "username": author.username,
                "is_creator": author.is_creator, "is_coach": author.is_coach,
            } if author else None,
            "engagement": {"likes": item.likes, "comments": item.comments, "saves": item.saves, "shares": item.shares},
            "macros": item.macros,
            "tags": item.tags,
            "created_at": item.created_at.isoformat(),
            "moderation_status": item.moderation_status,
            "video_status": item.video_status,
            "mux_playback_id": item.mux_playback_id,
        })

    return {"items": result, "total": total, "limit": limit, "offset": offset}


@feed_router.post("/upload-url")
def create_video_upload_url(user_id: str = Depends(get_current_user_id)):
    """
    Step 1 of video upload: ask Mux for a signed direct-upload URL.
    The frontend PUTs the raw video file straight to that URL (never through
    our backend — API keys never touch the browser, and we're not proxying
    potentially huge video files through our own server).
    """
    if not mux_configured():
        raise HTTPException(status_code=503, detail="Video upload isn't configured yet (missing Mux credentials)")

    try:
        asset_settings = mux_python.CreateAssetRequest(playback_policy=["public"])
        upload_request = mux_python.CreateUploadRequest(
            cors_origin="*",  # tighten to your real frontend domain once live
            new_asset_settings=asset_settings,
        )
        response = mux_uploads_api.create_direct_upload(upload_request)
        upload = response.data
        return {"upload_id": upload.id, "upload_url": upload.url}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Mux upload creation failed: {e}")


@feed_router.get("/upload-status/{upload_id}")
def get_video_upload_status(upload_id: str, user_id: str = Depends(get_current_user_id)):
    """
    Step 3: poll this after the browser finishes PUTting the file to Mux,
    to find out the asset_id once Mux has picked up the upload.
    """
    if not mux_configured():
        raise HTTPException(status_code=503, detail="Video upload isn't configured yet (missing Mux credentials)")

    try:
        response = mux_uploads_api.get_direct_upload(upload_id)
        upload = response.data
        return {"status": upload.status, "asset_id": upload.asset_id}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Mux status check failed: {e}")


@feed_router.get("/{item_id}/video-status")
def get_feed_item_video_status(item_id: str, db: Session = Depends(get_db)):
    """
    Step 4: poll this to find out when transcoding is done and get the
    real playback_id, which is what's needed to actually render the video.
    """
    item = db.query(FeedItem).filter(FeedItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    if not item.mux_asset_id:
        return {"video_status": item.video_status, "playback_id": None}

    if not mux_configured():
        return {"video_status": item.video_status, "playback_id": item.mux_playback_id}

    try:
        response = mux_assets_api.get_asset(item.mux_asset_id)
        asset = response.data
        item.video_status = asset.status  # preparing, ready, errored

        if asset.status == "ready" and asset.playback_ids:
            item.mux_playback_id = asset.playback_ids[0].id
            db.commit()

        return {"video_status": item.video_status, "playback_id": item.mux_playback_id}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Mux asset check failed: {e}")


@feed_router.post("/upload")
async def upload_content(
    title: str = Form(...),
    description: str = Form(...),
    category: str = Form(...),
    sub_category: str = Form(...),
    tags: str = Form(""),
    mux_upload_id: str = Form(None),  # from step 1, if this post has a video attached
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    user = _resolve_user(db, user_id)

    moderation = await run_moderation_pipeline(title, description, category, sub_category)

    item_id = generate_id("feed")
    moderation_status = "live" if moderation["pass"] else "under_review"

    video_status = "none"
    mux_asset_id = None
    if mux_upload_id and mux_configured():
        # Video was uploaded via the 2-step flow — look up its asset_id now.
        try:
            response = mux_uploads_api.get_direct_upload(mux_upload_id)
            mux_asset_id = response.data.asset_id
            video_status = "waiting" if not mux_asset_id else "processing"
        except Exception:
            video_status = "errored"

    feed_item = FeedItem(
        id=item_id,
        user_id=user.id,
        type="video" if mux_upload_id else "fact",
        title=title,
        description=description,
        category=category,
        workout_type=sub_category if category == "workout" else None,
        food_type=sub_category if category == "food" else None,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        moderation_status=moderation_status,
        moderation_notes=moderation.get("reason", ""),
        mux_upload_id=mux_upload_id,
        mux_asset_id=mux_asset_id,
        video_status=video_status,
    )
    db.add(feed_item)
    db.commit()
    db.refresh(feed_item)

    return {
        "id": item_id,
        "moderation_status": moderation_status,
        "video_status": video_status,
        "message": "Posted!" if moderation["pass"] else "Under review",
    }


@feed_router.post("/{item_id}/like")
def like_item(item_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    item = db.query(FeedItem).filter(FeedItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    item.likes += 1
    db.commit()
    return {"likes": item.likes}


@feed_router.post("/{item_id}/save")
def save_item(item_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    item = db.query(FeedItem).filter(FeedItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    item.saves += 1
    db.commit()
    return {"saves": item.saves}


class CommentRequest(BaseModel):
    text: str

@feed_router.get("/{item_id}/comments")
def get_comments(item_id: str, db: Session = Depends(get_db)):
    """Real comments for a feed item. Empty list if none — never fake data."""
    comments = db.query(Comment).filter(Comment.feed_item_id == item_id).order_by(Comment.created_at.desc()).all()
    result = []
    for c in comments:
        author = db.query(User).filter(User.id == c.user_id).first()
        result.append({
            "id": c.id,
            "text": c.text,
            "likes": c.likes,
            "author": author.username if author else "unknown",
            "created_at": c.created_at.isoformat(),
        })
    return {"comments": result}


@feed_router.post("/{item_id}/comments")
def post_comment(item_id: str, request: CommentRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    item = db.query(FeedItem).filter(FeedItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Comment cannot be empty")

    comment = Comment(id=generate_id("comment"), feed_item_id=item_id, user_id=user.id, text=request.text.strip())
    db.add(comment)
    item.comments += 1
    db.commit()
    db.refresh(comment)

    return {
        "id": comment.id,
        "text": comment.text,
        "likes": comment.likes,
        "author": user.username,
        "created_at": comment.created_at.isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════
# COLLECTIONS
# ═══════════════════════════════════════════════════════════════════════

class CollectionCreateRequest(BaseModel):
    name: str

@feed_router.get("/collections")
def list_collections(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """Real list of the current user's collections. Empty list if none."""
    user = _resolve_user(db, user_id)
    collections = db.query(Collection).filter(Collection.user_id == user.id).order_by(Collection.created_at.desc()).all()
    return {
        "collections": [
            {"id": c.id, "name": c.name, "item_count": len(c.items or [])}
            for c in collections
        ]
    }


@feed_router.post("/collections")
def create_collection(request: CollectionCreateRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    if not request.name.strip():
        raise HTTPException(status_code=400, detail="Collection name cannot be empty")

    collection = Collection(id=generate_id("collection"), user_id=user.id, name=request.name.strip(), items=[])
    db.add(collection)
    db.commit()
    db.refresh(collection)
    return {"id": collection.id, "name": collection.name, "item_count": 0}


@feed_router.post("/collections/{collection_id}/items/{item_id}")
def add_item_to_collection(collection_id: str, item_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    collection = db.query(Collection).filter(Collection.id == collection_id, Collection.user_id == user.id).first()
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    item = db.query(FeedItem).filter(FeedItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Feed item not found")

    items = list(collection.items or [])
    if item_id not in items:
        items.append(item_id)
        collection.items = items
        item.saves += 1
        db.commit()

    return {"id": collection.id, "name": collection.name, "item_count": len(collection.items)}


# ═══════════════════════════════════════════════════════════════════════
# COACH
# ═══════════════════════════════════════════════════════════════════════

class CoachMessageRequest(BaseModel):
    message: str

COACH_HISTORY_LIMIT = 40  # most recent messages kept in context — bounds token growth over months of use

@coach_router.post("/message")
def coach_message(request: CoachMessageRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    week_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")

    # ── Today's macros ──
    macro_log = db.query(MacroLog).filter(MacroLog.user_id == user.id, MacroLog.log_date == today).first()
    logged = {
        "calories": macro_log.calories if macro_log else 0,
        "protein": macro_log.protein if macro_log else 0,
        "carbs": macro_log.carbs if macro_log else 0,
        "fat": macro_log.fat if macro_log else 0,
    }
    goals = {"calories": user.goal_calories, "protein": user.goal_protein, "carbs": user.goal_carbs, "fat": user.goal_fat}

    # ── Recent workout history (last 7 days) ──
    recent_workouts = db.query(WorkoutLog).filter(
        WorkoutLog.user_id == user.id, WorkoutLog.log_date >= week_ago
    ).order_by(WorkoutLog.log_date.desc()).all()
    workout_lines = [
        f"  - {w.log_date}: {w.name} ({w.duration}min, energy {w.energy_level}/5)" for w in recent_workouts
    ] or ["  (none logged this week)"]

    # ── Body metrics trend ──
    body_entries = db.query(BodyMetrics).filter(BodyMetrics.user_id == user.id).order_by(BodyMetrics.metric_date.desc()).limit(2).all()
    body_line = "  (no body metrics logged yet)"
    if body_entries:
        latest = body_entries[0]
        body_line = f"  Latest: weight {latest.weight or '—'}lbs, body fat {latest.body_fat or '—'}%"
        if len(body_entries) > 1:
            prev = body_entries[1]
            if latest.weight and prev.weight:
                delta = round(latest.weight - prev.weight, 1)
                body_line += f" (weight change since last entry: {'+' if delta > 0 else ''}{delta}lbs)"

    # ── Today's vitals ──
    vital = db.query(VitalLog).filter(VitalLog.user_id == user.id, VitalLog.log_date == today).first()
    vitals_line = "  (not logged today)"
    if vital:
        vitals_line = f"  Water {vital.water}oz, sleep {vital.sleep}hrs, energy {vital.energy_level}/5, mood {vital.mood}/5"

    # ── Persisted conversation history (most recent N messages, oldest first) ──
    history = db.query(CoachMessage).filter(CoachMessage.user_id == user.id).order_by(
        CoachMessage.created_at.desc()
    ).limit(COACH_HISTORY_LIMIT).all()
    history = list(reversed(history))  # chronological order for the model

    system_prompt = f"""You are Apex AI Coach — direct, data-driven, no fluff.
User preference: "{user.coach_personality}"

Today's macros:
- Calories: {logged['calories']}/{goals['calories']} (remaining: {goals['calories'] - logged['calories']})
- Protein: {logged['protein']}g (goal: {goals['protein']}g)
- Carbs: {logged['carbs']}g (goal: {goals['carbs']}g)
- Fat: {logged['fat']}g (goal: {goals['fat']}g)

This week's workouts:
{chr(10).join(workout_lines)}

Body metrics:
{body_line}

Today's vitals:
{vitals_line}

You have access to the full conversation history below — use it. Don't repeat advice you've
already given, reference things the user has told you before when relevant, and notice patterns
across time (e.g. "you mentioned feeling low energy on leg days three times this month").

Respond in 2-4 sentences. Be specific to their actual data. No generic advice."""

    # Build the messages array from persisted history + the new message
    claude_messages = [{"role": ("user" if h.role == "user" else "assistant"), "content": h.text} for h in history]
    claude_messages.append({"role": "user", "content": request.message})

    try:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1000,
            system=system_prompt,
            messages=claude_messages,
        )
        reply_text = response.content[0].text

        # Persist both turns so memory survives across sessions/devices
        db.add(CoachMessage(id=generate_id("coachmsg"), user_id=user.id, role="user", text=request.message))
        db.add(CoachMessage(id=generate_id("coachmsg"), user_id=user.id, role="coach", text=reply_text))
        db.commit()

        return {"response": reply_text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Coach service error: {e}")


@coach_router.get("/history")
def get_coach_history(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """Full persisted conversation, so the frontend can restore it on load — real memory, not session-only state."""
    user = _resolve_user(db, user_id)
    history = db.query(CoachMessage).filter(CoachMessage.user_id == user.id).order_by(CoachMessage.created_at.asc()).all()
    return {
        "messages": [{"role": h.role, "text": h.text, "created_at": h.created_at.isoformat()} for h in history]
    }


@coach_router.post("/analyze-food-photo")
async def analyze_food_photo(
    photo: UploadFile = File(...),
    description: str = Form(""),  # optional user-provided context, e.g. "grilled chicken bowl, no rice"
    user_id: str = Depends(get_current_user_id),
):
    """
    Real photo-based food logging: user takes/uploads a photo, Claude's
    vision model estimates the meal identity and macros. Returns a
    structured estimate the user can review/edit before it's actually
    logged via the existing /api/macros/log endpoint — this never writes
    to the database itself, since an AI estimate should be a suggestion,
    not an automatic log entry.
    """
    import base64, json as json_module

    contents = await photo.read()
    if len(contents) > 8 * 1024 * 1024:  # 8MB safety cap
        raise HTTPException(status_code=400, detail="Photo is too large (max 8MB)")

    media_type = photo.content_type or "image/jpeg"
    if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        raise HTTPException(status_code=400, detail=f"Unsupported image type: {media_type}")

    image_b64 = base64.standard_b64encode(contents).decode("utf-8")

    context_line = f"\nAdditional context from the user: {description}" if description.strip() else ""

    try:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=800,
            system=(
                "You are a nutrition estimation assistant. Given a photo of food, identify what it "
                "likely is and estimate its macros as realistically as possible for a typical single "
                "serving shown in the image. Respond with ONLY valid JSON, no other text, in exactly "
                "this shape: "
                '{"title": "short dish name", "description": "1-sentence description", '
                '"calories": number, "protein": number, "carbs": number, "fat": number, '
                '"confidence": "high"|"medium"|"low"}. '
                "If you cannot identify food in the image at all, set title to \"Unrecognized\" and "
                "confidence to \"low\" with all macros at 0."
            ),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                    {"type": "text", "text": f"Estimate the macros for this meal.{context_line}"},
                ],
            }],
        )

        raw_text = response.content[0].text.strip()
        # Claude sometimes wraps JSON in ```json fences despite instructions — strip defensively.
        cleaned = raw_text.replace("```json", "").replace("```", "").strip()
        estimate = json_module.loads(cleaned)

        return {
            "title": estimate.get("title", "Unrecognized meal"),
            "description": estimate.get("description", ""),
            "calories": estimate.get("calories", 0),
            "protein": estimate.get("protein", 0),
            "carbs": estimate.get("carbs", 0),
            "fat": estimate.get("fat", 0),
            "confidence": estimate.get("confidence", "low"),
        }
    except json_module.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Couldn't parse the nutrition estimate — try a clearer photo")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Photo analysis failed: {e}")


# ═══════════════════════════════════════════════════════════════════════
# MACROS
# ═══════════════════════════════════════════════════════════════════════

class MacroLogRequest(BaseModel):
    date: str
    calories: float = 0
    protein: float = 0
    carbs: float = 0
    fat: float = 0

@macros_router.post("/log")
def log_macros(request: MacroLogRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    existing = db.query(MacroLog).filter(MacroLog.user_id == user.id, MacroLog.log_date == request.date).first()

    if existing:
        existing.calories, existing.protein, existing.carbs, existing.fat = request.calories, request.protein, request.carbs, request.fat
        db.commit()
        return {"id": existing.id, "status": "updated"}

    log = MacroLog(id=generate_id("macro"), user_id=user.id, log_date=request.date,
                    calories=request.calories, protein=request.protein, carbs=request.carbs, fat=request.fat)
    db.add(log)
    db.commit()
    db.refresh(log)
    return {"id": log.id, "status": "created"}


@macros_router.get("/{date}")
def get_macros_for_date(date: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    log = db.query(MacroLog).filter(MacroLog.user_id == user.id, MacroLog.log_date == date).first()
    if not log:
        return {"calories": 0, "protein": 0, "carbs": 0, "fat": 0}
    return {"id": log.id, "date": log.log_date, "calories": log.calories, "protein": log.protein, "carbs": log.carbs, "fat": log.fat}


@macros_router.get("/weekly/average")
def get_weekly_average(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    today = datetime.utcnow()
    start_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    logs = db.query(MacroLog).filter(MacroLog.user_id == user.id, MacroLog.log_date >= start_date, MacroLog.log_date <= end_date).all()
    if not logs:
        return {"days": [], "averages": {"calories": 0, "protein": 0, "carbs": 0, "fat": 0}}

    n = len(logs)
    return {
        "days": [l.log_date for l in logs],
        "averages": {
            "calories": round(sum(l.calories for l in logs) / n, 1),
            "protein": round(sum(l.protein for l in logs) / n, 1),
            "carbs": round(sum(l.carbs for l in logs) / n, 1),
            "fat": round(sum(l.fat for l in logs) / n, 1),
        },
    }


# ═══════════════════════════════════════════════════════════════════════
# WORKOUTS
# ═══════════════════════════════════════════════════════════════════════

class ExerciseSet(BaseModel):
    reps: int
    weight: float
    notes: Optional[str] = None

class Exercise(BaseModel):
    name: str
    sets: List[ExerciseSet]

class WorkoutLogRequest(BaseModel):
    date: str
    name: str
    duration: int
    activity_type: str = "strength"  # "strength", "running", "cycling", "swimming", etc. — not limited to the exercise library
    distance: Optional[float] = None  # miles, for cardio-type activities
    exercises: List[Exercise] = []
    energy_level: int = 3
    notes: Optional[str] = None
    completed: bool = False
    source: str = "logged"  # "logged" (backfilled after the fact), "live" (completed through the app), "template" (from a saved routine)
    template_id: Optional[str] = None

class WorkoutTemplateRequest(BaseModel):
    name: str
    exercises: List[dict] = []  # [{"name": "Bench Press", "target_sets": 3, "target_reps": 8, "timer_seconds": null}]

# ── Exercise library — proxies wger.de's public, no-auth-required API ──────
# wger is an open-source fitness database (~845 exercises, CC-BY-SA 4.0,
# commercial use OK with attribution). We proxy rather than call it
# directly from the frontend so we can cache and normalize the response
# shape, and so API keys/rate limits are never a frontend concern.
import requests as http_requests

WGER_BASE = "https://wger.de/api/v2"
WGER_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ApexFitnessApp/1.0)"}
_exercise_cache = {"categories": None, "equipment": None}

# ── Workout templates — the "Build" side, saved routines you can reuse ──────
# NOTE: registered before GET /{date} below, same route-ordering reason as
# the custom-vitals fix — /templates would otherwise be swallowed by the
# catch-all date parameter route.

@workouts_router.get("/templates")
def list_workout_templates(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    templates = db.query(WorkoutTemplate).filter(WorkoutTemplate.user_id == user.id, WorkoutTemplate.active == True).order_by(WorkoutTemplate.created_at.desc()).all()
    return {
        "templates": [
            {"id": t.id, "name": t.name, "exercises": t.exercises, "exercise_count": len(t.exercises or [])}
            for t in templates
        ]
    }


@workouts_router.post("/templates")
def create_workout_template(request: WorkoutTemplateRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    if not request.name.strip():
        raise HTTPException(status_code=400, detail="Template name cannot be empty")
    template = WorkoutTemplate(id=generate_id("template"), user_id=user.id, name=request.name.strip(), exercises=request.exercises)
    db.add(template)
    db.commit()
    db.refresh(template)
    return {"id": template.id, "name": template.name}


@workouts_router.get("/templates/{template_id}")
def get_workout_template(template_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    template = db.query(WorkoutTemplate).filter(WorkoutTemplate.id == template_id, WorkoutTemplate.user_id == user.id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"id": template.id, "name": template.name, "exercises": template.exercises}


@workouts_router.delete("/templates/{template_id}")
def delete_workout_template(template_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    template = db.query(WorkoutTemplate).filter(WorkoutTemplate.id == template_id, WorkoutTemplate.user_id == user.id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    template.active = False
    db.commit()
    return {"status": "removed"}

@workouts_router.get("/exercises/search")
def search_exercises(term: str = "", category: str = None, limit: int = 30):
    """Search the exercise library. Empty term returns a general list."""
    try:
        if term:
            resp = http_requests.get(
                f"{WGER_BASE}/exercise/search/",
                params={"term": term, "language": "english", "format": "json"},
                headers=WGER_HEADERS,
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            results = [
                {"id": s["data"]["id"], "name": s["data"]["name"], "category": s["data"].get("category")}
                for s in data.get("suggestions", [])[:limit]
            ]
        else:
            params = {"language": 2, "limit": limit, "format": "json"}
            if category:
                params["category"] = category
            resp = http_requests.get(f"{WGER_BASE}/exercise/", params=params, headers=WGER_HEADERS, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            results = [
                {"id": r["id"], "name": r.get("name", "Unnamed"), "category": r.get("category")}
                for r in data.get("results", [])
            ]
        return {"exercises": results}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Exercise library lookup failed: {e}")


@workouts_router.get("/exercises/categories")
def get_exercise_categories():
    """Muscle group / category list — cached in-process since this rarely changes."""
    if _exercise_cache["categories"] is not None:
        return {"categories": _exercise_cache["categories"]}
    try:
        resp = http_requests.get(f"{WGER_BASE}/exercisecategory/", params={"format": "json", "limit": 50}, headers=WGER_HEADERS, timeout=8)
        resp.raise_for_status()
        categories = [{"id": c["id"], "name": c["name"]} for c in resp.json().get("results", [])]
        _exercise_cache["categories"] = categories
        return {"categories": categories}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Category lookup failed: {e}")


@workouts_router.get("/exercises/{exercise_id}")
def get_exercise_detail(exercise_id: int):
    """Full detail for one exercise — description, muscles, equipment, images."""
    try:
        resp = http_requests.get(f"{WGER_BASE}/exerciseinfo/{exercise_id}/", params={"format": "json"}, headers=WGER_HEADERS, timeout=8)
        resp.raise_for_status()
        data = resp.json()

        translations = [t for t in data.get("translations", []) if t.get("language") == 2]
        t = translations[0] if translations else (data.get("translations") or [{}])[0]

        import re, html as html_module
        raw_desc = t.get("description", "")
        clean_desc = re.sub("<[^>]+>", "", html_module.unescape(raw_desc)).strip()

        images = [img.get("image") for img in data.get("images", []) if img.get("image")]

        return {
            "id": data.get("id"),
            "name": t.get("name", "Unnamed"),
            "description": clean_desc,
            "category": (data.get("category") or {}).get("name"),
            "muscles_primary": [m.get("name") for m in data.get("muscles", [])],
            "muscles_secondary": [m.get("name") for m in data.get("muscles_secondary", [])],
            "equipment": [e.get("name") for e in data.get("equipment", [])],
            "images": images,
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Exercise detail lookup failed: {e}")

@workouts_router.post("/log")
def log_workout(request: WorkoutLogRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    workout = WorkoutLog(
        id=generate_id("workout"), user_id=user.id, log_date=request.date, name=request.name,
        duration=request.duration, activity_type=request.activity_type, distance=request.distance,
        exercises=[ex.model_dump() for ex in request.exercises],
        energy_level=request.energy_level, notes=request.notes, completed=request.completed,
        source=request.source, template_id=request.template_id,
    )
    db.add(workout)
    db.commit()
    db.refresh(workout)
    return {"id": workout.id, "status": "logged"}


@workouts_router.get("/{date}")
def get_workouts_for_date(date: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    workouts = db.query(WorkoutLog).filter(WorkoutLog.user_id == user.id, WorkoutLog.log_date == date).all()
    return {"workouts": [
        {"id": w.id, "name": w.name, "duration": w.duration, "energy_level": w.energy_level,
         "completed": w.completed, "exercises": w.exercises}
        for w in workouts
    ]}


@workouts_router.get("/weekly/summary")
def get_weekly_summary(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    start_date = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    workouts = db.query(WorkoutLog).filter(WorkoutLog.user_id == user.id, WorkoutLog.log_date >= start_date, WorkoutLog.completed == True).all()

    total_duration = sum(w.duration for w in workouts) if workouts else 0
    avg_energy = sum(w.energy_level for w in workouts) / len(workouts) if workouts else 0
    return {"completed": len(workouts), "total_minutes": total_duration, "avg_energy": round(avg_energy, 1)}


# ═══════════════════════════════════════════════════════════════════════
# VITALS
# ═══════════════════════════════════════════════════════════════════════

class VitalLogRequest(BaseModel):
    date: str
    water: float = 0
    sleep: float = 0
    energy_level: int = 3
    mood: int = 3

@vitals_router.post("/log")
def log_vitals(request: VitalLogRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    existing = db.query(VitalLog).filter(VitalLog.user_id == user.id, VitalLog.log_date == request.date).first()

    if existing:
        existing.water, existing.sleep, existing.energy_level, existing.mood = request.water, request.sleep, request.energy_level, request.mood
        db.commit()
        return {"id": existing.id, "status": "updated"}

    vital = VitalLog(id=generate_id("vital"), user_id=user.id, log_date=request.date,
                      water=request.water, sleep=request.sleep, energy_level=request.energy_level, mood=request.mood)
    db.add(vital)
    db.commit()
    db.refresh(vital)
    return {"id": vital.id, "status": "created"}


# ── Custom vital types — user-defined fields beyond water/sleep/energy/mood ──
# NOTE: these MUST be registered before GET /{date} below — FastAPI matches
# routes in registration order, and /{date} is a catch-all that would
# otherwise swallow requests to /custom-types (matching "custom-types" as
# if it were a date string).

class CustomVitalTypeRequest(BaseModel):
    name: str
    unit: Optional[str] = None

@vitals_router.get("/custom-types")
def list_custom_vital_types(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    types = db.query(CustomVitalType).filter(CustomVitalType.user_id == user.id, CustomVitalType.active == True).order_by(CustomVitalType.created_at.asc()).all()
    return {"types": [{"id": t.id, "name": t.name, "unit": t.unit} for t in types]}


@vitals_router.post("/custom-types")
def create_custom_vital_type(request: CustomVitalTypeRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    if not request.name.strip():
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    item = CustomVitalType(id=generate_id("vitaltype"), user_id=user.id, name=request.name.strip(), unit=request.unit)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "name": item.name, "unit": item.unit}


@vitals_router.delete("/custom-types/{type_id}")
def delete_custom_vital_type(type_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    item = db.query(CustomVitalType).filter(CustomVitalType.id == type_id, CustomVitalType.user_id == user.id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Custom vital type not found")
    item.active = False
    db.commit()
    return {"status": "removed"}


@vitals_router.get("/custom/{date}")
def get_custom_vitals_for_date(date: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    logs = db.query(CustomVitalLog).filter(CustomVitalLog.user_id == user.id, CustomVitalLog.log_date == date).all()
    return {"values": {l.vital_type_id: l.value for l in logs}}


class CustomVitalValueRequest(BaseModel):
    value: float

@vitals_router.post("/custom/{date}/{type_id}")
def log_custom_vital(date: str, type_id: str, request: CustomVitalValueRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    vital_type = db.query(CustomVitalType).filter(CustomVitalType.id == type_id, CustomVitalType.user_id == user.id).first()
    if not vital_type:
        raise HTTPException(status_code=404, detail="Custom vital type not found")

    existing = db.query(CustomVitalLog).filter(
        CustomVitalLog.user_id == user.id, CustomVitalLog.vital_type_id == type_id, CustomVitalLog.log_date == date
    ).first()
    if existing:
        existing.value = request.value
        db.commit()
        return {"id": existing.id, "value": existing.value}

    log = CustomVitalLog(id=generate_id("cvlog"), user_id=user.id, vital_type_id=type_id, log_date=date, value=request.value)
    db.add(log)
    db.commit()
    return {"id": log.id, "value": request.value}


@vitals_router.get("/{date}")
def get_vitals_for_date(date: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    vital = db.query(VitalLog).filter(VitalLog.user_id == user.id, VitalLog.log_date == date).first()
    if not vital:
        return {"water": 0, "sleep": 0, "energy_level": 3, "mood": 3}
    return {"date": vital.log_date, "water": vital.water, "sleep": vital.sleep, "energy_level": vital.energy_level, "mood": vital.mood}


# ═══════════════════════════════════════════════════════════════════════
# BODY METRICS
# ═══════════════════════════════════════════════════════════════════════

class BodyMetricsRequest(BaseModel):
    date: str
    weight: Optional[float] = None
    body_fat: Optional[float] = None
    measurements: dict = {}  # e.g. {"waist": 33.5, "chest": 40, "arms": 15.2}

@body_router.post("/log")
def log_body_metrics(request: BodyMetricsRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    existing = db.query(BodyMetrics).filter(BodyMetrics.user_id == user.id, BodyMetrics.metric_date == request.date).first()

    if existing:
        existing.weight = request.weight
        existing.body_fat = request.body_fat
        existing.measurements = request.measurements
        db.commit()
        return {"id": existing.id, "status": "updated"}

    entry = BodyMetrics(
        id=generate_id("body"), user_id=user.id, metric_date=request.date,
        weight=request.weight, body_fat=request.body_fat, measurements=request.measurements,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return {"id": entry.id, "status": "created"}


@body_router.get("/latest")
def get_latest_body_metrics(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """
    Most recent entry, plus a real week-over-week delta computed from actual
    logged history — never a hardcoded "-1.2 this week" placeholder.
    """
    user = _resolve_user(db, user_id)
    entries = db.query(BodyMetrics).filter(BodyMetrics.user_id == user.id).order_by(BodyMetrics.metric_date.desc()).limit(30).all()

    if not entries:
        return {"latest": None, "trends": {}}

    latest = entries[0]
    week_ago_cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    older_entries = [e for e in entries if e.metric_date <= week_ago_cutoff]
    baseline = older_entries[0] if older_entries else (entries[-1] if len(entries) > 1 else None)

    def delta(current, base):
        if current is None or base is None:
            return None
        return round(current - base, 1)

    trends = {"weight": None, "body_fat": None, "measurements": {}}
    if baseline:
        trends["weight"] = delta(latest.weight, baseline.weight)
        trends["body_fat"] = delta(latest.body_fat, baseline.body_fat)
        for key in (latest.measurements or {}):
            base_val = (baseline.measurements or {}).get(key)
            trends["measurements"][key] = delta((latest.measurements or {}).get(key), base_val)

    return {
        "latest": {
            "date": latest.metric_date,
            "weight": latest.weight,
            "body_fat": latest.body_fat,
            "measurements": latest.measurements,
        },
        "trends": trends,
    }


# ═══════════════════════════════════════════════════════════════════════
# SUPPLEMENTS — manual entry only, never AI-suggested
# ═══════════════════════════════════════════════════════════════════════

class SupplementCreateRequest(BaseModel):
    name: str
    dosage_amount: Optional[float] = None
    dosage_unit: Optional[str] = None
    timing: str = "Morning"
    frequency: str = "daily"
    notes: Optional[str] = None

@supplements_router.get("")
def list_supplements(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """Real list of the user's own manually-added supplements. Empty if none."""
    user = _resolve_user(db, user_id)
    items = db.query(Supplement).filter(Supplement.user_id == user.id, Supplement.active == True).order_by(Supplement.created_at.asc()).all()
    return {
        "supplements": [
            {
                "id": s.id, "name": s.name, "dosage_amount": s.dosage_amount,
                "dosage_unit": s.dosage_unit, "timing": s.timing, "frequency": s.frequency, "notes": s.notes,
            }
            for s in items
        ]
    }


@supplements_router.post("")
def create_supplement(request: SupplementCreateRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    if not request.name.strip():
        raise HTTPException(status_code=400, detail="Supplement name cannot be empty")

    item = Supplement(
        id=generate_id("supp"), user_id=user.id, name=request.name.strip(),
        dosage_amount=request.dosage_amount, dosage_unit=request.dosage_unit,
        timing=request.timing, frequency=request.frequency, notes=request.notes,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "name": item.name}


@supplements_router.delete("/{supplement_id}")
def delete_supplement(supplement_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """Soft-delete — keeps historical logs intact even after removal from the active stack."""
    user = _resolve_user(db, user_id)
    item = db.query(Supplement).filter(Supplement.id == supplement_id, Supplement.user_id == user.id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Supplement not found")
    item.active = False
    db.commit()
    return {"status": "removed"}


@supplements_router.get("/log/{date}")
def get_supplement_log(date: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """Which supplements were checked off on a given day — real data, no defaults faked as 'done'."""
    user = _resolve_user(db, user_id)
    logs = db.query(SupplementLog).filter(SupplementLog.user_id == user.id, SupplementLog.log_date == date).all()
    taken_ids = {l.supplement_id for l in logs if l.taken}
    return {"taken_supplement_ids": list(taken_ids)}


@supplements_router.post("/log/{date}/{supplement_id}")
def toggle_supplement_taken(date: str, supplement_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """Toggle whether a supplement was taken on a given day."""
    user = _resolve_user(db, user_id)
    existing = db.query(SupplementLog).filter(
        SupplementLog.user_id == user.id, SupplementLog.supplement_id == supplement_id, SupplementLog.log_date == date
    ).first()

    if existing:
        existing.taken = not existing.taken
        db.commit()
        return {"taken": existing.taken}

    log = SupplementLog(id=generate_id("supplog"), user_id=user.id, supplement_id=supplement_id, log_date=date, taken=True)
    db.add(log)
    db.commit()
    return {"taken": True}


# ═══════════════════════════════════════════════════════════════════════
# COMMUNITY — groups, challenges
# ═══════════════════════════════════════════════════════════════════════

class GroupCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    privacy: str = "private"
    group_type: str = "general"  # general, challenge, accountability
    settings: dict = {}

class GroupMessageRequest(BaseModel):
    message: str

class ChallengeCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    category: str
    end_date: str

@community_router.post("/groups/create")
def create_group(request: GroupCreateRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    group = Group(
        id=generate_id("group"), creator_id=user.id, name=request.name,
        description=request.description, members=[user.id], privacy=request.privacy,
        group_type=request.group_type, settings=request.settings,
    )
    db.add(group)
    db.commit()
    return {
        "id": group.id, "name": group.name, "group_type": group.group_type,
        "members": group.members, "privacy": group.privacy,
    }


@community_router.get("/groups")
def list_my_groups(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """Real list of groups the current user belongs to. Empty list if none — never fake data."""
    user = _resolve_user(db, user_id)
    all_groups = db.query(Group).all()
    mine = [g for g in all_groups if user.id in (g.members or [])]
    return {
        "groups": [
            {
                "id": g.id, "name": g.name, "description": g.description,
                "members": len(g.members or []), "group_type": g.group_type,
                "privacy": g.privacy,
            }
            for g in mine
        ]
    }


@community_router.get("/groups/{group_id}")
def get_group(group_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return {
        "id": group.id, "name": group.name, "description": group.description,
        "members": group.members, "privacy": group.privacy,
        "group_type": group.group_type, "settings": group.settings,
    }


@community_router.post("/groups/{group_id}/message")
def send_group_message(group_id: str, request: GroupMessageRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if user.id not in group.members:
        raise HTTPException(status_code=403, detail="Not a group member")

    message = GroupMessage(id=generate_id("msg"), group_id=group_id, sender_id=user.id, message=request.message)
    db.add(message)
    db.commit()
    return {"id": message.id}


@community_router.get("/groups/{group_id}/messages")
def get_group_messages(group_id: str, limit: int = 50, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    messages = db.query(GroupMessage).filter(GroupMessage.group_id == group_id).order_by(GroupMessage.created_at.desc()).limit(limit).all()
    return {"messages": [{"id": m.id, "sender_id": m.sender_id, "message": m.message, "created_at": m.created_at.isoformat()} for m in messages]}


@community_router.post("/challenges/create")
def create_challenge(request: ChallengeCreateRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    challenge = Challenge(
        id=generate_id("challenge"), name=request.name, description=request.description, creator_id=user.id,
        category=request.category, start_date=datetime.utcnow(),
        end_date=datetime.strptime(request.end_date, "%Y-%m-%d"), participants=[user.id],
    )
    db.add(challenge)
    db.commit()
    return {"id": challenge.id, "name": challenge.name}


@community_router.get("/challenges")
def get_challenges(category: str = None, db: Session = Depends(get_db)):
    query = db.query(Challenge).filter(Challenge.end_date > datetime.utcnow())
    if category:
        query = query.filter(Challenge.category == category)
    challenges = query.all()
    return {"challenges": [
        {"id": c.id, "name": c.name, "category": c.category, "participants": len(c.participants), "end_date": c.end_date.isoformat()}
        for c in challenges
    ]}


@community_router.post("/challenges/{challenge_id}/join")
def join_challenge(challenge_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    challenge = db.query(Challenge).filter(Challenge.id == challenge_id).first()
    if not challenge:
        raise HTTPException(status_code=404, detail="Challenge not found")
    if user.id not in challenge.participants:
        challenge.participants.append(user.id)
        db.commit()
    return {"status": "joined"}


# ═══════════════════════════════════════════════════════════════════════
# Q&A
# ═══════════════════════════════════════════════════════════════════════

class QuestionCreateRequest(BaseModel):
    text: str

class AnswerCreateRequest(BaseModel):
    text: str

@community_router.get("/questions")
def list_questions(db: Session = Depends(get_db)):
    """Real questions, sorted by votes. Empty list if none — never fake."""
    questions = db.query(Question).order_by(Question.votes.desc(), Question.created_at.desc()).all()
    result = []
    for q in questions:
        author = db.query(User).filter(User.id == q.user_id).first()
        answer_count = db.query(Answer).filter(Answer.question_id == q.id).count()
        result.append({
            "id": q.id, "text": q.text, "votes": q.votes,
            "author": author.username if author else "unknown",
            "answer_count": answer_count,
            "created_at": q.created_at.isoformat(),
        })
    return {"questions": result}


@community_router.post("/questions")
def create_question(request: QuestionCreateRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
    q = Question(id=generate_id("question"), user_id=user.id, text=request.text.strip())
    db.add(q)
    db.commit()
    db.refresh(q)
    return {"id": q.id, "text": q.text}


@community_router.post("/questions/{question_id}/upvote")
def upvote_question(question_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    q = db.query(Question).filter(Question.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Question not found")
    voters = list(q.voters or [])
    if user.id not in voters:
        voters.append(user.id)
        q.voters = voters
        q.votes += 1
        db.commit()
    return {"votes": q.votes}


@community_router.get("/questions/{question_id}/answers")
def get_answers(question_id: str, db: Session = Depends(get_db)):
    answers = db.query(Answer).filter(Answer.question_id == question_id).order_by(Answer.votes.desc(), Answer.created_at.asc()).all()
    result = []
    for a in answers:
        author = db.query(User).filter(User.id == a.user_id).first()
        result.append({
            "id": a.id, "text": a.text, "votes": a.votes,
            "author": author.username if author else "unknown",
            "created_at": a.created_at.isoformat(),
        })
    return {"answers": result}


@community_router.post("/questions/{question_id}/answers")
def post_answer(question_id: str, request: AnswerCreateRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    question = db.query(Question).filter(Question.id == question_id).first()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Answer cannot be empty")

    a = Answer(id=generate_id("answer"), question_id=question_id, user_id=user.id, text=request.text.strip())
    db.add(a)
    db.commit()
    db.refresh(a)
    return {"id": a.id, "text": a.text}


@community_router.post("/answers/{answer_id}/upvote")
def upvote_answer(answer_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    a = db.query(Answer).filter(Answer.id == answer_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Answer not found")
    voters = list(a.voters or [])
    if user.id not in voters:
        voters.append(user.id)
        a.voters = voters
        a.votes += 1
        db.commit()
    return {"votes": a.votes}


# ═══════════════════════════════════════════════════════════════════════
# HISTORY — unified, filterable, reflectable view across everything logged
# ═══════════════════════════════════════════════════════════════════════
# Rather than a single unified table (which would mean migrating existing
# WorkoutLog/MacroLog/VitalLog/BodyMetrics data), this aggregates across
# the existing tables at query time. Every entry type carries a `reflection`
# field so users can add notes after the fact, regardless of how the entry
# was created (logged live, built-and-completed, or backfilled).

@history_router.get("")
def get_history(
    type: str = None,  # "workout" | "meal" | "vitals" | "body" | None (all)
    start_date: str = None,
    end_date: str = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    user = _resolve_user(db, user_id)
    entries = []

    def in_range(date_str):
        if start_date and date_str < start_date:
            return False
        if end_date and date_str > end_date:
            return False
        return True

    if type is None or type == "workout":
        workouts = db.query(WorkoutLog).filter(WorkoutLog.user_id == user.id).order_by(WorkoutLog.log_date.desc()).all()
        for w in workouts:
            if not in_range(w.log_date):
                continue
            entries.append({
                "id": w.id, "type": "workout", "date": w.log_date,
                "title": w.name, "summary": f"{w.duration} min · {len(w.exercises or [])} exercises",
                "completed": w.completed, "reflection": w.notes,
                "detail": {"duration": w.duration, "exercises": w.exercises, "energy_level": w.energy_level},
            })

    if type is None or type == "meal":
        macros = db.query(MacroLog).filter(MacroLog.user_id == user.id).order_by(MacroLog.log_date.desc()).all()
        for m in macros:
            if not in_range(m.log_date):
                continue
            entries.append({
                "id": m.id, "type": "meal", "date": m.log_date,
                "title": f"{int(m.calories)} kcal logged", "summary": f"P {int(m.protein)}g · C {int(m.carbs)}g · F {int(m.fat)}g",
                "completed": True, "reflection": m.reflection,
                "detail": {"calories": m.calories, "protein": m.protein, "carbs": m.carbs, "fat": m.fat, "meals": m.meals},
            })

    if type is None or type == "vitals":
        vitals = db.query(VitalLog).filter(VitalLog.user_id == user.id).order_by(VitalLog.log_date.desc()).all()
        for v in vitals:
            if not in_range(v.log_date):
                continue
            entries.append({
                "id": v.id, "type": "vitals", "date": v.log_date,
                "title": "Vitals logged", "summary": f"Water {v.water}oz · Sleep {v.sleep}hrs · Mood {v.mood}/5",
                "completed": True, "reflection": v.reflection,
                "detail": {"water": v.water, "sleep": v.sleep, "energy_level": v.energy_level, "mood": v.mood},
            })

    if type is None or type == "body":
        body = db.query(BodyMetrics).filter(BodyMetrics.user_id == user.id).order_by(BodyMetrics.metric_date.desc()).all()
        for b in body:
            if not in_range(b.metric_date):
                continue
            entries.append({
                "id": b.id, "type": "body", "date": b.metric_date,
                "title": "Body metrics logged", "summary": f"{b.weight or '—'}lbs · {b.body_fat or '—'}% body fat",
                "completed": True, "reflection": b.reflection,
                "detail": {"weight": b.weight, "body_fat": b.body_fat, "measurements": b.measurements},
            })

    entries.sort(key=lambda e: e["date"], reverse=True)
    return {"entries": entries[:limit]}


class ReflectionRequest(BaseModel):
    reflection: str

@history_router.post("/{entry_type}/{entry_id}/reflect")
def add_reflection(entry_type: str, entry_id: str, request: ReflectionRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """Attach or update a reflection note on any past entry, regardless of type."""
    user = _resolve_user(db, user_id)

    model_map = {"workout": WorkoutLog, "meal": MacroLog, "vitals": VitalLog, "body": BodyMetrics}
    field_map = {"workout": "notes", "meal": "reflection", "vitals": "reflection", "body": "reflection"}

    if entry_type not in model_map:
        raise HTTPException(status_code=400, detail=f"Unknown entry type: {entry_type}")

    Model = model_map[entry_type]
    entry = db.query(Model).filter(Model.id == entry_id, Model.user_id == user.id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    setattr(entry, field_map[entry_type], request.reflection)
    db.commit()
    return {"status": "saved"}


# ═══════════════════════════════════════════════════════════════════════
# CUSTOM FOOD DATABASE — USDA FoodData Central search + user's own foods
# ═══════════════════════════════════════════════════════════════════════
# USDA requires a free API key (unlike wger). DEMO_KEY works for testing
# but is heavily rate-limited (see config.py). Sign up for a real key at
# https://api.data.gov/signup/ and set USDA_API_KEY in Render env vars.

USDA_BASE = "https://api.nal.usda.gov/fdc/v1"

def _extract_macros_from_usda_food(food: dict) -> dict:
    """USDA returns a flat list of nutrients by name — pull out the 4 we care about."""
    macros = {"calories": 0, "protein": 0, "carbs": 0, "fat": 0}
    name_map = {
        "Energy": "calories",
        "Protein": "protein",
        "Carbohydrate, by difference": "carbs",
        "Total lipid (fat)": "fat",
    }
    for n in food.get("foodNutrients", []):
        nutrient_name = (n.get("nutrientName") or n.get("nutrient", {}).get("name") or "")
        if nutrient_name in name_map:
            macros[name_map[nutrient_name]] = n.get("value") or n.get("amount") or 0
    return macros


@food_router.get("/search")
def search_usda_foods(query: str, limit: int = 15):
    """Search USDA's food database. Values returned are per 100g unless the food specifies otherwise."""
    if not query.strip():
        return {"foods": []}
    try:
        resp = http_requests.get(
            f"{USDA_BASE}/foods/search",
            params={"api_key": settings.usda_api_key, "query": query, "pageSize": limit, "dataType": "Foundation,SR Legacy,Branded"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for f in data.get("foods", []):
            macros = _extract_macros_from_usda_food(f)
            results.append({
                "fdc_id": f.get("fdcId"),
                "name": f.get("description", "Unknown"),
                "brand": f.get("brandOwner"),
                **macros,
            })
        return {"foods": results}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"USDA food search failed: {e}")


@food_router.get("/usda/{fdc_id}")
def get_usda_food_detail(fdc_id: int):
    try:
        resp = http_requests.get(f"{USDA_BASE}/food/{fdc_id}", params={"api_key": settings.usda_api_key}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        macros = _extract_macros_from_usda_food(data)
        return {"fdc_id": fdc_id, "name": data.get("description", "Unknown"), **macros}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"USDA food detail lookup failed: {e}")


class CustomFoodRequest(BaseModel):
    name: str
    calories: float = 0
    protein: float = 0
    carbs: float = 0
    fat: float = 0
    serving_size: Optional[str] = None

@food_router.get("/custom")
def list_custom_foods(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    foods = db.query(CustomFood).filter(CustomFood.user_id == user.id, CustomFood.active == True).order_by(CustomFood.created_at.desc()).all()
    return {
        "foods": [
            {"id": f.id, "name": f.name, "calories": f.calories, "protein": f.protein, "carbs": f.carbs, "fat": f.fat, "serving_size": f.serving_size}
            for f in foods
        ]
    }


@food_router.post("/custom")
def create_custom_food(request: CustomFoodRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    if not request.name.strip():
        raise HTTPException(status_code=400, detail="Food name cannot be empty")
    food = CustomFood(
        id=generate_id("food"), user_id=user.id, name=request.name.strip(),
        calories=request.calories, protein=request.protein, carbs=request.carbs, fat=request.fat,
        serving_size=request.serving_size,
    )
    db.add(food)
    db.commit()
    db.refresh(food)
    return {"id": food.id, "name": food.name}


@food_router.delete("/custom/{food_id}")
def delete_custom_food(food_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    food = db.query(CustomFood).filter(CustomFood.id == food_id, CustomFood.user_id == user.id).first()
    if not food:
        raise HTTPException(status_code=404, detail="Custom food not found")
    food.active = False
    db.commit()
    return {"status": "removed"}


# ═══════════════════════════════════════════════════════════════════════
# MEAL PLANNING — schedule a food for a future date+time, real to-do
# ═══════════════════════════════════════════════════════════════════════

class MealPlanRequest(BaseModel):
    date: str
    meal_time: str = "breakfast"  # breakfast, lunch, dinner, snack
    food_name: str
    calories: float = 0
    protein: float = 0
    carbs: float = 0
    fat: float = 0
    custom_food_id: Optional[str] = None
    usda_fdc_id: Optional[str] = None

@food_router.get("/meal-plans/{date}")
def get_meal_plans_for_date(date: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    plans = db.query(MealPlan).filter(MealPlan.user_id == user.id, MealPlan.plan_date == date).order_by(MealPlan.meal_time).all()
    return {
        "meal_plans": [
            {
                "id": p.id, "meal_time": p.meal_time, "food_name": p.food_name,
                "calories": p.calories, "protein": p.protein, "carbs": p.carbs, "fat": p.fat,
                "completed": p.completed,
            }
            for p in plans
        ]
    }


@food_router.post("/meal-plans")
def create_meal_plan(request: MealPlanRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    plan = MealPlan(
        id=generate_id("mealplan"), user_id=user.id, plan_date=request.date, meal_time=request.meal_time,
        food_name=request.food_name, calories=request.calories, protein=request.protein,
        carbs=request.carbs, fat=request.fat, custom_food_id=request.custom_food_id, usda_fdc_id=request.usda_fdc_id,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return {"id": plan.id, "food_name": plan.food_name}


@food_router.post("/meal-plans/{plan_id}/complete")
def complete_meal_plan(plan_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """
    Marks a planned meal as done AND logs its macros into the real daily
    macro total — completing a plan should count toward actual intake,
    not just flip a checkbox with no effect on tracking.
    """
    user = _resolve_user(db, user_id)
    plan = db.query(MealPlan).filter(MealPlan.id == plan_id, MealPlan.user_id == user.id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Meal plan not found")

    plan.completed = True

    existing = db.query(MacroLog).filter(MacroLog.user_id == user.id, MacroLog.log_date == plan.plan_date).first()
    if existing:
        existing.calories += plan.calories
        existing.protein += plan.protein
        existing.carbs += plan.carbs
        existing.fat += plan.fat
    else:
        existing = MacroLog(
            id=generate_id("macro"), user_id=user.id, log_date=plan.plan_date,
            calories=plan.calories, protein=plan.protein, carbs=plan.carbs, fat=plan.fat,
        )
        db.add(existing)

    db.commit()
    return {"status": "completed", "logged_calories": plan.calories}


@food_router.delete("/meal-plans/{plan_id}")
def delete_meal_plan(plan_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    plan = db.query(MealPlan).filter(MealPlan.id == plan_id, MealPlan.user_id == user.id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Meal plan not found")
    db.delete(plan)
    db.commit()
    return {"status": "deleted"}


# ═══════════════════════════════════════════════════════════════════════
# CALENDAR — aggregates workouts + meal plans + history for a date range
# ═══════════════════════════════════════════════════════════════════════

@food_router.get("/calendar")
def get_calendar(start_date: str, end_date: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """
    One date-indexed view of everything scheduled or logged — workouts,
    meal plans, and completed history — so users can see their whole
    picture for a date range at a glance.
    """
    user = _resolve_user(db, user_id)
    by_date = {}

    def bucket(date_str):
        if date_str not in by_date:
            by_date[date_str] = {"workouts": [], "meal_plans": [], "logged_meals": 0}
        return by_date[date_str]

    workouts = db.query(WorkoutLog).filter(WorkoutLog.user_id == user.id, WorkoutLog.log_date >= start_date, WorkoutLog.log_date <= end_date).all()
    for w in workouts:
        bucket(w.log_date)["workouts"].append({"id": w.id, "name": w.name, "completed": w.completed})

    plans = db.query(MealPlan).filter(MealPlan.user_id == user.id, MealPlan.plan_date >= start_date, MealPlan.plan_date <= end_date).all()
    for p in plans:
        bucket(p.plan_date)["meal_plans"].append({"id": p.id, "food_name": p.food_name, "meal_time": p.meal_time, "completed": p.completed})

    macros = db.query(MacroLog).filter(MacroLog.user_id == user.id, MacroLog.log_date >= start_date, MacroLog.log_date <= end_date).all()
    for m in macros:
        bucket(m.log_date)["logged_meals"] = 1 if m.calories > 0 else 0

    return {"days": by_date}


# ═══════════════════════════════════════════════════════════════════════
# GOAL SETTINGS — real, user-editable calorie/macro/water targets
# ═══════════════════════════════════════════════════════════════════════

class GoalSettingsRequest(BaseModel):
    goal_calories: Optional[int] = None
    goal_protein: Optional[int] = None
    goal_carbs: Optional[int] = None
    goal_fat: Optional[int] = None
    goal_water: Optional[int] = None
    goal_workouts_per_week: Optional[int] = None

@food_router.get("/goals")
def get_goals(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    return {
        "goal_calories": user.goal_calories, "goal_protein": user.goal_protein,
        "goal_carbs": user.goal_carbs, "goal_fat": user.goal_fat,
        "goal_water": user.goal_water, "goal_workouts_per_week": user.goal_workouts_per_week,
    }


@food_router.post("/goals")
def update_goals(request: GoalSettingsRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    if request.goal_calories is not None:
        user.goal_calories = request.goal_calories
    if request.goal_protein is not None:
        user.goal_protein = request.goal_protein
    if request.goal_carbs is not None:
        user.goal_carbs = request.goal_carbs
    if request.goal_fat is not None:
        user.goal_fat = request.goal_fat
    if request.goal_water is not None:
        user.goal_water = request.goal_water
    if request.goal_workouts_per_week is not None:
        user.goal_workouts_per_week = request.goal_workouts_per_week
    db.commit()
    return {
        "goal_calories": user.goal_calories, "goal_protein": user.goal_protein,
        "goal_carbs": user.goal_carbs, "goal_fat": user.goal_fat,
        "goal_water": user.goal_water, "goal_workouts_per_week": user.goal_workouts_per_week,
    }
