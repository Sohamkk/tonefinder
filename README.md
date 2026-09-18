# Tonefinder

Upload a photo, get songs that match it. Flask backend, Claude vision for the
photo read, Spotify's Search API for real track IDs (so the embedded player
plays the right song), Razorpay for paid plans, email + password accounts with
login and logout.

## What's actually real right now

- ✅ **Accounts** — register, log in, log out. Passwords hashed with Werkzeug,
  sessions in a signed cookie, plans stored per user in SQLite.
- ✅ **Photo analysis** — the browser resizes your photo to 1280px, the server
  sends it to the Anthropic API, and Claude returns the scene, time of day,
  palette, mood tags, energy and the song picks as JSON.
- ✅ **Correct Spotify embeds** — every suggested song is looked up through
  Spotify's Search API server-side, so the player gets a real track ID instead
  of a guess. Songs with no match fall back to a search link.
- ✅ **Plan limits enforced on the server** — the free plan is capped at 3 songs
  and 5 photos a day in `app.py`, not in JavaScript, so it can't be bypassed
  from the console.
- ✅ **Razorpay checkout** — `/api/subscribe` creates a real order server-side,
  the frontend opens Razorpay's checkout, and `/api/verify-payment` checks the
  HMAC signature with your secret before the plan changes. The client is never
  trusted to say "payment succeeded".
- ✅ **Colour palette** — pulled out of your photo in the browser and used as
  the site's accent colour.

If a key is missing, the API says which one. Nothing fails silently.

## Keys you need

Your Razorpay test keys are already in `.env` — carried over from FitPulse.
Two more are needed before the app can do anything:

| Key | Where to get it | Needed for |
|---|---|---|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com → API keys | reading the photo |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | https://developer.spotify.com/dashboard → Create app | correct embedded players |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | already set | paid plans |

The Spotify app needs no redirect URI and no user login — Tonefinder uses the
client-credentials flow, which only reads public catalogue data.

Without the Spotify keys everything still works; you just get search links
instead of inline players.

## Run it locally

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5000**, create an account, drop in a photo.

Check what's configured at any time: **http://127.0.0.1:5000/api/health/config**
(it reports which keys are set, never their values).

## Testing payments

Use Razorpay test keys (`rzp_test_…`, which is what's in `.env`) with their
published test card and UPI numbers:
https://razorpay.com/docs/payments/payments/test-card-upi-details/ — no real
money moves. Switch to `rzp_live_…` only once Razorpay approves the account.

## Deploying

**Render / Railway / Fly.io** are the easy path for Flask: push this folder to
GitHub, connect the repo, set every line of `.env` as an environment variable
in the dashboard, done. Start command: `gunicorn app:app` (add
`gunicorn==22.0.0` to requirements) or `python app.py`.

**Vercel** works too — `vercel.json` is included. One catch: Vercel's filesystem
is read-only apart from `/tmp`, so `app.py` puts the SQLite file there, and
**/tmp is wiped between cold starts — accounts will disappear.** For anything
real on Vercel, swap SQLite for Vercel Postgres or Supabase. On Render or
Railway with a persistent disk, the SQLite file survives and you can ship as-is.

Set the same env vars in the dashboard either way. Never commit `.env` —
`.gitignore` already excludes it.

## Project structure

```
tonefinder/
  app.py                 # Flask backend — auth, analysis, Spotify, Razorpay
  templates/index.html   # single page
  static/css/style.css   # photo-lab design system
  static/js/app.js       # upload, colour extraction, checkout
  requirements.txt
  .env                   # your real keys (gitignored)
  .env.example
  vercel.json
```

## API

| Route | What it does |
|---|---|
| `POST /api/auth/register` | create account, starts a session |
| `POST /api/auth/login` | sign in |
| `POST /api/auth/logout` | clear the session |
| `GET /api/me` | current user, plan, limits, today's usage |
| `POST /api/analyze` | multipart `photo` + filters → scene read + songs |
| `POST /api/caption` | Pro — caption and hashtags for the read |
| `GET /api/history` | last 20 reads for this account |
| `POST /api/subscribe` | free plan instantly, or a Razorpay order |
| `POST /api/verify-payment` | verifies the signature, then grants the plan |
| `GET /api/health/config` | which keys are set |

## What I'd build next, in order

1. **Playlist export** — Spotify OAuth (`playlist-modify-public`) so "Save as
   playlist" creates a real playlist from the picks. Biggest user-visible win,
   and the clearest reason to pay.
2. **Carousel mode** — several photos in, one coherent soundtrack out.
3. **Licence-cleared filter** for brand accounts, which can't use the same
   catalogue personal accounts can. Studio-tier feature people will actually pay
   for.
4. **Postgres + a real session store** so deploys don't reset accounts.
5. **Rate limiting** on `/api/analyze` by IP as well as by account — every
   analysis costs you money at the Anthropic API.
