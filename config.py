from pydantic_settings import BaseSettings
import os

class Settings(BaseSettings):
    # Database
    database_url: str = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost/apex")

    # API Keys
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    mux_token_id: str = os.getenv("MUX_TOKEN_ID", "")
    mux_token_secret: str = os.getenv("MUX_TOKEN_SECRET", "")

    # JWT
    jwt_secret: str = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
    jwt_algorithm: str = os.getenv("JWT_ALGORITHM", "HS256")
    jwt_expiration_hours: int = int(os.getenv("JWT_EXPIRATION_HOURS", "24"))

    # ── TEMPORARY DEV PASSKEY ──────────────────────────────────────────────
    # Development-only quick-login bypass. Set DEV_PASSKEY in Render env vars
    # to enable; leave unset/blank to fully disable. Delete this block plus
    # the /api/auth/dev-login route in auth.py when development wraps up —
    # this is intentionally NOT wired into production auth logic anywhere else.
    dev_passkey: str = os.getenv("DEV_PASSKEY", "")
    # ─────────────────────────────────────────────────────────────────────

    # USDA FoodData Central — free food/nutrition database, requires a free
    # API key from https://api.data.gov/signup/. DEMO_KEY works for testing
    # but is heavily rate-limited — replace with a real key for production.
    usda_api_key: str = os.getenv("USDA_API_KEY", "DEMO_KEY")

    # OAuth
    google_client_id: str = os.getenv("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    apple_client_id: str = os.getenv("APPLE_CLIENT_ID", "")
    apple_team_id: str = os.getenv("APPLE_TEAM_ID", "")
    apple_key_id: str = os.getenv("APPLE_KEY_ID", "")
    apple_private_key: str = os.getenv("APPLE_PRIVATE_KEY", "")

    # App
    app_name: str = os.getenv("APP_NAME", "Apex")
    debug: bool = os.getenv("DEBUG", "False").lower() == "true"
    environment: str = os.getenv("ENVIRONMENT", "production")

    class Config:
        env_file = ".env"
        case_sensitive = False

settings = Settings()
