"""Security tests: headers on every response, POST rate limiting, 401s."""

import asyncio
import json
import re
from pathlib import Path

from fastapi import HTTPException
from starlette.requests import Request

from app.main import app, unauthorized
from app.security import HitTable, RateLimitMiddleware, client_key, hit_table


def test_security_headers_present(testapp):
    client = testapp["client"]
    r = client.get("/login")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "same-origin"
    assert "camera=()" in r.headers["Permissions-Policy"]
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def _csp(r) -> dict[str, str]:
    return {
        d.split(" ", 1)[0]: d.split(" ", 1)[1] if " " in d else ""
        for d in (p.strip() for p in r.headers["Content-Security-Policy"].split(";"))
    }


def test_csp_uses_a_fresh_nonce_instead_of_unsafe_inline(testapp):
    client = testapp["client"]
    r = client.get("/login")
    csp = _csp(r)
    nonce = re.search(r"'nonce-([^']+)'", csp["script-src"]).group(1)
    assert "unsafe-inline" not in csp["script-src"]
    assert "unsafe-inline" not in csp["style-src"]
    assert "style-src-attr" not in csp
    assert f"'nonce-{nonce}'" in csp["style-src"]
    assert f'nonce="{nonce}"' in r.text  # the page's script carries it
    assert nonce not in client.get("/login").headers["Content-Security-Policy"]


def test_post_rate_limit_429s_then_recovers(testapp):
    client = testapp["client"]
    hit_table(app).clear()
    app.state.rate_limit = (2, 60)
    try:
        url = "/review/00000000-0000-0000-0000-000000000000/answer"
        assert client.post(url, data={"answer": "x"}).status_code == 403  # CSRF, still counts
        assert client.post(url, data={"answer": "x"}).status_code == 403
        r = client.post(url, data={"answer": "x"})
        assert r.status_code == 429
        assert "Retry-After" in r.headers
    finally:
        del app.state.rate_limit
        hit_table(app).clear()
    # default budget restored
    url = "/review/00000000-0000-0000-0000-000000000000/answer"
    assert client.post(url, data={"answer": "x"}).status_code == 403


def test_hit_table_is_a_bounded_lru():
    table = HitTable(max_keys=3)
    for i in range(5):
        assert table.hit(f"ip:{i}", 10, 60, now=float(i)) is None
    table.hit("ip:2", 10, 60, now=5.0)  # touch: now most recent
    table.hit("ip:9", 10, 60, now=6.0)
    assert len(table) == 3
    assert list(table._hits) == ["ip:4", "ip:2", "ip:9"]


def test_hit_table_window_slides():
    table = HitTable()
    assert table.hit("k", 1, 60, now=0.0) is None
    assert table.hit("k", 1, 60, now=30.0) == 30.0  # blocked, 30s to wait
    assert table.hit("k", 1, 60, now=61.0) is None  # old hit aged out


def test_limiter_state_lives_on_the_app_not_the_class():
    assert not hasattr(RateLimitMiddleware, "_instances")
    assert hit_table(app) is hit_table(app)


def _request(session: dict, ip: str = "10.0.0.1", path: str = "/") -> Request:
    return Request({"type": "http", "method": "POST", "path": path, "headers": [],
                    "query_string": b"", "client": (ip, 1234), "session": session})


def test_signed_in_users_behind_one_nat_get_separate_budgets():
    a = client_key(_request({"user_id": "a"}))
    b = client_key(_request({"user_id": "b"}))
    assert a != b
    assert client_key(_request({})) == "ip:10.0.0.1"


def test_api_401_is_json():
    r = asyncio.run(unauthorized(_request({}, path="/api/x"), HTTPException(401)))
    assert r.status_code == 401
    assert r.media_type == "application/json"
    assert json.loads(r.body) == {"detail": "login required"}
    page = asyncio.run(unauthorized(_request({}, path="/courses"), HTTPException(401)))
    assert page.status_code == 303 and page.headers["location"] == "/login"


def test_templates_have_no_inline_style_attributes():
    # The CSP blocks style="" attributes; they'd silently not apply.
    templates = Path(__file__).parent.parent / "templates"
    offenders = [
        f"{path.name}:{n}"
        for path in sorted(templates.glob("*.html"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"\sstyle=", line)
    ]
    assert offenders == []


def _assert_hardened(r):
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_rate_limited_429_carries_security_headers(testapp):
    client = testapp["client"]
    hit_table(app).clear()
    app.state.rate_limit = (1, 60)
    try:
        client.post("/logout")
        r = client.post("/logout")
        assert r.status_code == 429
        _assert_hardened(r)
    finally:
        del app.state.rate_limit
        hit_table(app).clear()


def test_unhandled_error_is_an_html_500_with_security_headers(testapp, caplog):
    from app.main import current_user

    def boom():
        raise RuntimeError("kaboom")

    app.dependency_overrides[current_user] = boom
    r = testapp["client"].get("/stats")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("text/html")
    assert "Something went wrong" in r.text and "kaboom" not in r.text
    _assert_hardened(r)
    assert "kaboom" in caplog.text  # logged, not swallowed


def test_page_errors_are_html(testapp):
    client = testapp["client"]
    for url, status, title in [
        ("/courses/00000000-0000-0000-0000-000000000000", 404, "Not found"),
        ("/no-such-page", 404, "Not found"),
        ("/courses/not-a-uuid", 400, "Bad request"),
    ]:
        r = client.get(url)
        assert r.status_code == status, url
        assert r.headers["content-type"].startswith("text/html"), url
        assert f"<h1 class=\"page-title\">{title}</h1>" in r.text, url
        _assert_hardened(r)
    r = client.post("/settings/google/disconnect", data={"csrf_token": "wrong"})
    assert r.status_code == 403 and "Not allowed" in r.text


def test_api_errors_stay_json(testapp):
    r = testapp["client"].get("/api/no-such-thing")
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}


def test_405_keeps_its_allow_header(testapp):
    r = testapp["client"].put("/login")
    assert r.status_code == 405 and "GET" in r.headers["allow"]
