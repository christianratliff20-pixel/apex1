"""
Celery application setup. This is imported by both:
  - main.py (the web service), to QUEUE jobs
  - worker.py (the background worker), to PROCESS jobs
Both must point at the same Redis instance via REDIS_URL, or they'll
never see each other's queue.
"""
from celery import Celery
from config import settings

celery_app = Celery(
    "apex_video",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Video processing can genuinely take minutes for longer/complex edits —
    # don't let Celery kill a task early assuming it's stuck.
    task_time_limit=900,  # 15 min hard limit
    task_soft_time_limit=780,  # 13 min soft limit (raises an exception the task can catch)
)
