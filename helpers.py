import uuid
from datetime import datetime

def generate_id(prefix: str = "id") -> str:
    """Generate a prefixed UUID"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"

def get_today() -> str:
    """Get today's date as YYYY-MM-DD"""
    return datetime.utcnow().strftime("%Y-%m-%d")

def format_iso(dt: datetime) -> str:
    """Format datetime to ISO 8601"""
    return dt.isoformat()

def parse_date(date_str: str) -> datetime:
    """Parse date string YYYY-MM-DD to datetime"""
    return datetime.strptime(date_str, "%Y-%m-%d")
