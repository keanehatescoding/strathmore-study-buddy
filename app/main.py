import hmac
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
from zoneinfo import available_timezones

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import defer
from sqlmodel import Session, func, select
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app import auth as auth_mod
from app.config import running_commit, settings
from app.db import get_session
from app.grade import (
    MAX_ANSWER_CHARS,
    InvalidAnswer,
    NotDue,
    correct_mcq_index,
    due_count,
    due_items,
    mcq_index,
    submit_answer,
    user_owns_item,
)
from app.llm import LLMClient, LLMError
from app.models import (
    Assignment,
    Chunk,
    Course,
    QuizItem,
    Resource,
    ReviewState,
    Topic,
    User,
)
from app.security import RateLimitMiddleware, SecurityHeadersMiddleware, hit_table
from app.stats import compute_stats

log = logging.getLogger("uvicorn.error")  # uvicorn configures this one


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A lost deploy trigger once left the web service weeks behind unnoticed;
    # the commit in the deploy log (and /version) makes drift visible.
    log.info("starting web at commit %s", running_commit())
    yield


app = FastAPI(title="Strathmore Study Buddy", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

ERROR_MESSAGES = {
    400: ("Bad request", "That link or form wasn't quite right. Go back and try again."),
    403: ("Not allowed", "This page may have been open too long. Reload it and try again."),
    404: ("Not found", "We couldn't find that page. It may have been removed."),
    500: ("Something went wrong", "That's on us, and it has been logged. Try again in a moment."),
}


def error_page(request: Request, status_code: int, headers: dict | None = None):
    """The HTML error page for `status_code` (JSON under /api)."""
    title, message = ERROR_MESSAGES.get(
        status_code, ("Something went wrong", "Go back and try again."))
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": title}, status_code=status_code, headers=headers)
    try:
        return templates.TemplateResponse(
            request, "error.html",
            {"status_code": status_code, "title": title, "message": message},
            status_code=status_code, headers=headers,
        )
    except Exception:  # never let the error page itself fail
        return PlainTextResponse(title, status_code=status_code, headers=headers)


# Middleware added last runs first: sessions, then security headers (so the
# limiter's 429 and the 500 page carry them), then the rate limiter.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    SecurityHeadersMiddleware, error_response=lambda request: error_page(request, 500))
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=settings.session_secure_cookie,
    max_age=settings.session_max_age,
)
static_dir = Path(__file__).parent.parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


def allowed_emails() -> set[str]:
    return {
        e.strip().lower()
        for e in settings.allowed_emails.split(",")
        if e.strip()
    }


def email_allowed(email: str) -> bool:
    """Exact address or "@domain" entry match; an empty allowlist admits anyone."""
    allowed = allowed_emails()
    if not allowed:
        return True
    domain = "@" + email.rpartition("@")[2]
    return email in allowed or domain in allowed


def current_user(
    request: Request, session: Session = Depends(get_session)
) -> User:
    """The signed-in user. A session ends when the user is deleted, their
    sessions are revoked (session_version bumped: logout, admin_cli), or
    their address leaves ALLOWED_EMAILS."""
    user_id = request.session.get("user_id")
    user = session.get(User, UUID(user_id)) if _is_uuid(user_id) else None
    if (user is None
            or request.session.get("session_version") != user.session_version
            or not email_allowed(user.email)):
        request.session.clear()  # so /login doesn't bounce back to /
        raise HTTPException(status_code=401, detail="login required")
    return user


def _is_uuid(value) -> bool:
    try:
        UUID(str(value))
    except ValueError:
        return False
    return True


def owned_course(session: Session, user: User, course_id: UUID) -> Course:
    course = session.get(Course, course_id)
    if course is None or course.user_id != user.id:
        raise HTTPException(404, "course not found")
    return course


def _login_redirect(request: Request):
    return RedirectResponse(url="/login", status_code=303)


def csrf_token(request: Request) -> str:
    """Per-session CSRF token, minted lazily and checked on every POST."""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


templates.env.globals["session_csrf_token"] = csrf_token

# Region/City zones only; the bare aliases ("EST", "Etc/GMT+3") just clutter the picker.
TIMEZONES = sorted({settings.timezone} | {
    z for z in available_timezones()
    if "/" in z and not z.startswith(("Etc/", "SystemV/", "US/", "posix/", "right/"))
})


