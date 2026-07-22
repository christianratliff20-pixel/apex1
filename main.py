from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from auth import router as auth_router
from routes import feed_router, coach_router, macros_router, workouts_router, vitals_router, community_router, body_router, supplements_router, history_router, food_router, video_router
from database import init_db
from config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Initializing database...")
    init_db()
    print("Database ready.")
    yield
    print("Shutting down...")


app = FastAPI(
    title="Apex API",
    description="Fitness + Health Social Platform API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to your actual frontend domain once live
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api/auth", tags=["Auth"])
app.include_router(feed_router, prefix="/api/feed", tags=["Feed"])
app.include_router(coach_router, prefix="/api/coach", tags=["Coach"])
app.include_router(macros_router, prefix="/api/macros", tags=["Macros"])
app.include_router(workouts_router, prefix="/api/workouts", tags=["Workouts"])
app.include_router(vitals_router, prefix="/api/vitals", tags=["Vitals"])
app.include_router(body_router, prefix="/api/body", tags=["Body"])
app.include_router(supplements_router, prefix="/api/supplements", tags=["Supplements"])
app.include_router(history_router, prefix="/api/history", tags=["History"])
app.include_router(food_router, prefix="/api/food", tags=["Food"])
app.include_router(video_router, prefix="/api/video", tags=["Video Editing"])
app.include_router(community_router, prefix="/api/community", tags=["Community"])


@app.get("/")
def root():
    return {"message": "Apex API is running", "docs": "/docs"}


@app.get("/health")
def health_check():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
