from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, ForeignKey, Text, JSON, Enum as SQLEnum
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base
import enum

class User(Base):
    __tablename__ = "users"
    
    id = Column(String, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    display_name = Column(String)
    bio = Column(String, nullable=True)
    avatar_url = Column(String, nullable=True)
    
    # Fitness goals
    goal_calories = Column(Integer, default=2400)
    goal_protein = Column(Integer, default=180)
    goal_carbs = Column(Integer, default=240)
    goal_fat = Column(Integer, default=80)
    goal_water = Column(Integer, default=128)
    goal_workouts_per_week = Column(Integer, default=5)
    
    # Coach preference
    coach_personality = Column(String, default="Be direct and data-driven.")
    
    # Subscription
    subscription = Column(String, default="free")  # free, active, performance, creator_pro
    is_creator = Column(Boolean, default=False)
    is_coach = Column(Boolean, default=False)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    feed_items = relationship("FeedItem", back_populates="author")
    macro_logs = relationship("MacroLog", back_populates="user")
    workout_logs = relationship("WorkoutLog", back_populates="user")
    body_metrics = relationship("BodyMetrics", back_populates="user")
    vital_logs = relationship("VitalLog", back_populates="user")
    collections = relationship("Collection", back_populates="user")

class FeedItem(Base):
    __tablename__ = "feed_items"
    
    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    
    type = Column(String)  # video, fact, article, recipe, workout, challenge
    title = Column(String)
    description = Column(Text, nullable=True)
    
    # Content metadata
    category = Column(String)  # workout, food
    workout_type = Column(String, nullable=True)  # Strength, Hypertrophy, Cardio, etc.
    food_type = Column(String, nullable=True)  # High Protein, Meal Prep, etc.
    tags = Column(JSON, default=[])  # ["#HighProtein", "#Under30Mins"]
    
    # Macros (for recipes)
    macros = Column(JSON, nullable=True)  # {"calories": 520, "protein": 47, "carbs": 38, "fat": 18}
    
    # Media
    video_url = Column(String, nullable=True)  # legacy/unused, kept for compatibility
    video_mux_id = Column(String, nullable=True)  # legacy/unused, kept for compatibility
    mux_upload_id = Column(String, nullable=True)  # Mux's temporary upload ID, used to poll status
    mux_asset_id = Column(String, nullable=True)  # Mux's permanent asset ID once transcoding starts
    mux_playback_id = Column(String, nullable=True)  # Mux's playback ID — this is what actually plays the video
    video_status = Column(String, default="none")  # none, waiting, processing, ready, errored
    thumbnail_url = Column(String, nullable=True)
    image_url = Column(String, nullable=True)
    
    # Engagement
    likes = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    saves = Column(Integer, default=0)
    shares = Column(Integer, default=0)
    
    # Moderation
    moderation_status = Column(String, default="live")  # live, under_review, removed
    moderation_notes = Column(Text, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    author = relationship("User", back_populates="feed_items")

class Comment(Base):
    __tablename__ = "comments"

    id = Column(String, primary_key=True, index=True)
    feed_item_id = Column(String, ForeignKey("feed_items.id"), index=True)
    user_id = Column(String, ForeignKey("users.id"))

    text = Column(Text)
    likes = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

class CoachMessage(Base):
    __tablename__ = "coach_messages"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True)

    role = Column(String)  # "user" or "coach"
    text = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

class MacroLog(Base):
    __tablename__ = "macro_logs"
    
    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    
    log_date = Column(String, index=True)  # YYYY-MM-DD
    calories = Column(Float, default=0)
    protein = Column(Float, default=0)
    carbs = Column(Float, default=0)
    fat = Column(Float, default=0)
    
    meals = Column(JSON, default=[])  # List of meal objects with macros
    reflection = Column(Text, nullable=True)  # user's post-hoc notes on how this day/meal felt

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    user = relationship("User", back_populates="macro_logs")

class WorkoutLog(Base):
    __tablename__ = "workout_logs"
    
    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    
    log_date = Column(String, index=True)  # YYYY-MM-DD
    name = Column(String)
    duration = Column(Integer)  # minutes
    activity_type = Column(String, default="strength")  # "strength", "cardio", "running", etc. — not limited to the exercise library
    distance = Column(Float, nullable=True)  # for running/cardio, in miles
    
    exercises = Column(JSON, default=[])  # List of exercise objects
    energy_level = Column(Integer, default=3)  # 1-5
    notes = Column(Text, nullable=True)  # freeform personal note — separate from the structured reflection below
    reflection_answers = Column(JSON, nullable=True)  # {"felt_during": "...", "feel_now": "..."} — structured guided template, filled in after the fact
    completed = Column(Boolean, default=False)
    source = Column(String, default="logged")  # "logged" (backfilled), "live" (done through the app), "template" (from a saved routine)
    template_id = Column(String, ForeignKey("workout_templates.id"), nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    user = relationship("User", back_populates="workout_logs")

class WorkoutTemplate(Base):
    """A saved, reusable workout routine — the 'Build' side. Not itself a log entry."""
    __tablename__ = "workout_templates"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True)

    name = Column(String)
    exercises = Column(JSON, default=[])  # [{"name": "Bench Press", "target_sets": 3, "target_reps": 8, "timer_seconds": null}]
    active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class BodyMetrics(Base):
    __tablename__ = "body_metrics"
    
    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    
    metric_date = Column(String, index=True)  # YYYY-MM-DD
    weight = Column(Float, nullable=True)  # lbs
    body_fat = Column(Float, nullable=True)  # %
    
    measurements = Column(JSON, default={})  # {"waist": 33.5, "chest": 40, "arms": 15.2}
    reflection = Column(Text, nullable=True)  # user's post-hoc notes

    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    user = relationship("User", back_populates="body_metrics")

class VitalLog(Base):
    __tablename__ = "vital_logs"
    
    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    
    log_date = Column(String, index=True)  # YYYY-MM-DD
    water = Column(Float, default=0)  # oz
    sleep = Column(Float, default=0)  # hours
    mood = Column(String, nullable=True)  # word-based: "Great"/"Good"/"Okay"/"Rough"/"Bad" or custom text
    reflection = Column(Text, nullable=True)  # user's post-hoc notes
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    user = relationship("User", back_populates="vital_logs")

class CustomVitalType(Base):
    """A user-defined vital field beyond the built-in water/sleep/energy/mood — e.g. Heart Rate, Blood Pressure."""
    __tablename__ = "custom_vital_types"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True)

    name = Column(String)  # e.g. "Heart Rate"
    unit = Column(String, nullable=True)  # e.g. "bpm"
    active = Column(Boolean, default=True)  # soft-delete, keeps historical logs intact

    created_at = Column(DateTime, default=datetime.utcnow)