def local_time(value: datetime, user: User) -> str:
    """A stored UTC instant as wall-clock time in the user's zone."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(settings.zone(user.timezone)).strftime("%Y-%m-%d %H:%M %Z")


templates.env.filters["local_time"] = local_time


async def checked_form(request: Request):
    """Parsed POST form, after verifying its CSRF token."""
    form = await request.form()
    submitted = str(form.get("csrf_token", ""))
    expected = str(request.session.get("csrf_token", ""))
    if not expected or not hmac.compare_digest(submitted, expected):
        raise HTTPException(403, "invalid csrf token")
    return form


def _flash(request: Request, kind: str, text: str) -> None:
    request.session["flash"] = {"kind": kind, "text": text}


@app.exception_handler(401)
async def unauthorized(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": "login required"}, status_code=401)
    return _login_redirect(request)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api"):
        return await http_exception_handler(request, exc)
    return error_page(request, exc.status_code, getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError):
    # e.g. /courses/not-a-uuid: a bad link, not a 422 JSON dump
    if request.url.path.startswith("/api"):
        return await request_validation_exception_handler(request, exc)
    return error_page(request, 400)


@app.get("/health")
def health(session: Session = Depends(get_session)):
    # Platform restart decisions use this: verify Postgres is actually reachable.
    try:
        session.exec(select(User.id)).first()
    except Exception:
        return JSONResponse(
            {"status": "degraded", "db": "unreachable"}, status_code=503
        )
    return {"status": "ok"}


@app.get("/version")
def version():
    # Compare with `git rev-parse origin/master` to spot a stale deploy.
    return {"commit": running_commit()}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"flash": request.session.pop("flash", None)})


@app.get("/login/google")
def login_google(request: Request):
    return _google_redirect(request)


# sign-ins started but not yet returned from Google: a double tap or a second
# tab starts another, and each callback must still find its own state
MAX_PENDING_OAUTH = 5


def _google_redirect(request: Request, consent: bool = False, login_hint: str | None = None):
    state = auth_mod.new_state()
    # state -> whether this round shows the consent screen, so a missing
    # refresh token re-prompts once, not forever; oldest dropped first
    pending = dict(request.session.get("oauth_states") or {})
    pending[state] = consent
    request.session["oauth_states"] = dict(list(pending.items())[-MAX_PENDING_OAUTH:])
    return RedirectResponse(
        auth_mod.login_url(settings.google_client_id, oauth_redirect_uri(), state,
                           consent=consent, login_hint=login_hint),
        status_code=303,
    )


def oauth_redirect_uri() -> str:
    """The callback registered with Google, from APP_BASE_URL rather than the
    request's Host header: a proxy that rewrites Host (or an attacker-chosen
    Host) can't change where Google sends the code."""
    return settings.app_base_url.rstrip("/") + "/auth/callback"


def _login_failed(request: Request, text: str, status_code: int = 303):
    _flash(request, "error", text)
    return RedirectResponse(url="/login", status_code=status_code)


@app.get("/auth/callback")
def auth_callback(
    request: Request, session: Session = Depends(get_session),
    code: str = "", state: str = "", error: str = "",
):
    # each state is good for one callback; the other pending ones stay
    pending = dict(request.session.get("oauth_states") or {})
    known = bool(state) and state in pending
    consented = pending.pop(state, False) if known else False
    request.session["oauth_states"] = pending
    if error:
        # e.g. access_denied: they cancelled on Google's consent screen
        return _login_failed(request, "Google sign-in was cancelled. Try again when you're ready.")
    if not code or not known:
        if request.session.get("user_id"):
            # the other tab's sign-in already finished: this one has nothing to add
            return RedirectResponse(url="/", status_code=303)
        return _login_failed(request, "That sign-in link expired. Please sign in again.")
    redirect_uri = oauth_redirect_uri()
    try:
        tokens = auth_mod.exchange_code(
            settings.google_client_id, settings.google_client_secret, code, redirect_uri
        )
        if not tokens.get("access_token"):
            raise auth_mod.AuthError("token response had no access_token")
        email = auth_mod.fetch_email(tokens["access_token"])
    except auth_mod.AuthError:
        # expired or reused code, Google unreachable, unverified email
        return _login_failed(request, "Google sign-in didn't complete. Please try again.")
    if not email_allowed(email):
        return _login_failed(
            request, f"{email} isn't allowed to sign in here. Use your university account.")
    if (not tokens.get("refresh_token") and not consented
            and not auth_mod.refresh_token_for(auth_mod.find_user(session, email))):
        # Google skips the refresh token without a consent screen, and we
        # have none stored (new user, or theirs was revoked): ask once more
        return _google_redirect(request, consent=True, login_hint=email)
    user = auth_mod.sign_in(session, email, tokens.get("refresh_token"))
    if tokens.get("refresh_token"):
        # a fresh Classroom (+ Drive) grant: pull their classes now
        from app.jobs import enqueue_sync_once

        enqueue_sync_once(session, "classroom", user.email)
    # a fresh session: nothing from before sign-in (CSRF token, flash) carries over
    request.session.clear()
    if pending:
        # another tab's sign-in is still at Google; let it land too
        request.session["oauth_states"] = pending
    request.session["user_id"] = str(user.id)
    request.session["session_version"] = user.session_version
    return RedirectResponse(url="/", status_code=303)


