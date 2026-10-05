import os
import warnings
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.dburl import normalize_database_url

INSECURE_SECRET_KEY = "dev-insecure-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://studybuddy:studybuddy@localhost:5432/studybuddy"
    moodle_base_url: str = "https://elearning.strathmore.edu"
    moodle_token: str = ""
    moodle_token_owner: str = ""  # the only email allowed to use MOODLE_TOKEN
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_refresh_token_owner: str = ""  # the only email allowed to use GOOGLE_REFRESH_TOKEN
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_chunk_model: str = "gemini-3.6-flash"
    llm_quiz_model: str = "gemini-3.6-flash"
    llm_grade_model: str = "gemini-3.6-flash"
    # grading runs inside a web request, so it fails fast instead of retrying for minutes
    llm_grade_timeout: int = 30
    llm_grade_attempts: int = 2
    llm_pace: float = 0.0  # seconds between pipeline LLM calls; ~5-45 on free tiers
    app_base_url: str = "http://localhost:8000"  # public origin for links in emails
    resend_api_key: str = ""
    email_from: str = ""
    email_to: str = ""
    secret_key: str = INSECURE_SECRET_KEY
    session_secure_cookie: bool = False
    session_max_age: int = 7 * 24 * 3600  # seconds a sign-in lasts without use
    dev: bool = False  # local development: allows the public default SECRET_KEY
    # comma-separated addresses and/or "@domain" entries; empty = any Google account
    allowed_emails: str = "@strathmore.edu"
    healthcheck_ping_url: str = ""  # e.g. healthchecks.io; the worker pings it every pass
    job_retention_days: int = 30  # finished (completed/failed) jobs older than this are pruned
    timezone: str = "Africa/Nairobi"  # IANA zone whose midnight starts a study day

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def zone(self, name: str | None) -> ZoneInfo:
        """The IANA zone `name`, or the app's TIMEZONE when it's unset or unknown
        (a zone dropped from the system tzdata shouldn't break the user's pages)."""
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                pass
        return self.tz

    @field_validator("database_url")
    @classmethod
    def _psycopg_driver(cls, v: str) -> str:
        return normalize_database_url(v)

    @model_validator(mode="after")
    def _require_known_timezone(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"TIMEZONE {self.timezone!r} is not an IANA zone name") from None
        return self

    @model_validator(mode="after")
    def _require_real_secret(self):
        # SECRET_KEY signs sessions and encrypts stored tokens; the default is
        # public, so only an explicit DEV=1 accepts it, and never on a host
        # that is clearly production (HTTPS cookies, Railway).
        in_prod = self.session_secure_cookie or any(
            os.environ.get(k) for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME")
        )
        if self.secret_key in ("", INSECURE_SECRET_KEY) and (in_prod or not self.dev):
            raise ValueError(
                "SECRET_KEY must be set to a random value (openssl rand -hex 32); "
                "for local development only, set DEV=1 to use the default"
            )
        return self

    @model_validator(mode="after")
    def _warn_unowned_shared_tokens(self):
        # A shared token acts as one real account, so it is only ever used for
        # its named owner; with no owner it is unused (it used to go to everyone).
        for token, owner in (("MOODLE_TOKEN", "MOODLE_TOKEN_OWNER"),
                             ("GOOGLE_REFRESH_TOKEN", "GOOGLE_REFRESH_TOKEN_OWNER")):
            if getattr(self, token.lower()) and not getattr(self, owner.lower()).strip():
                warnings.warn(f"{token} is set but {owner} is empty, so no user will "
                              f"get it; set {owner} to the account it belongs to",
                              stacklevel=2)
        return self


settings = Settings()


def running_commit() -> str:
    """The git commit this process was deployed from. Railway sets it on
    GitHub-triggered deploys; a `railway up` or local run reports "unknown"."""
    return os.environ.get("RAILWAY_GIT_COMMIT_SHA", "").strip() or "unknown"