class CustomVitalLog(Base):
    """Per-day value for one custom vital type."""
    __tablename__ = "custom_vital_logs"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True)
    vital_type_id = Column(String, ForeignKey("custom_vital_types.id"), index=True)

    log_date = Column(String, index=True)  # YYYY-MM-DD
    value = Column(Float)

    created_at = Column(DateTime, default=datetime.utcnow)

class Supplement(Base):
    """A user-defined supplement in their stack — created manually, never AI-suggested."""
    __tablename__ = "supplements"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True)

    name = Column(String)
    dosage_amount = Column(Float, nullable=True)  # e.g. 5
    dosage_unit = Column(String, nullable=True)  # e.g. "g", "mg", "IU", "capsule"
    timing = Column(String, default="Morning")  # freeform: "Morning", "With lunch", "Before bed", etc.
    frequency = Column(String, default="daily")  # "daily", "weekly", "as_needed"
    notes = Column(Text, nullable=True)
    active = Column(Boolean, default=True)  # soft-delete flag, so history stays intact

    created_at = Column(DateTime, default=datetime.utcnow)

class SupplementLog(Base):
    """Per-day checkoff of whether a given supplement was actually taken."""
    __tablename__ = "supplement_logs"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True)
    supplement_id = Column(String, ForeignKey("supplements.id"), index=True)

    log_date = Column(String, index=True)  # YYYY-MM-DD
    taken = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)

class Collection(Base):
    __tablename__ = "collections"
    
    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    
    name = Column(String)
    items = Column(JSON, default=[])  # List of feed_item_ids
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    user = relationship("User", back_populates="collections")

class Group(Base):
    __tablename__ = "groups"
    
    id = Column(String, primary_key=True, index=True)
    creator_id = Column(String, ForeignKey("users.id"))
    
    name = Column(String)
    description = Column(Text, nullable=True)
    members = Column(JSON, default=[])  # List of user_ids
    privacy = Column(String, default="private")  # private, public

    # Configurable group type — owner picks the shape their group takes.
    # "general" = free-form chat/activity feed
    # "challenge" = built around a shared goal/streak
    # "accountability" = check-in based (daily/weekly)
    group_type = Column(String, default="general")
    settings = Column(JSON, default={})  # type-specific config, e.g. {"checkin_frequency": "daily"}
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class GroupMessage(Base):
    __tablename__ = "group_messages"
    
    id = Column(String, primary_key=True, index=True)
    group_id = Column(String, ForeignKey("groups.id"))
    sender_id = Column(String, ForeignKey("users.id"))
    
    message = Column(Text)
    
    created_at = Column(DateTime, default=datetime.utcnow)

