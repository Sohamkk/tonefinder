"""
Tonefinder — suggest songs that match a photo.

Flask backend:
  - email + password accounts, real sessions, login / logout
  - photo analysis through the Anthropic API (Claude vision)
  - real Spotify track IDs through Spotify's Search API, so the
    embedded player actually plays the right song
  - Razorpay checkout with server-side signature verification

Nothing here fakes success. If a key is missing the API says exactly
which one, instead of pretending the call worked.
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

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

try:
    import razorpay
except ImportError:
    razorpay = None  # the payment route reports this clearly instead of crashing


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if load_dotenv:
    load_dotenv(os.path.join(BASE_DIR, ".env"))

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,  # 8 MB request cap
)

# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")

# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------

PLANS = {
    "free": {
        "name": "Free",
        "amount": 0,                 # paise
        "display": "₹0",
        "songs": 3,
        "daily_photos": 5,
        "filters": False,
        "captions": False,
    },
    "pro": {
        "name": "Pro",
        "amount": 14900,
        "display": "₹149 / month",
        "songs": 10,
        "daily_photos": 0,           # 0 = unlimited
        "filters": True,
        "captions": True,
    },
    "studio": {
        "name": "Studio",
        "amount": 99900,
        "display": "₹999 / month",
        "songs": 10,
        "daily_photos": 0,
        "filters": True,
        "captions": True,
    },
}

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def db_path():
    # Vercel and most serverless hosts only allow writes under /tmp.
    if os.environ.get("VERCEL") or os.environ.get("READ_ONLY_FS"):
        return "/tmp/tonefinder.db"
    return os.environ.get("DB_PATH", os.path.join(BASE_DIR, "tonefinder.db"))


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(db_path())
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT UNIQUE NOT NULL,
  name          TEXT,
  password_hash TEXT NOT NULL,
  plan          TEXT NOT NULL DEFAULT 'free',
  plan_expires  TEXT,
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
  user_id INTEGER NOT NULL,
  day     TEXT NOT NULL,
  count   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS payments (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL,
  plan       TEXT NOT NULL,
  order_id   TEXT NOT NULL,
  payment_id TEXT,
  amount     INTEGER NOT NULL,
  status     TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL,
  scene      TEXT,
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def init_db():
    con = sqlite3.connect(db_path())
    con.executescript(SCHEMA)
    con.commit()
    con.close()


_db_ready = False


@app.before_request
def ensure_db():
    global _db_ready
    if not _db_ready:
        init_db()
        _db_ready = True


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return row


def user_plan(row):
    """A paid plan that has expired quietly falls back to free."""
    if not row:
        return "free"
    plan = row["plan"] or "free"
    if plan != "free" and row["plan_expires"]:
        try:
            if datetime.fromisoformat(row["plan_expires"]) < datetime.now(timezone.utc):
                return "free"
        except ValueError:
            return "free"
    return plan if plan in PLANS else "free"


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return jsonify(ok=False, error="Sign in first.", auth=False), 401
        return fn(*args, **kwargs)
    return wrapper


def public_user(row):
    plan = user_plan(row)
    return {
        "email": row["email"],
        "name": row["name"] or row["email"].split("@")[0],
        "plan": plan,
        "planName": PLANS[plan]["name"],
        "planExpires": row["plan_expires"],
        "limits": {
            "songs": PLANS[plan]["songs"],
            "dailyPhotos": PLANS[plan]["daily_photos"],
            "filters": PLANS[plan]["filters"],
            "captions": PLANS[plan]["captions"],
        },
    }


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------

@app.post("/api/auth/register")
def register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    name = (data.get("name") or "").strip()[:60]

    if not EMAIL_RE.match(email):
        return jsonify(ok=False, error="That email address doesn't look right."), 400
    if len(password) < 8:
        return jsonify(ok=False, error="Use a password of at least 8 characters."), 400

    db = get_db()
    if db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        return jsonify(ok=False, error="An account with that email already exists. Sign in instead."), 409

    cur = db.execute(
        "INSERT INTO users (email, name, password_hash, plan, created_at) VALUES (?,?,?,?,?)",
        (email, name, generate_password_hash(password), "free", now_iso()),
    )
    db.commit()
    session.permanent = True
    session["uid"] = cur.lastrowid
    return jsonify(ok=True, user=public_user(current_user()))


@app.post("/api/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    row = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify(ok=False, error="Wrong email or password."), 401

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
    if not row:
        return jsonify(ok=True, user=None, plans=plan_catalogue())
    return jsonify(ok=True, user=public_user(row), plans=plan_catalogue(), usage=usage_today(row["id"]))


def plan_catalogue():
    return [
        {"id": pid, "name": p["name"], "display": p["display"], "amount": p["amount"]}
        for pid, p in PLANS.items()
    ]


def usage_today(user_id):
    row = get_db().execute(
        "SELECT count FROM usage WHERE user_id = ? AND day = ?", (user_id, date.today().isoformat())
    ).fetchone()
    return row["count"] if row else 0


def bump_usage(user_id):
    db = get_db()
    day = date.today().isoformat()
    db.execute(
        "INSERT INTO usage (user_id, day, count) VALUES (?,?,1) "
        "ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1",
        (user_id, day),
    )
    db.commit()


# --------------------------------------------------------------------------
# Spotify — real track IDs so the embedded player is correct
# --------------------------------------------------------------------------

_spotify_token = {"value": None, "expires": 0}


def spotify_token():
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    if _spotify_token["value"] and _spotify_token["expires"] > time.time() + 30:
        return _spotify_token["value"]
    basic = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
    try:
        r = requests.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}"},
            timeout=12,
        )
        r.raise_for_status()
        body = r.json()
    except Exception as exc:
        app.logger.warning("Spotify token failed: %s", exc)
        return None
    _spotify_token["value"] = body.get("access_token")
    _spotify_token["expires"] = time.time() + int(body.get("expires_in", 3600))
    return _spotify_token["value"]


def spotify_lookup(title, artist):
    """Return {id, url, preview, album_art} for the best match, or {}."""
    token = spotify_token()
    if not token or not title:
        return {}
    query = f'track:"{title}"'
    if artist:
        query += f' artist:"{artist}"'
    try:
        r = requests.get(
            "https://api.spotify.com/v1/search",
            params={"q": query, "type": "track", "limit": 1},
            headers={"Authorization": f"Bearer {token}"},
            timeout=12,
        )
        if r.status_code == 401:
            _spotify_token["value"] = None
            return {}
        r.raise_for_status()
        items = r.json().get("tracks", {}).get("items", [])
        if not items:
            # Loosen the query — exact-phrase search misses a lot of film music.
            r = requests.get(
                "https://api.spotify.com/v1/search",
                params={"q": f"{title} {artist}".strip(), "type": "track", "limit": 1},
                headers={"Authorization": f"Bearer {token}"},
                timeout=12,
            )
            r.raise_for_status()
            items = r.json().get("tracks", {}).get("items", [])
        if not items:
            return {}
        t = items[0]
        images = (t.get("album") or {}).get("images") or []
        return {
            "id": t.get("id"),
            "url": (t.get("external_urls") or {}).get("spotify"),
            "matchedTitle": t.get("name"),
            "matchedArtist": ", ".join(a["name"] for a in t.get("artists", [])),
            "albumArt": images[-1]["url"] if images else None,
        }
    except Exception as exc:
        app.logger.warning("Spotify search failed for %s: %s", title, exc)
        return {}


# --------------------------------------------------------------------------
# Claude — read the photo
# --------------------------------------------------------------------------

def build_prompt(count, lang, era, steer, energy_floor):
    lines = [
        "You are the music curator for Tonefinder, an app that suggests songs to pair "
        "with a photograph someone is about to post.",
        "Look at the attached photograph. Read the setting, the light and time of day, the "
        "weather, the colours, the activity and the overall feeling. Do not try to identify "
        "or name any person in it.",
        "",
        f"Then choose {count} songs that would sit well behind this photo as a post or a reel.",
        "Rules for the picks:",
        "- Real, released songs only. Never invent a title or an artist.",
        "- No two songs by the same artist.",
        "- Mix the obvious fit with one less predictable choice.",
        "- Never quote or reproduce any lyrics, in any field.",
    ]
    if lang and lang != "any":
        lines.append(f"- Every song must be {lang}.")
    else:
        lines.append("- A natural mix of languages is fine, including Hindi and other "
                     "Indian-language music alongside English.")
    if era and era != "any":
        lines.append(f"- Prefer songs {era}.")
    if energy_floor:
        lines.append(f"- Keep the energy at or above {energy_floor} out of 100.")
    if steer:
        lines.append(f"- Extra direction from the user, follow it: {steer}")
    lines += [
        "",
        "Reply with ONLY this JSON object and nothing else:",
        '{"scene":"one plain sentence describing what is in the photo",'
        '"timeOfDay":"short phrase","palette":"two or three colour words","energy":0,'
        '"moods":["three to five one-word or two-word moods"],'
        '"songs":[{"title":"","artist":"","year":0,"language":"",'
        '"why":"one short sentence tying this song to something actually visible in the photo"}]}',
        '"energy" is 0-100. No markdown fences, no commentary.',
    ]
    return "\n".join(lines)


def call_claude(prompt, image_b64, media_type, max_tokens=1600):
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "Photo analysis isn't configured on the server yet — set ANTHROPIC_API_KEY in .env."
        )
    content = []
    if image_b64:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": image_b64},
        })
    content.append({"type": "text", "text": prompt})

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        },
        timeout=90,
    )
    if r.status_code >= 400:
        detail = ""
        try:
            detail = r.json().get("error", {}).get("message", "")
        except Exception:
            detail = r.text[:200]
        raise RuntimeError(f"Claude returned {r.status_code}: {detail}")
    blocks = r.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def parse_json_loose(text):
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min([i for i in (text.find("{"), text.find("[")) if i != -1], default=-1)
    end = max(text.rfind("}"), text.rfind("]"))
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("No JSON in the reply.")


ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}


@app.post("/api/analyze")
@login_required
def analyze():
    row = current_user()
    plan = user_plan(row)
    limits = PLANS[plan]

    if limits["daily_photos"] and usage_today(row["id"]) >= limits["daily_photos"]:
        return jsonify(
            ok=False,
            error=f"The free plan covers {limits['daily_photos']} photos a day. Upgrade to Pro for unlimited.",
            upgrade=True,
        ), 402

    photo = request.files.get("photo")
    if not photo:
        return jsonify(ok=False, error="No photo came through. Pick a file and try again."), 400
    media_type = photo.mimetype
    if media_type not in ALLOWED_TYPES:
        return jsonify(ok=False, error="Use a JPEG, PNG or WebP image."), 400

    raw = photo.read()
    if len(raw) > 5 * 1024 * 1024:
        return jsonify(ok=False, error="That image is over 5 MB after resizing. Try a smaller one."), 400

    count = min(int(request.form.get("count") or 3), limits["songs"])
    if limits["filters"]:
        lang = request.form.get("lang") or "any"
        era = request.form.get("era") or "any"
        steer = (request.form.get("steer") or "")[:200]
        energy_floor = int(request.form.get("energy") or 0)
    else:
        lang, era, steer, energy_floor = "any", "any", "", 0

    prompt = build_prompt(count, lang, era, steer, energy_floor)
    try:
        text = call_claude(prompt, base64.b64encode(raw).decode(), media_type)
        data = parse_json_loose(text)
    except RuntimeError as exc:
        return jsonify(ok=False, error=str(exc)), 502
    except Exception as exc:
        app.logger.exception("analyze failed")
        return jsonify(ok=False, error=f"The read didn't finish: {exc}"), 502

    songs = data.get("songs") or []
    for s in songs[:count]:
        s.update(spotify_lookup(s.get("title"), s.get("artist")))
    data["songs"] = songs[:count]
    data["spotifyConfigured"] = bool(spotify_token())

    bump_usage(row["id"])
    db = get_db()
    db.execute(
        "INSERT INTO analyses (user_id, scene, payload, created_at) VALUES (?,?,?,?)",
        (row["id"], str(data.get("scene", ""))[:300], json.dumps(data), now_iso()),
    )
    db.commit()

    return jsonify(ok=True, result=data, usage=usage_today(row["id"]))


@app.post("/api/caption")
@login_required
def caption():
    row = current_user()
    if not PLANS[user_plan(row)]["captions"]:
        return jsonify(ok=False, error="Captions are part of Pro.", upgrade=True), 402
    data = request.get_json(silent=True) or {}
    scene = str(data.get("scene") or "")[:400]
    moods = ", ".join([str(m)[:30] for m in (data.get("moods") or [])][:6])
    prompt = (
        f"Write one Instagram caption for a photo described as: {scene}. Mood: {moods}. "
        "Keep it under 14 words, no emoji at the start, plain and human, not salesy. "
        "Then give 8 relevant hashtags. Reply with ONLY this JSON: "
        '{"caption":"","hashtags":["",""]}. Never quote song lyrics.'
    )
    try:
        parsed = parse_json_loose(call_claude(prompt, None, None, max_tokens=400))
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 502
    return jsonify(ok=True, caption=parsed.get("caption", ""), hashtags=parsed.get("hashtags", [])[:10])


@app.get("/api/history")
@login_required
def history():
    rows = get_db().execute(
        "SELECT id, scene, payload, created_at FROM analyses WHERE user_id = ? "
        "ORDER BY id DESC LIMIT 20",
        (current_user()["id"],),
    ).fetchall()
    return jsonify(ok=True, items=[
        {"id": r["id"], "scene": r["scene"], "createdAt": r["created_at"],
         "result": json.loads(r["payload"])}
        for r in rows
    ])


# --------------------------------------------------------------------------
# Payments — Razorpay
# --------------------------------------------------------------------------

def get_razorpay_client():
    if not razorpay:
        return None
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return None
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


def verify_razorpay_signature(order_id, payment_id, signature):
    if not RAZORPAY_KEY_SECRET:
        return False
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode("utf-8"),
        f"{order_id}|{payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def grant_plan(user_id, plan_id, days=30):
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    db = get_db()
    db.execute("UPDATE users SET plan = ?, plan_expires = ? WHERE id = ?", (plan_id, expires, user_id))
    db.commit()


@app.post("/api/subscribe")
@login_required
def subscribe():
    row = current_user()
    data = request.get_json(silent=True) or {}
    plan_id = data.get("plan")
    if plan_id not in PLANS:
        return jsonify(ok=False, error="Unknown plan."), 400

    plan = PLANS[plan_id]
    if plan["amount"] == 0:
        db = get_db()
        db.execute("UPDATE users SET plan = 'free', plan_expires = NULL WHERE id = ?", (row["id"],))
        db.commit()
        return jsonify(ok=True, paid=False, user=public_user(current_user()))

    client = get_razorpay_client()
    if not client:
        return jsonify(
            ok=False,
            error="Payments aren't configured on the server yet (missing RAZORPAY_KEY_ID / "
                  "RAZORPAY_KEY_SECRET, or the razorpay package isn't installed).",
        ), 503

    receipt = f"tf-{plan_id}-{uuid.uuid4().hex[:12]}"
    try:
        order = client.order.create({
            "amount": plan["amount"],
            "currency": "INR",
            "receipt": receipt,
            "notes": {"plan": plan_id, "email": row["email"]},
        })
    except Exception as exc:
        app.logger.exception("razorpay order failed")
        return jsonify(ok=False, error=f"Razorpay wouldn't create the order: {exc}"), 502

    db = get_db()
    db.execute(
        "INSERT INTO payments (user_id, plan, order_id, amount, status, created_at) VALUES (?,?,?,?,?,?)",
        (row["id"], plan_id, order["id"], plan["amount"], "created", now_iso()),
    )
    db.commit()

    return jsonify(
        ok=True,
        paid=True,
        order_id=order["id"],
        amount=plan["amount"],
        currency="INR",
        key_id=RAZORPAY_KEY_ID,
        plan=plan_id,
        plan_name=plan["name"],
        prefill={"email": row["email"], "name": row["name"] or ""},
    )


@app.post("/api/verify-payment")
@login_required
def verify_payment():
    row = current_user()
    data = request.get_json(silent=True) or {}
    order_id = data.get("razorpay_order_id")
    payment_id = data.get("razorpay_payment_id")
    signature = data.get("razorpay_signature")

    if not (order_id and payment_id and signature):
        return jsonify(ok=False, error="Payment details were incomplete."), 400

    db = get_db()
    pay = db.execute(
        "SELECT * FROM payments WHERE order_id = ? AND user_id = ?", (order_id, row["id"])
    ).fetchone()
    if not pay:
        return jsonify(ok=False, error="That order doesn't belong to this account."), 403

    if not verify_razorpay_signature(order_id, payment_id, signature):
        db.execute("UPDATE payments SET status = 'signature_failed', payment_id = ? WHERE id = ?",
                   (payment_id, pay["id"]))
        db.commit()
        return jsonify(ok=False, error="Payment signature didn't verify. The plan was not changed."), 400

    db.execute("UPDATE payments SET status = 'paid', payment_id = ? WHERE id = ?", (payment_id, pay["id"]))
    db.commit()
    grant_plan(row["id"], pay["plan"])
    return jsonify(ok=True, user=public_user(current_user()))


# --------------------------------------------------------------------------
# Pages, diagnostics, errors
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html", razorpay_key_id=RAZORPAY_KEY_ID)


@app.get("/api/health/config")
def health_config():
    """Tells you which keys are missing, without ever printing a secret."""
    return jsonify(
        ok=True,
        anthropic_key_set=bool(ANTHROPIC_API_KEY),
        anthropic_model=ANTHROPIC_MODEL,
        spotify_keys_set=bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET),
        spotify_token_ok=bool(spotify_token()),
        razorpay_package_installed=razorpay is not None,
        razorpay_key_id_set=bool(RAZORPAY_KEY_ID),
        razorpay_key_id_prefix=(RAZORPAY_KEY_ID[:8] + "…") if RAZORPAY_KEY_ID else None,
        razorpay_key_secret_set=bool(RAZORPAY_KEY_SECRET),
        db_path=db_path(),
    )


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error="No such endpoint."), 404
    return render_template("index.html", razorpay_key_id=RAZORPAY_KEY_ID), 404


@app.errorhandler(413)
def too_large(_e):
    return jsonify(ok=False, error="That upload was too big. Photos are resized in the browser — reload and retry."), 413


@app.errorhandler(Exception)
def catch_all(exc):
    app.logger.exception("unhandled error")
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error=f"Server error: {exc}"), 500
    raise exc


if __name__ == "__main__":
    init_db()
    app.run(debug=bool(os.environ.get("FLASK_DEBUG")), port=int(os.environ.get("PORT", 5000)))
