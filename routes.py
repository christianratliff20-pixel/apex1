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

from database import get_db
from models import (
    FeedItem, User, MacroLog, WorkoutLog, VitalLog,
    Group, GroupMessage, Challenge,
)
from auth import get_current_user_id
from helpers import generate_id
from moderation import run_moderation_pipeline
from config import settings

client = Anthropic()

# One router per feature — main.py mounts each at its own prefix
feed_router = APIRouter()
coach_router = APIRouter()
macros_router = APIRouter()
workouts_router = APIRouter()
vitals_router = APIRouter()
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
        })

    return {"items": result, "total": total, "limit": limit, "offset": offset}


@feed_router.post("/upload")
async def upload_content(
    title: str = Form(...),
    description: str = Form(...),
    category: str = Form(...),
    sub_category: str = Form(...),
    tags: str = Form(""),
    video: UploadFile = File(None),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    user = _resolve_user(db, user_id)

    moderation = await run_moderation_pipeline(title, description, category, sub_category)

    item_id = generate_id("feed")
    moderation_status = "live" if moderation["pass"] else "under_review"

    # TODO: actual Mux upload happens here once you wire MUX_TOKEN_ID/SECRET —
    # for now this creates the feed item without real video processing.
    feed_item = FeedItem(
        id=item_id,
        user_id=user.id,
        type="video",
        title=title,
        description=description,
        category=category,
        workout_type=sub_category if category == "workout" else None,
        food_type=sub_category if category == "food" else None,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        moderation_status=moderation_status,
        moderation_notes=moderation.get("reason", ""),
    )
    db.add(feed_item)
    db.commit()
    db.refresh(feed_item)

    return {"id": item_id, "moderation_status": moderation_status, "message": "Posted!" if moderation["pass"] else "Under review"}


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


# ═══════════════════════════════════════════════════════════════════════
# COACH
# ═══════════════════════════════════════════════════════════════════════

class CoachMessage(BaseModel):
    message: str

@coach_router.post("/message")
def coach_message(request: CoachMessage, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)

    today = datetime.utcnow().strftime("%Y-%m-%d")
    macro_log = db.query(MacroLog).filter(MacroLog.user_id == user.id, MacroLog.log_date == today).first()

    logged = {
        "calories": macro_log.calories if macro_log else 0,
        "protein": macro_log.protein if macro_log else 0,
        "carbs": macro_log.carbs if macro_log else 0,
        "fat": macro_log.fat if macro_log else 0,
    }
    goals = {"calories": user.goal_calories, "protein": user.goal_protein, "carbs": user.goal_carbs, "fat": user.goal_fat}

    system_prompt = f"""You are Apex AI Coach — direct, data-driven, no fluff.
User preference: "{user.coach_personality}"

Current stats:
- Calories: {logged['calories']}/{goals['calories']} (remaining: {goals['calories'] - logged['calories']})
- Protein: {logged['protein']}g (goal: {goals['protein']}g)
- Carbs: {logged['carbs']}g (goal: {goals['carbs']}g)
- Fat: {logged['fat']}g (goal: {goals['fat']}g)

Respond in 2-3 sentences max. Be specific to their data. No generic advice."""

    try:
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1000,
            system=system_prompt,
            messages=[{"role": "user", "content": request.message}],
        )
        return {"response": response.content[0].text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Coach service error: {e}")


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
    exercises: List[Exercise] = []
    energy_level: int = 3
    notes: Optional[str] = None
    completed: bool = False

@workouts_router.post("/log")
def log_workout(request: WorkoutLogRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    workout = WorkoutLog(
        id=generate_id("workout"), user_id=user.id, log_date=request.date, name=request.name,
        duration=request.duration, exercises=[ex.model_dump() for ex in request.exercises],
        energy_level=request.energy_level, notes=request.notes, completed=request.completed,
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


@vitals_router.get("/{date}")
def get_vitals_for_date(date: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    user = _resolve_user(db, user_id)
    vital = db.query(VitalLog).filter(VitalLog.user_id == user.id, VitalLog.log_date == date).first()
    if not vital:
        return {"water": 0, "sleep": 0, "energy_level": 3, "mood": 3}
    return {"date": vital.log_date, "water": vital.water, "sleep": vital.sleep, "energy_level": vital.energy_level, "mood": vital.mood}


# ═══════════════════════════════════════════════════════════════════════
# COMMUNITY — groups, challenges
# ═══════════════════════════════════════════════════════════════════════

class GroupCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    privacy: str = "private"

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
    group = Group(id=generate_id("group"), creator_id=user.id, name=request.name,
                   description=request.description, members=[user.id], privacy=request.privacy)
    db.add(group)
    db.commit()
    return {"id": group.id, "name": group.name}


@community_router.get("/groups/{group_id}")
def get_group(group_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    group = db.query(Group).filter(Group.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return {"id": group.id, "name": group.name, "description": group.description, "members": group.members, "privacy": group.privacy}


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
