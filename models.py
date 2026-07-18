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
    video_url = Column(String, nullable=True)
    video_mux_id = Column(String, nullable=True)
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
    
    exercises = Column(JSON, default=[])  # List of exercise objects
    energy_level = Column(Integer, default=3)  # 1-5
    notes = Column(Text, nullable=True)
    completed = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    user = relationship("User", back_populates="workout_logs")

class BodyMetrics(Base):
    __tablename__ = "body_metrics"
    
    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"))
    
    metric_date = Column(String, index=True)  # YYYY-MM-DD
    weight = Column(Float, nullable=True)  # lbs
    body_fat = Column(Float, nullable=True)  # %
    
    measurements = Column(JSON, default={})  # {"waist": 33.5, "chest": 40, "arms": 15.2}
    
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
    energy_level = Column(Integer, default=3)  # 1-5
    mood = Column(Integer, default=3)  # 1-5
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    user = relationship("User", back_populates="vital_logs")

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