def _revoke_user_sessions(session: Session, user_id, version) -> None:
    user = session.get(User, UUID(user_id)) if _is_uuid(user_id) else None
    # only a live session may revoke: an already-revoked copy of the cookie
    # must not be able to keep signing the user out
    if user is not None and user.session_version == version:
        auth_mod.revoke_sessions(session, user)


@app.post("/logout")
async def logout(request: Request, session: Session = Depends(get_session)):
    await checked_form(request)
    # server-side too: a copied cookie stops working, not just this browser's
    await run_in_threadpool(_revoke_user_sessions, session, request.session.get("user_id"),
                            request.session.get("session_version"))
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def course_list(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    courses = session.exec(
        select(Course)
        .where(Course.user_id == user.id)
        .order_by(Course.name)
    ).all()
    ids = [c.id for c in courses]
    n_topics = dict(session.exec(
        select(Topic.course_id, func.count())
        .where(Topic.course_id.in_(ids))
        .group_by(Topic.course_id)
    ).all())
    n_resources = dict(session.exec(
        select(Topic.course_id, func.count(Resource.id))
        .join(Resource, Resource.topic_id == Topic.id)
        .where(Topic.course_id.in_(ids))
        .group_by(Topic.course_id)
    ).all())
    counts = {
        str(c.id): {"topics": n_topics.get(c.id, 0), "resources": n_resources.get(c.id, 0)}
        for c in courses
    }
    return templates.TemplateResponse(
        request,
        "courses.html",
        {
            "courses": courses,
            "counts": counts,
            "user": user,
            "due_count": due_count(session, user.id),
            "active_page": "courses",
        },
    )


@app.get("/courses/{course_id}", response_class=HTMLResponse)
def course_detail(
    course_id: UUID,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    course = owned_course(session, user, course_id)
    topics = session.exec(
        select(Topic).where(Topic.course_id == course.id).order_by(Topic.order)
    ).all()
    resources_by_topic: dict[str, list] = {str(t.id): [] for t in topics}
    for r in session.exec(
        select(Resource).join(Topic, Topic.id == Resource.topic_id)
        .where(Topic.course_id == course.id).order_by(Resource.title)
        .options(defer(Resource.extracted_text))  # the page lists titles only
    ):
        resources_by_topic[str(r.topic_id)].append(r)
    assignments = session.exec(
        select(Assignment)
        .where(Assignment.course_id == course.id)
        .order_by(Assignment.due_date)
    ).all()
    return templates.TemplateResponse(
        request,
        "course.html",
        {
            "course": course,
            "topics": topics,
            "resources_by_topic": resources_by_topic,
            "assignments": assignments,
            "user": user,
            "active_page": "courses",
        },
    )


# The resource page shows only this much extracted text; Copy fetches the rest.
RESOURCE_PREVIEW_CHARS = 20_000


def _owned_resource(session: Session, user: User, resource_id: UUID):
    resource = session.get(Resource, resource_id)
    topic = session.get(Topic, resource.topic_id) if resource else None
    course = session.get(Course, topic.course_id) if topic else None
    if resource is None or course is None or course.user_id != user.id:
        raise HTTPException(404, "resource not found")
    return resource, topic, course


# Text shown on each side of a highlighted source passage
SOURCE_CONTEXT_CHARS = 400


def source_passage(text: str | None, chunk: Chunk) -> dict:
    """The chunk's span in the resource text with some context around it, or
    the chunk's own content when its offsets are missing or out of range."""
    start, end = chunk.start_char, chunk.end_char
    if (not text or start is None or end is None
            or not 0 <= start < end <= len(text)):
        return {"before": "", "passage": chunk.content, "after": "", "located": False}
    lo = max(0, start - SOURCE_CONTEXT_CHARS)
    hi = min(len(text), end + SOURCE_CONTEXT_CHARS)
    return {
        "before": ("…" if lo else "") + text[lo:start],
        "passage": text[start:end],
        "after": text[end:hi] + ("…" if hi < len(text) else ""),
        "located": True,
    }


@app.get("/resources/{resource_id}", response_class=HTMLResponse)
def resource_detail(
    resource_id: UUID,
    request: Request,
    chunk: UUID | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    resource, topic, course = _owned_resource(session, user, resource_id)
    chunks = session.exec(
        select(Chunk).where(Chunk.resource_id == resource.id).order_by(Chunk.order)
    ).all()
    # ?chunk= from a question's "source" link; a stale or foreign id is ignored
    source = next((c for c in chunks if c.id == chunk), None)
    return templates.TemplateResponse(
        request,
        "resource.html",
        {
            "resource": resource,
            "topic": topic,
            "course": course,
            "chunks": chunks,
            "source": source,
            "passage": source_passage(resource.extracted_text, source) if source else None,
            "preview_chars": RESOURCE_PREVIEW_CHARS,
            "user": user,
            "active_page": "courses",
        },
    )


@app.get("/resources/{resource_id}/text", response_class=PlainTextResponse)
def resource_text(
    resource_id: UUID,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    resource, _, _ = _owned_resource(session, user, resource_id)
    if not resource.extracted_text:
        raise HTTPException(404, "no extracted text")
    return PlainTextResponse(resource.extracted_text)


@app.get("/review", response_class=HTMLResponse)
def review_queue(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "items": due_items(session, user.id),
            "user": user,
            "active_page": "review",
        },
    )


def _take_page(
    request: Request, session: Session, user: User, item: QuizItem,
    status_code: int = 200, **ctx,
):
    # the answered item is no longer due; an unanswered one still counts itself
    remaining = due_count(session, user.id)
    if not ctx.get("result"):
        remaining = max(0, remaining - 1)
    return templates.TemplateResponse(
        request,
        "take.html",
        {
            "item": item,
            "remaining": remaining,
            "result": None,
            "error": None,
            "answer": "",
            "max_answer_chars": MAX_ANSWER_CHARS,
            "csrf_token": csrf_token(request),
            "user": user,
            "active_page": "review",
        } | ctx,
        status_code=status_code,
    )


@app.get("/review/take", response_class=HTMLResponse)
def review_take(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    queue = due_items(session, user.id)
    if not queue:
        return templates.TemplateResponse(
            request, "review.html", {"items": [], "user": user, "active_page": "review"}
        )
    return _take_page(request, session, user, queue[0])


def _result_path(item: QuizItem) -> str:
    # built from the stored item's id via the route table, not from request input
    return app.url_path_for("review_result", item_id=str(item.id))


@app.post("/review/{item_id}/answer", response_class=HTMLResponse)
async def review_answer(
    item_id: UUID,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    form = await checked_form(request)
    answer = str(form.get("answer", ""))
    # the DB work and grading are blocking: keep them off the event loop
    return await run_in_threadpool(_answer_and_redirect, request, session, user, item_id, answer)


def _answer_and_redirect(
    request: Request, session: Session, user: User, item_id: UUID, answer: str,
):
    item = session.get(QuizItem, item_id)
    if item is None or not user_owns_item(session, user.id, item_id):
        raise HTTPException(404, "quiz item not found")
    try:
        llm = None
        if item.question_type == "short_answer":
            # raises LLMError when unconfigured: handled like an outage below
            llm = LLMClient(
                settings.llm_base_url, settings.llm_api_key, settings.llm_grade_model,
                timeout=settings.llm_grade_timeout,
                max_attempts=settings.llm_grade_attempts, max_backoff=5,
            )
        submit_answer(session, user.id, item.id, answer, llm)
    except NotDue:
        # a replayed or double submit: show what was already recorded
        return RedirectResponse(_result_path(item), status_code=303)
    except InvalidAnswer as e:
        return _take_page(request, session, user, item, status_code=400,
                          error=str(e), answer=answer)
    except LLMError:
        # Nothing was recorded; hand the answer back so it isn't lost.
        return _take_page(
            request, session, user, item, status_code=503, answer=answer,
            error="The grader is unavailable right now. Your answer is below — try again shortly.",
        )
    # Post/Redirect/Get: refresh or back can't resubmit the answer
    # only the id: the result itself is read back from ReviewState, since
    # feedback can outgrow the signed session cookie
    request.session["review_result"] = str(item.id)
    return RedirectResponse(_result_path(item), status_code=303)


@app.get("/review/{item_id}/result", response_class=HTMLResponse)
def review_result(
    item_id: UUID,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    item = session.get(QuizItem, item_id)
    state = session.exec(select(ReviewState).where(
        ReviewState.user_id == user.id, ReviewState.quiz_item_id == item_id
    )).first()
    if (request.session.get("review_result") != str(item_id) or item is None
            or state is None or not user_owns_item(session, user.id, item_id)):
        return RedirectResponse("/review/take", status_code=303)
    result = {
        "correct": state.last_result == "correct",
        "verdict": state.last_result,
        "feedback": state.last_feedback or "",
        "interval_days": state.interval_days,
        "answer": state.last_answer,  # NULL on rows answered before 0017
    }
    if item.question_type == "mcq":
        result["chosen"] = mcq_index(state.last_answer, item)
        result["correct_index"] = correct_mcq_index(item)
    # where the question came from: shown only after answering, as the
    # passage gives the answer away
    chunk = session.get(Chunk, item.chunk_id)
    resource = session.get(Resource, chunk.resource_id) if chunk else None
    if resource is not None:
        result["source"] = {"resource": resource, "chunk": chunk}
    return _take_page(request, session, user, item, result=result)


@app.get("/stats", response_class=HTMLResponse)
def stats_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "stats": compute_stats(session, user.id),
            "due": due_count(session, user.id),
            "user": user,
            "active_page": "stats",
        },
    )


@app.get("/settings/moodle", response_class=HTMLResponse)
def moodle_settings(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    from app.moodle_tokens import decrypt_token, token_for

    own = decrypt_token(user.moodle_token) is not None
    google_own = auth_mod.refresh_token_for(user) is not None
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "connected": own,
            "shared": not own and token_for(user) is not None,
            "stale": bool(user.moodle_token) and not own,
            "google_connected": google_own,
            "google_shared": not google_own and auth_mod.classroom_token_for(user) is not None,
            "google_stale": bool(user.google_refresh_token) and not google_own,
            "moodle_url": settings.moodle_base_url,
            "timezones": TIMEZONES,
            "user_timezone": settings.zone(user.timezone).key,
            "notify_email": user.notify_email,
            "flash": request.session.pop("flash", None),
            "csrf_token": csrf_token(request),
            "user": user,
            "due_count": due_count(session, user.id),
            "active_page": "settings",
        },
    )


def _connect_moodle(session: Session, user: User, token: str) -> str:
    """Verify `token`, store it encrypted, queue a first sync; returns the Moodle name."""
    from app.jobs import enqueue
    from app.moodle_tokens import encrypt_token, verify_token

    info = verify_token(settings.moodle_base_url, token)
    user.moodle_token = encrypt_token(token)
    session.add(user)
    session.commit()
    enqueue(session, "sync", {
        "source": "moodle", "course_id": None, "user_email": user.email,
    })
    return str(info.get("fullname") or info.get("username") or "your account")


async def _connect_and_redirect(request, session, user, get_token) -> RedirectResponse:
    from app.moodle import MoodleError

    try:
        token = await run_in_threadpool(get_token)
        name = await run_in_threadpool(_connect_moodle, session, user, token)
    except MoodleError as e:
        _flash(request, "error", f"Moodle said: {e}")
    else:
        _flash(request, "success",
               f"Connected as {name}. Your courses will sync in the next few minutes.")
    return RedirectResponse("/settings/moodle", status_code=303)


MOODLE_LOGIN_LIMIT = (5, 15 * 60)  # attempts per window, per account and per username


def _moodle_login_wait(request: Request, user: User, username: str) -> float | None:
    """Seconds to wait before another Moodle password attempt, else None.

    The form checks passwords against Moodle, so the global POST budget alone
    would let one account guess another student's password 120 times a
    minute. Limit by our account (one user trying many usernames) and by the
    Moodle username (many accounts trying one username)."""
    limit, window = getattr(request.app.state, "moodle_login_limit", MOODLE_LOGIN_LIMIT)
    table = hit_table(request.app, "moodle_login_hits")
    now = time.monotonic()
    waits = [table.hit(key, limit, window, now)
             for key in (f"user:{user.id}", f"moodle:{username.lower()}")]
    waits = [w for w in waits if w is not None]
    return max(waits) if waits else None


@app.post("/settings/moodle/login")
async def moodle_login(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    from app.moodle_tokens import fetch_token

    form = await checked_form(request)
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    if not username or not password:
        _flash(request, "error", "Enter your Moodle username and password.")
        return RedirectResponse("/settings/moodle", status_code=303)
    wait = _moodle_login_wait(request, user, username)
    if wait is not None:
        _flash(request, "error", "Too many Moodle sign-in attempts. Try again in "
               f"{int(wait // 60) + 1} minute(s), or paste your key below instead.")
        return RedirectResponse("/settings/moodle", status_code=303)
    return await _connect_and_redirect(
        request, session, user,
        lambda: fetch_token(settings.moodle_base_url, username, password),
    )


@app.post("/settings/moodle/token")
async def moodle_token(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    form = await checked_form(request)
    token = str(form.get("token", "")).strip()
    if not token:
        _flash(request, "error", "Paste your Moodle mobile web service key.")
        return RedirectResponse("/settings/moodle", status_code=303)
    return await _connect_and_redirect(request, session, user, lambda: token)


def _disconnect_moodle(session: Session, user: User) -> None:
    user.moodle_token = None
    session.add(user)
    session.commit()


@app.post("/settings/moodle/disconnect")
async def moodle_disconnect(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    await checked_form(request)
    await run_in_threadpool(_disconnect_moodle, session, user)
    _flash(request, "success",
           "Disconnected. Already-synced courses stay; new material won't sync.")
    return RedirectResponse("/settings/moodle", status_code=303)


def _save_timezone(session: Session, user: User, name: str) -> None:
    # the app default is stored as NULL, so it follows a later TIMEZONE change
    user.timezone = None if name == settings.timezone else name
    session.add(user)
    session.commit()


def _save_notify_email(session: Session, user: User, on: bool) -> None:
    from app.notify import opt_out

    if on:
        user.notify_email = True
        session.add(user)
        session.commit()
    else:
        opt_out(session, user)


@app.post("/settings/timezone")
async def set_timezone(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    form = await checked_form(request)
    name = str(form.get("timezone", ""))
    if name not in TIMEZONES:
        _flash(request, "error", "Pick a time zone from the list.")
    else:
        await run_in_threadpool(_save_timezone, session, user, name)
        _flash(request, "success", f"Time zone set to {name}. Reviews now fall due at "
               "local midnight.")
    return RedirectResponse("/settings/moodle", status_code=303)


@app.post("/settings/notifications")
async def set_notifications(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    form = await checked_form(request)
    await run_in_threadpool(_save_notify_email, session, user,
                            form.get("notify_email") == "on")
    _flash(request, "success", "Email notifications turned "
           + ("on." if user.notify_email else "off."))
    return RedirectResponse("/settings/moodle", status_code=303)


def _unsubscribe_target(session: Session, token: str) -> User:
    from app.notify import unsubscribe_user

    user = unsubscribe_user(session, token)
    if user is None:
        raise HTTPException(404, "unknown unsubscribe link")
    return user


@app.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_page(request: Request, token: str, session: Session = Depends(get_session)):
    """Confirmation only: mail scanners follow links, so GET changes nothing."""
    user = _unsubscribe_target(session, token)
    return templates.TemplateResponse(
        request, "unsubscribe.html",
        {"done": not user.notify_email, "email": user.email, "token": token})


@app.post("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe(request: Request, token: str, session: Session = Depends(get_session)):
    """One-click unsubscribe (RFC 8058): the signed token is the credential,
    so no session or CSRF token; mail clients POST here directly."""
    from app.notify import opt_out

    user = _unsubscribe_target(session, token)
    opt_out(session, user)  # also when already off: fails anything still queued
    return templates.TemplateResponse(
        request, "unsubscribe.html", {"done": True, "email": user.email, "token": token})


@app.post("/settings/google/disconnect")
async def google_disconnect(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    await checked_form(request)
    if await run_in_threadpool(auth_mod.revoke_google_access, session, user):
        _flash(request, "success", "Google access revoked. Already-synced Classroom "
               "courses stay; sign in again to reconnect.")
    else:
        _flash(request, "error", "We've deleted your Google key, but couldn't reach Google "
               "to revoke it. Remove Study Buddy at myaccount.google.com/permissions.")
    return RedirectResponse("/settings/moodle", status_code=303)
