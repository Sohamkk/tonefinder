"""
Tonefinder — suggest songs that match a photo.

  - accounts that survive restarts: SQLite on disk, or Postgres if
    DATABASE_URL is set
  - photo analysis with no paid key required (see analyzers.py)
  - real Spotify track IDs so the embedded player plays the right song
  - Razorpay checkout with server-side signature verification
"""

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import wraps

import requests
from flask import Flask, g, jsonify, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

import analyzers

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    import razorpay
except ImportError:
    razorpay = None

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if load_dotenv:
    load_dotenv(os.path.join(BASE_DIR, ".env"))


def env(name, default=""):
    """Env values often arrive with stray quotes or whitespace from a
    copy-paste. Strip them — a trailing space in a secret looks exactly
    like a wrong secret."""
    v = os.environ.get(name, default) or ""
    return v.strip().strip('"').strip("'").strip()


app = Flask(__name__)
app.secret_key = env("FLASK_SECRET_KEY") or "dev-only-change-me"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(env("FORCE_HTTPS_COOKIE")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=90),
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
)

SPOTIFY_CLIENT_ID = env("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = env("SPOTIFY_CLIENT_SECRET")
RAZORPAY_KEY_ID = env("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = env("RAZORPAY_KEY_SECRET")
DATABASE_URL = env("DATABASE_URL") or env("POSTGRES_URL")

PLANS = {
    "free":   {"name": "Free",   "amount": 0,     "songs": 3,  "daily_photos": 5, "filters": False, "captions": False},
    "pro":    {"name": "Pro",    "amount": 14900, "songs": 10, "daily_photos": 0, "filters": True,  "captions": True},
    "studio": {"name": "Studio", "amount": 99900, "songs": 10, "daily_photos": 0, "filters": True,  "captions": True},
}

# ---------------------------------------------------------------------------
# Database — SQLite on disk by default, Postgres when DATABASE_URL is set.
# Both keep data across restarts. /tmp is only used as a last resort on a
# read-only filesystem, and the config endpoint warns when that happens.
# ---------------------------------------------------------------------------

USING_POSTGRES = bool(DATABASE_URL and psycopg2)
DB_EPHEMERAL = False
_sqlite_path = None


def sqlite_path():
    global DB_EPHEMERAL, _sqlite_path
    if _sqlite_path:
        return _sqlite_path
    explicit = env("DB_PATH")
    if explicit:
        os.makedirs(os.path.dirname(os.path.abspath(explicit)) or ".", exist_ok=True)
        _sqlite_path = explicit
        return _sqlite_path
    try:  # can we actually write next to app.py?
        probe = os.path.join(BASE_DIR, ".write-probe")
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
        _sqlite_path = os.path.join(BASE_DIR, "tonefinder.db")
    except OSError:
        DB_EPHEMERAL = True
        _sqlite_path = "/tmp/tonefinder.db"
    return _sqlite_path


def connect():
    if USING_POSTGRES:
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    con = sqlite3.connect(sqlite_path(), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")       # survives crashes, allows readers
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def q(sql):
    """One SQL dialect in the code; translate for Postgres at the edge."""
    if USING_POSTGRES:
        sql = sql.replace("?", "%s")
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    return sql


def fetchone(sql, args=()):
    cur = get_db().cursor()
    cur.execute(q(sql), args)
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None


def fetchall(sql, args=()):
    cur = get_db().cursor()
    cur.execute(q(sql), args)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    return rows


def execute(sql, args=()):
    db = get_db()
    cur = db.cursor()
    cur.execute(q(sql), args)
    db.commit()
    cur.close()


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         email TEXT UNIQUE NOT NULL,
         name TEXT,
         password_hash TEXT NOT NULL,
         plan TEXT NOT NULL DEFAULT 'free',
         plan_expires TEXT,
         created_at TEXT NOT NULL,
         last_login TEXT)""",
    """CREATE TABLE IF NOT EXISTS usage (
         user_id INTEGER NOT NULL, day TEXT NOT NULL,
         count INTEGER NOT NULL DEFAULT 0,
         PRIMARY KEY (user_id, day))""",
    """CREATE TABLE IF NOT EXISTS payments (
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         user_id INTEGER NOT NULL, plan TEXT NOT NULL,
         order_id TEXT NOT NULL, payment_id TEXT,
         amount INTEGER NOT NULL, status TEXT NOT NULL,
         created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS analyses (
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         user_id INTEGER NOT NULL, scene TEXT,
         provider TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL)""",
]

_ready = False


def init_db():
    con = connect()
    cur = con.cursor()
    for stmt in SCHEMA:
        cur.execute(q(stmt))
    con.commit()
    cur.close()
    con.close()


@app.before_request
def ensure_db():
    global _ready
    if not _ready:
        init_db()
        _ready = True


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def current_user():
    uid = session.get("uid")
    return fetchone("SELECT * FROM users WHERE id = ?", (uid,)) if uid else None


def user_plan(row):
    if not row:
        return "free"
    plan = row.get("plan") or "free"
    if plan != "free" and row.get("plan_expires"):
        try:
            if datetime.fromisoformat(row["plan_expires"]) < datetime.now(timezone.utc):
                return "free"
        except (ValueError, TypeError):
            return "free"
    return plan if plan in PLANS else "free"


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user():
            return jsonify(ok=False, error="Sign in first.", auth=False), 401
        return fn(*a, **kw)
    return wrapper


def public_user(row):
    plan = user_plan(row)
    p = PLANS[plan]
    return {
        "email": row["email"],
        "name": row.get("name") or row["email"].split("@")[0],
        "plan": plan, "planName": p["name"], "planExpires": row.get("plan_expires"),
        "limits": {"songs": p["songs"], "dailyPhotos": p["daily_photos"],
                   "filters": p["filters"], "captions": p["captions"]},
    }


@app.post("/api/auth/register")
def register():
    d = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip().lower()
    password = d.get("password") or ""
    name = (d.get("name") or "").strip()[:60]

    if not EMAIL_RE.match(email):
        return jsonify(ok=False, error="That email address doesn't look right."), 400
    if len(password) < 8:
        return jsonify(ok=False, error="Use a password of at least 8 characters."), 400
    if fetchone("SELECT id FROM users WHERE email = ?", (email,)):
        return jsonify(ok=False, error="An account with that email already exists. Sign in instead."), 409

    execute("INSERT INTO users (email, name, password_hash, plan, created_at, last_login) "
            "VALUES (?,?,?,?,?,?)",
            (email, name, generate_password_hash(password), "free", now_iso(), now_iso()))
    row = fetchone("SELECT * FROM users WHERE email = ?", (email,))
    session.permanent = True
    session["uid"] = row["id"]
    return jsonify(ok=True, user=public_user(row))


@app.post("/api/auth/login")
def login():
    d = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip().lower()
    row = fetchone("SELECT * FROM users WHERE email = ?", (email,))
    if not row or not check_password_hash(row["password_hash"], d.get("password") or ""):
        return jsonify(ok=False, error="Wrong email or password."), 401
    execute("UPDATE users SET last_login = ? WHERE id = ?", (now_iso(), row["id"]))
    session.permanent = True
    session["uid"] = row["id"]
    return jsonify(ok=True, user=public_user(row))


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def me():
    row = current_user()
    base = {
        "ok": True,
        "plans": [{"id": k, "name": v["name"], "amount": v["amount"]} for k, v in PLANS.items()],
        "analyzers": analyzers.available(),
        "paymentsReady": bool(get_razorpay_client()),
    }
    if not row:
        base["user"] = None
        return jsonify(base)
    base["user"] = public_user(row)
    base["usage"] = usage_today(row["id"])
    return jsonify(base)


def usage_today(uid):
    r = fetchone("SELECT count FROM usage WHERE user_id = ? AND day = ?",
                 (uid, date.today().isoformat()))
    return r["count"] if r else 0


def bump_usage(uid):
    execute("INSERT INTO usage (user_id, day, count) VALUES (?,?,1) "
            "ON CONFLICT (user_id, day) DO UPDATE SET count = usage.count + 1",
            (uid, date.today().isoformat()))


# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------

_token = {"value": None, "expires": 0}


def spotify_token():
    if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        return None
    if _token["value"] and _token["expires"] > time.time() + 30:
        return _token["value"]
    basic = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
    try:
        r = requests.post("https://accounts.spotify.com/api/token",
                          data={"grant_type": "client_credentials"},
                          headers={"Authorization": f"Basic {basic}"}, timeout=12)
        r.raise_for_status()
        body = r.json()
    except Exception as exc:
        app.logger.warning("Spotify token failed: %s", exc)
        return None
    _token["value"] = body.get("access_token")
    _token["expires"] = time.time() + int(body.get("expires_in", 3600))
    return _token["value"]


def spotify_lookup(title, artist):
    token = spotify_token()
    if not token or not title:
        return {}
    headers = {"Authorization": f"Bearer {token}"}
    for query in (f'track:"{title}" artist:"{artist}"'.strip(), f"{title} {artist}".strip()):
        try:
            r = requests.get("https://api.spotify.com/v1/search", headers=headers,
                             params={"q": query, "type": "track", "limit": 1}, timeout=12)
            if r.status_code == 401:
                _token["value"] = None
                return {}
            r.raise_for_status()
            items = r.json().get("tracks", {}).get("items", [])
        except Exception as exc:
            app.logger.warning("Spotify search failed (%s): %s", title, exc)
            return {}
        if items:
            t = items[0]
            return {
                "id": t.get("id"),
                "url": (t.get("external_urls") or {}).get("spotify"),
                "matchedTitle": t.get("name"),
                "matchedArtist": ", ".join(a["name"] for a in t.get("artists", [])),
            }
    return {}


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}


@app.post("/api/analyze")
@login_required
def analyze_route():
    row = current_user()
    limits = PLANS[user_plan(row)]

    if limits["daily_photos"] and usage_today(row["id"]) >= limits["daily_photos"]:
        return jsonify(ok=False, upgrade=True,
                       error=f"The free plan covers {limits['daily_photos']} photos a day. "
                             "Upgrade to Pro for unlimited reads."), 402

    photo = request.files.get("photo")
    if not photo:
        return jsonify(ok=False, error="No photo came through. Pick a file and try again."), 400
    if photo.mimetype not in ALLOWED_TYPES:
        return jsonify(ok=False, error="Use a JPEG, PNG or WebP image."), 400

    raw = photo.read()
    if len(raw) > 5 * 1024 * 1024:
        return jsonify(ok=False, error="That image is over 5 MB after resizing. Try a smaller one."), 400

    count = max(1, min(int(request.form.get("count") or 3), limits["songs"]))
    if limits["filters"]:
        lang = request.form.get("lang") or "any"
        era = request.form.get("era") or "any"
        steer = (request.form.get("steer") or "")[:200]
        floor = int(request.form.get("energy") or 0)
    else:
        lang, era, steer, floor = "any", "any", "", 0
    nonce = int(request.form.get("nonce") or 0)

    try:
        data, provider, tried = analyzers.analyze(
            raw, photo.mimetype, count, lang, era, steer, floor, nonce)
    except analyzers.AnalyzerError as exc:
        return jsonify(ok=False, error=f"Every reader failed: {exc}"), 502

    for s in data.get("songs", []):
        s.update(spotify_lookup(s.get("title"), s.get("artist")))

    data["provider"] = provider
    data["providerLabel"] = analyzers.LABELS.get(provider, provider)
    data["spotifyConfigured"] = bool(spotify_token())

    bump_usage(row["id"])
    execute("INSERT INTO analyses (user_id, scene, provider, payload, created_at) VALUES (?,?,?,?,?)",
            (row["id"], str(data.get("scene", ""))[:300], provider, json.dumps(data), now_iso()))

    return jsonify(ok=True, result=data, usage=usage_today(row["id"]),
                   fellBackFrom=[t["provider"] for t in tried] or None)


@app.post("/api/caption")
@login_required
def caption_route():
    row = current_user()
    if not PLANS[user_plan(row)]["captions"]:
        return jsonify(ok=False, error="Captions are part of Pro.", upgrade=True), 402
    d = request.get_json(silent=True) or {}
    scene = str(d.get("scene") or "")[:400]
    moods = [str(m)[:30] for m in (d.get("moods") or [])][:6]
    prompt = (
        f"Write one Instagram caption for a photo described as: {scene}. "
        f"Mood: {', '.join(moods)}. Keep it under 14 words, no emoji at the start, "
        "plain and human, not salesy. Then give 8 relevant hashtags. Reply with ONLY "
        'this JSON: {"caption":"","hashtags":["",""]}. Never quote song lyrics.'
    )

    have = analyzers.available()
    for name in analyzers.default_order():
        try:
            if name == "gemini" and have["gemini"]:
                model = env("GEMINI_MODEL") or "gemini-2.0-flash"
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    params={"key": env("GOOGLE_API_KEY")},
                    json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                          "generationConfig": {"responseMimeType": "application/json"}},
                    timeout=45)
                r.raise_for_status()
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            elif name == "anthropic" and have["anthropic"]:
                text = analyzers.anthropic_text([{"type": "text", "text": prompt}], 400)
            else:
                continue
            parsed = analyzers.parse_json_loose(text)
            return jsonify(ok=True, caption=parsed.get("caption", ""),
                           hashtags=[str(h) for h in (parsed.get("hashtags") or [])][:10],
                           provider=name)
        except Exception as exc:
            app.logger.info("caption via %s failed: %s", name, exc)

    tags = ["#" + re.sub(r"[^a-z0-9]", "", m.lower()) for m in moods if m]
    tags += ["#photooftheday", "#nowplaying", "#reels", "#mood", "#soundtrack"]
    return jsonify(
        ok=True, provider="local",
        caption=(moods[0].capitalize() + " kind of frame." if moods
                 else "Some frames pick their own soundtrack."),
        hashtags=[t for t in dict.fromkeys(tags) if len(t) > 2][:8])


@app.get("/api/history")
@login_required
def history():
    rows = fetchall("SELECT id, scene, provider, payload, created_at FROM analyses "
                    "WHERE user_id = ? ORDER BY id DESC LIMIT 20", (current_user()["id"],))
    return jsonify(ok=True, items=[{"id": r["id"], "scene": r["scene"],
                                    "provider": r["provider"], "createdAt": r["created_at"],
                                    "result": json.loads(r["payload"])} for r in rows])


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

def razorpay_blocker():
    """Why payments can't run right now — or None when they can."""
    if razorpay is None:
        return "The razorpay package isn't installed. Run: pip install -r requirements.txt"
    if not RAZORPAY_KEY_ID:
        return "RAZORPAY_KEY_ID isn't set in .env."
    if not RAZORPAY_KEY_SECRET:
        return "RAZORPAY_KEY_SECRET isn't set in .env."
    return None


def get_razorpay_client():
    if razorpay_blocker():
        return None
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


def verify_signature(order_id, payment_id, signature):
    if not RAZORPAY_KEY_SECRET:
        return False
    expected = hmac.new(RAZORPAY_KEY_SECRET.encode(),
                        f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def razorpay_hint(exc):
    """Turn Razorpay's terse errors into something you can act on."""
    text = str(exc)
    if "Authentication failed" in text or "401" in text:
        return ("Razorpay rejected the key pair. The key id and the secret must come from "
                "the SAME generated pair and the same mode — a test key id with an old or "
                "live secret fails exactly like this, and a regenerated key silently kills "
                "the old secret. Fix: Razorpay Dashboard → Account & Settings → API Keys → "
                "Regenerate Test Key, then copy BOTH values into .env and restart. "
                "Check /api/health/razorpay afterwards.")
    if "amount" in text.lower():
        return "Razorpay rejected the amount. It must be a whole number of paise, at least 100."
    return text


@app.post("/api/subscribe")
@login_required
def subscribe():
    row = current_user()
    plan_id = (request.get_json(silent=True) or {}).get("plan")
    if plan_id not in PLANS:
        return jsonify(ok=False, error="Unknown plan."), 400
    plan = PLANS[plan_id]

    if plan["amount"] == 0:
        execute("UPDATE users SET plan = 'free', plan_expires = NULL WHERE id = ?", (row["id"],))
        return jsonify(ok=True, paid=False, user=public_user(current_user()))

    client = get_razorpay_client()
    if not client:
        return jsonify(ok=False, error="Payments aren't ready: " + razorpay_blocker()), 503
    try:
        order = client.order.create({
            "amount": plan["amount"], "currency": "INR",
            "receipt": f"tf-{plan_id}-{uuid.uuid4().hex[:12]}",
            "notes": {"plan": plan_id, "email": row["email"]},
        })
    except Exception as exc:
        app.logger.exception("razorpay order failed")
        return jsonify(ok=False, error=razorpay_hint(exc)), 502

    execute("INSERT INTO payments (user_id, plan, order_id, amount, status, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (row["id"], plan_id, order["id"], plan["amount"], "created", now_iso()))

    return jsonify(ok=True, paid=True, order_id=order["id"], amount=plan["amount"],
                   currency="INR", key_id=RAZORPAY_KEY_ID, plan=plan_id,
                   plan_name=plan["name"],
                   prefill={"email": row["email"], "name": row.get("name") or ""})


@app.post("/api/verify-payment")
@login_required
def verify_payment():
    row = current_user()
    d = request.get_json(silent=True) or {}
    order_id, payment_id = d.get("razorpay_order_id"), d.get("razorpay_payment_id")
    signature = d.get("razorpay_signature")
    if not (order_id and payment_id and signature):
        return jsonify(ok=False, error="Payment details were incomplete."), 400

    pay = fetchone("SELECT * FROM payments WHERE order_id = ? AND user_id = ?",
                   (order_id, row["id"]))
    if not pay:
        return jsonify(ok=False, error="That order doesn't belong to this account."), 403
    if not verify_signature(order_id, payment_id, signature):
        execute("UPDATE payments SET status = 'signature_failed', payment_id = ? WHERE id = ?",
                (payment_id, pay["id"]))
        return jsonify(ok=False, error="Payment signature didn't verify. The plan was not changed."), 400

    execute("UPDATE payments SET status = 'paid', payment_id = ? WHERE id = ?",
            (payment_id, pay["id"]))
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    execute("UPDATE users SET plan = ?, plan_expires = ? WHERE id = ?",
            (pay["plan"], expires, row["id"]))
    return jsonify(ok=True, user=public_user(current_user()))


# ---------------------------------------------------------------------------
# Pages and diagnostics
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health/config")
def health_config():
    return jsonify(
        ok=True,
        analyzers=analyzers.available(),
        analyzer_order=analyzers.default_order(),
        spotify_keys_set=bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET),
        spotify_token_ok=bool(spotify_token()),
        razorpay_package_installed=razorpay is not None,
        razorpay_key_id_set=bool(RAZORPAY_KEY_ID),
        razorpay_key_id_prefix=(RAZORPAY_KEY_ID[:12] + "…") if RAZORPAY_KEY_ID else None,
        razorpay_key_secret_len=len(RAZORPAY_KEY_SECRET) or None,
        database="postgres" if USING_POSTGRES else "sqlite",
        database_file=None if USING_POSTGRES else sqlite_path(),
        database_is_temporary=DB_EPHEMERAL,
        accounts=(fetchone("SELECT COUNT(*) AS n FROM users") or {}).get("n", 0),
        warning=("Accounts are being written to /tmp, which this host wipes between "
                 "restarts. Set DATABASE_URL to a Postgres database, or deploy somewhere "
                 "with a persistent disk.") if DB_EPHEMERAL else None,
    )


@app.get("/api/health/razorpay")
def health_razorpay():
    """Creates and immediately abandons a ₹1 order to prove the keys work."""
    client = get_razorpay_client()
    if not client:
        return jsonify(ok=False, error=razorpay_blocker()), 503
    mode = "test" if RAZORPAY_KEY_ID.startswith("rzp_test") else "live"
    try:
        order = client.order.create({"amount": 100, "currency": "INR", "receipt": "health-check"})
        return jsonify(ok=True, mode=mode, order_created=order["id"],
                       message="Keys work. Nothing was charged — this order is never paid.")
    except Exception as exc:
        return jsonify(ok=False, mode=mode, error=str(exc), fix=razorpay_hint(exc)), 502


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error="No such endpoint."), 404
    return render_template("index.html"), 404


@app.errorhandler(413)
def too_large(_e):
    return jsonify(ok=False, error="That upload was too big. Photos are resized in the "
                                   "browser — reload the page and try again."), 413


@app.errorhandler(Exception)
def catch_all(exc):
    app.logger.exception("unhandled error")
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error=f"Server error: {exc}"), 500
    raise exc


if __name__ == "__main__":
    init_db()
    print("  database :", "postgres" if USING_POSTGRES else sqlite_path())
    print("  analyzers:", analyzers.available())
    print("  order    :", analyzers.default_order())
    app.run(debug=bool(env("FLASK_DEBUG")), port=int(env("PORT") or 5000))