class Challenge(Base):
    __tablename__ = "challenges"
    
    id = Column(String, primary_key=True, index=True)
    name = Column(String)
    description = Column(Text, nullable=True)
    creator_id = Column(String, ForeignKey("users.id"))
    
    category = Column(String)  # Nutrition, Strength, Cardio, etc.
    start_date = Column(DateTime)
    end_date = Column(DateTime)
    
    participants = Column(JSON, default=[])  # List of user_ids
    
    created_at = Column(DateTime, default=datetime.utcnow)

class Question(Base):
    __tablename__ = "questions"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))

    text = Column(Text)
    votes = Column(Integer, default=0)
    voters = Column(JSON, default=[])  # list of user_ids who've upvoted, prevents double-voting

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

class Answer(Base):
    __tablename__ = "answers"

    id = Column(String, primary_key=True, index=True)
    question_id = Column(String, ForeignKey("questions.id"), index=True)
    user_id = Column(String, ForeignKey("users.id"))

    text = Column(Text)
    votes = Column(Integer, default=0)
    voters = Column(JSON, default=[])

    created_at = Column(DateTime, default=datetime.utcnow)

class CustomFood(Base):
    """A user's own saved food entry — separate from external USDA lookups."""
    __tablename__ = "custom_foods"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True)

    name = Column(String)
    calories = Column(Float, default=0)
    protein = Column(Float, default=0)
    carbs = Column(Float, default=0)
    fat = Column(Float, default=0)
    serving_size = Column(String, nullable=True)  # e.g. "1 cup", "100g"
    active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)

class FoodSearchHistory(Base):
    """
    Auto-populated: any USDA food a user selects gets remembered here so
    they never have to re-search it. Separate from CustomFood, which is
    only for foods the user manually typed in themselves.
    """
    __tablename__ = "food_search_history"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True)

    name = Column(String)
    calories = Column(Float, default=0)
    protein = Column(Float, default=0)
    carbs = Column(Float, default=0)
    fat = Column(Float, default=0)
    usda_fdc_id = Column(String, nullable=True)

    use_count = Column(Integer, default=1)  # bump each time it's reselected, so frequently-used foods surface first
    last_used_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)

class MealPlan(Base):
    """A meal scheduled for a future date+time — a real to-do, not just a log of what happened."""
    __tablename__ = "meal_plans"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True)

    plan_date = Column(String, index=True)  # YYYY-MM-DD
    meal_time = Column(String, default="breakfast")  # "breakfast", "lunch", "dinner", "snack"

    food_name = Column(String)
    calories = Column(Float, default=0)
    protein = Column(Float, default=0)
    carbs = Column(Float, default=0)
    fat = Column(Float, default=0)

    # Optional link back to the source, so re-logging is one click
    custom_food_id = Column(String, ForeignKey("custom_foods.id"), nullable=True)
    usda_fdc_id = Column(String, nullable=True)  # external USDA food ID, if sourced from there

    completed = Column(Boolean, default=False)  # checked off once actually eaten

    created_at = Column(DateTime, default=datetime.utcnow)

class CoachTake(Base):
    """
    A pre-generated 'coach's take' card for the Overview page — a short,
    real-data-driven observation from Vex. Generated in small batches so
    dismissing one is instant (no live API call/lag), not generated fresh
    on every tap. When a user's unseen queue runs low, a new batch gets
    generated in the background.
    """
    __tablename__ = "coach_takes"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True)

    text = Column(Text)
    acknowledged = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

class VideoEditJob(Base):
    """
    A queued/processing/completed video edit. The `edit_spec` JSON holds the
    full requested edit (clips, trim points, text overlays, filters, music,
    transitions) as a structured description — the Celery worker reads this
    and builds the actual ffmpeg command from it. Status is polled by the
    frontend the same way Mux upload status already is.
    """
    __tablename__ = "video_edit_jobs"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True)

    status = Column(String, default="queued")  # queued, processing, done, failed
    edit_spec = Column(JSON)  # the full requested edit, see EditSpec shape in routes.py
    error_message = Column(Text, nullable=True)

    # Result — populated once status == "done". Uploaded to Mux same as any
    # other video, so playback reuses the existing Mux player pipeline.
    result_mux_upload_id = Column(String, nullable=True)
    result_mux_asset_id = Column(String, nullable=True)
    result_mux_playback_id = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
