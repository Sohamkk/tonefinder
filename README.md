# Tonefinder

Upload a photo, get songs that match it. Flask backend, accounts with login and
logout that survive restarts, Spotify's Search API for correct embedded players,
Razorpay for paid plans — and photo analysis that costs nothing and has no limit.

---

## 1. Run it

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5000**, create an account, drop in a photo, press
Find songs. It works with **zero API keys**. Add keys later to make it better.

Two diagnostic pages tell you the truth about your setup at any moment:

- `/api/health/config` — which readers are live, which database is in use,
  whether it's a temporary one, how many accounts exist
- `/api/health/razorpay` — creates and abandons a ₹1 order to prove your keys
  work, and explains the failure if they don't

---

## 2. The analysis no longer needs your Anthropic balance

`analyzers.py` has four readers. The app tries them in the order set by
`ANALYZER_ORDER` and uses the first that answers. A reader with no key is
skipped silently, and a reader that errors (including "credit balance is too
low") falls through to the next one.

| Reader | Cost | Limit | Reads the actual content? |
|---|---|---|---|
| `local` | free | none | **No** — colour, light, contrast and detail only |
| `gemini` | free tier | daily quota | Yes |
| `openrouter` | free models | rate-limited | Yes |
| `anthropic` | paid | your balance | Yes, best quality |

**`local` is the one that makes the app free and unlimited.** It opens the
image with Pillow, measures brightness, warmth, saturation, contrast, hue and
edge detail, works out time of day / palette / energy / mood from those
numbers, scores ten mood buckets, and picks from the curated library in
`data/songs.json`. No key, no network, no cost, no cap. Be clear-eyed about
what it can't do: it sees a warm, busy, saturated frame — it cannot tell a
wedding from a street market. The picks are mood-accurate, not content-aware.

**Best free setup:** spend two minutes getting a Google AI Studio key at
https://aistudio.google.com/apikey and put it in `.env` as `GOOGLE_API_KEY`.
That gives real scene understanding on a free daily quota, and the moment the
quota runs out the offline reader takes over instead of the app breaking. This
is what `ANALYZER_ORDER=gemini,openrouter,anthropic,local` does by default.

Keep `local` last in the order. It is the floor that stops the app ever
failing.

Your Anthropic key is still supported and still last-but-one in the chain. It
simply isn't needed any more, and an empty balance no longer stops anything.

---

## 3. Accounts now persist

Two things were going wrong before, and both are fixed.

**The database file.** It now lives next to `app.py` as `tonefinder.db`, in WAL
mode. The app probes whether that directory is writable at startup; only on a
read-only host does it fall back to `/tmp`, and when it does,
`/api/health/config` returns a `warning` field saying so in plain words.
Sessions last 90 days, so returning to the site keeps you signed in.

**Serverless hosts wipe `/tmp`.** If you deploy to Vercel, set `DATABASE_URL`
to a Postgres connection string (Neon and Supabase both have free tiers) and
the app uses Postgres instead — `psycopg2-binary` is already in
`requirements.txt`. Everything else is identical; the SQL is translated at the
edge in `q()`. On Render or Railway with a normal disk, SQLite is fine as-is
and you can leave `DATABASE_URL` blank.

Whichever you pick, `/api/health/config` reports `database`,
`database_file`, `database_is_temporary` and a live account count, so you can
confirm signups are landing somewhere real.

---

## 4. Fixing "Razorpay wouldn't create the order: Authentication failed"

This one I can't fix from code, and I want to be straight with you about why.
That error is Razorpay saying the credentials themselves are rejected. Your key
id and secret are both structurally correct — right length, right `rzp_test_`
prefix, no stray quotes or spaces (the app now strips those anyway). So the
pair itself is the problem, and there are only three causes:

1. **The secret belongs to an older key.** Regenerating a key in the Razorpay
   dashboard silently invalidates the previous secret. If the key id and secret
   were copied at different times, they will never work together.
2. **Mode mismatch** — a `rzp_test_` id with a live secret, or the reverse.
3. **The account's test keys were reset** after the FitPulse `.env` was written.

**The fix, once:** Razorpay Dashboard → Account & Settings → API Keys →
Regenerate Test Key. Copy **both** values in that same moment into `.env`.
Restart the server. Open `/api/health/razorpay` — it will either say the keys
work, or print Razorpay's exact reason.

The app now handles this properly rather than dumping a raw error: it names the
specific blocker (package missing vs key missing vs key rejected) and, on an
authentication failure, prints the regenerate-both-keys instruction into the
response so you never have to guess again.

Test payments with Razorpay's published test cards:
https://razorpay.com/docs/payments/payments/test-card-upi-details/ — no real
money moves on `rzp_test_` keys.

---

## 5. Spotify — the embedded player

Without Spotify keys you get search links. With them you get a real inline
player on every track, because the server looks each suggestion up through
Spotify's Search API and uses the returned track ID instead of guessing.

https://developer.spotify.com/dashboard → Create app → copy the Client ID and
Client Secret into `.env`. No redirect URI needed, no user login — Tonefinder
uses the client-credentials flow, which only reads public catalogue data. Free,
and the quota is far beyond anything a small app will hit.

---

## 6. Plans

Enforced server-side in `app.py`, not in JavaScript, so nobody unlocks Pro from
the browser console.

| | Free | Pro ₹149/mo | Studio ₹999/mo |
|---|---|---|---|
| Songs per photo | 3 | 10 | 10 |
| Photos per day | 5 | unlimited | unlimited |
| Language / era / energy filters | — | ✅ | ✅ |
| Caption and hashtag writer | — | ✅ | ✅ |
| Saved history | ✅ | ✅ | ✅ |
| Download the list | — | ✅ | ✅ |

Change any of it in the `PLANS` dict at the top of `app.py`. Amounts are in
paise.

---

## 7. Deploying

**Render or Railway** (easiest): push to GitHub, connect the repo, start
command `gunicorn app:app`, and paste every line of `.env` into the dashboard's
environment variables. Attach a persistent disk and SQLite keeps working.

**Vercel**: `vercel.json` is included, but you **must** set `DATABASE_URL` to a
Postgres URL first or every signup vanishes on the next cold start.

Set `FORCE_HTTPS_COOKIE=1` in production so the session cookie is HTTPS-only.
Never commit `.env` — `.gitignore` already excludes it.

---

## 8. Files

```
tonefinder/
  app.py                 # routes, accounts, plans, Spotify, Razorpay
  analyzers.py           # the four photo readers and the fallback chain
  data/songs.json        # curated library the offline reader picks from
  templates/index.html
  static/css/style.css
  static/js/app.js
  requirements.txt
  .env                   # your real keys (gitignored)
  .env.example
  vercel.json
```

## 9. API

| Route | What it does |
|---|---|
| `POST /api/auth/register` `/login` `/logout` | accounts |
| `GET /api/me` | user, plan, limits, usage, which readers are live |
| `POST /api/analyze` | multipart `photo` → scene read + songs |
| `POST /api/caption` | Pro — caption and hashtags |
| `GET /api/history` | last 20 reads |
| `POST /api/subscribe` → `/api/verify-payment` | Razorpay order, then signature check |
| `GET /api/health/config` `/api/health/razorpay` | diagnostics |

## 10. Worth doing next

1. **Grow `data/songs.json`.** It ships with 80 songs across ten moods. Every
   song you add makes the free reader better, and it costs nothing to run.
2. **Playlist export** — Spotify OAuth so "Save as playlist" creates a real
   playlist. The clearest reason for someone to pay you.
3. **Rate-limit `/api/analyze` by IP**, not just by account, once you add a
   paid reader.
4. **Carousel mode** — several photos, one coherent soundtrack.
