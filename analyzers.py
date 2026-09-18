"""
Photo analyzers for Tonefinder.

Four back ends, tried in the order set by ANALYZER_ORDER:

  local        no key, no cost, no limit. Reads light, colour, contrast and
               detail out of the pixels with Pillow, works out the mood, and
               picks from a curated library in data/songs.json. It cannot tell
               you *what* is in the photo, only what it looks like.
  gemini       Google AI Studio. Has a free tier with a daily quota and does
               read the actual content of the photo.
  openrouter   Free vision models hosted on OpenRouter (rate-limited, free).
  anthropic    Claude. Best quality, but a paid API balance is required.

Every back end returns the same dict:
  {scene, timeOfDay, palette, energy, moods[], songs[{title,artist,year,language,why}]}
"""

import base64
import colorsys
import hashlib
import json
import os
import random
import re

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SONGS_PATH = os.path.join(BASE_DIR, "data", "songs.json")

try:
    from PIL import Image, ImageFilter, ImageStat
except ImportError:  # the local analyzer needs Pillow; the API ones don't
    Image = None


class AnalyzerError(RuntimeError):
    """A back end could not answer. The caller moves on to the next one."""


# ---------------------------------------------------------------------------
# Shared prompt for the API back ends
# ---------------------------------------------------------------------------

def build_prompt(count, lang="any", era="any", steer="", energy_floor=0):
    lines = [
        "You are the music curator for Tonefinder, an app that suggests songs to pair "
        "with a photograph someone is about to post.",
        "Look at the attached photograph. Read the setting, the light and time of day, "
        "the weather, the colours, the activity and the overall feeling. Do not try to "
        "identify or name any person in it.",
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
        "Reply with ONLY this JSON object and nothing else, no markdown fences:",
        '{"scene":"one plain sentence describing what is in the photo",'
        '"timeOfDay":"short phrase","palette":"two or three colour words","energy":0,'
        '"moods":["three to five one-word or two-word moods"],'
        '"songs":[{"title":"","artist":"","year":0,"language":"",'
        '"why":"one short sentence tying this song to something actually visible in the photo"}]}',
        '"energy" is 0-100.',
    ]
    return "\n".join(lines)


def parse_json_loose(text):
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    start = min(starts) if starts else -1
    end = max(text.rfind("}"), text.rfind("]"))
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise AnalyzerError("The model didn't return JSON.")


def _shape(data, count):
    """Make sure whatever came back has the fields the frontend expects."""
    if not isinstance(data, dict):
        raise AnalyzerError("The model returned the wrong shape.")
    songs = [s for s in (data.get("songs") or []) if isinstance(s, dict) and s.get("title")]
    if not songs:
        raise AnalyzerError("The model returned no songs.")
    seen, clean = set(), []
    for s in songs:
        key = (str(s.get("artist", "")).lower(), str(s.get("title", "")).lower())
        if key in seen:
            continue
        seen.add(key)
        clean.append({
            "title": str(s.get("title", ""))[:120],
            "artist": str(s.get("artist", ""))[:120],
            "year": s.get("year") or "",
            "language": str(s.get("language", ""))[:40],
            "why": str(s.get("why", ""))[:300],
        })
    return {
        "scene": str(data.get("scene", ""))[:400],
        "timeOfDay": str(data.get("timeOfDay", ""))[:60],
        "palette": str(data.get("palette", ""))[:60],
        "energy": max(0, min(100, int(float(data.get("energy") or 50)))),
        "moods": [str(m)[:30] for m in (data.get("moods") or [])][:6],
        "songs": clean[:count],
    }


# ---------------------------------------------------------------------------
# 1. local — free, unlimited, no key
# ---------------------------------------------------------------------------

_library = None


def library():
    global _library
    if _library is None:
        with open(SONGS_PATH, encoding="utf-8") as fh:
            _library = json.load(fh)["buckets"]
    return _library


def photo_features(raw):
    """Pull light, colour and detail statistics out of the image bytes."""
    if Image is None:
        raise AnalyzerError("Pillow isn't installed — run pip install -r requirements.txt.")
    import io
    try:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
    except Exception as exc:
        raise AnalyzerError(f"That image couldn't be opened: {exc}")

    img.thumbnail((360, 360))
    stat = ImageStat.Stat(img)
    r, g, b = stat.mean
    sr, sg, sb = stat.stddev

    grey = img.convert("L")
    brightness = ImageStat.Stat(grey).mean[0]           # 0-255
    contrast = ImageStat.Stat(grey).stddev[0]           # 0-~100

    edges = grey.filter(ImageFilter.FIND_EDGES)
    detail = ImageStat.Stat(edges).mean[0]              # busy-ness of the frame

    small = img.resize((64, 64))
    pixels = list(small.getdata())
    sat_total = hue_bins = 0
    hues = [0.0] * 12
    for (pr, pg, pb) in pixels:
        h, s, v = colorsys.rgb_to_hsv(pr / 255, pg / 255, pb / 255)
        sat_total += s
        if s > 0.18 and v > 0.12:
            hues[int(h * 12) % 12] += 1
            hue_bins += 1
    saturation = sat_total / len(pixels)                # 0-1
    dominant_hue = hues.index(max(hues)) if hue_bins else -1

    warmth = (r - b) / 255.0                            # -1 cool .. +1 warm
    greenness = (g - (r + b) / 2) / 255.0
    blueness = (b - (r + g) / 2) / 255.0

    return {
        "brightness": brightness, "contrast": contrast, "detail": detail,
        "saturation": saturation, "warmth": warmth, "greenness": greenness,
        "blueness": blueness, "dominant_hue": dominant_hue,
        "colour_spread": (sr + sg + sb) / 3,
    }


HUE_WORDS = ["red", "orange", "amber", "yellow", "lime", "green",
             "teal", "cyan", "sky blue", "indigo", "violet", "magenta"]


def describe(f):
    """Turn the numbers into the words the page shows."""
    bright, warm, sat = f["brightness"], f["warmth"], f["saturation"]

    if bright < 55:
        time_of_day = "night or very low light"
    elif bright < 100 and warm > 0.04:
        time_of_day = "late evening, warm light"
    elif warm > 0.10 and bright < 175:
        time_of_day = "golden hour"
    elif bright > 195 and sat < 0.25:
        time_of_day = "flat bright daylight"
    elif bright > 165:
        time_of_day = "open daylight"
    else:
        time_of_day = "overcast or indoor light"

    words = []
    if f["dominant_hue"] >= 0:
        words.append(HUE_WORDS[f["dominant_hue"]])
    words.append("warm" if warm > 0.05 else ("cool" if warm < -0.03 else "neutral"))
    if sat < 0.16:
        words.append("muted")
    elif sat > 0.42:
        words.append("saturated")
    palette = ", ".join(dict.fromkeys(words))

    energy = (
        28
        + min(34, f["saturation"] * 78)
        + min(20, f["contrast"] * 0.24)
        + min(18, f["detail"] * 1.5)
        - (16 if bright < 60 else 0)
    )
    energy = int(max(8, min(97, energy)))

    moods = []
    if bright < 60:
        moods.append("nocturnal")
    if warm > 0.09:
        moods.append("warm")
    if warm < -0.05:
        moods.append("cool")
    if sat < 0.16:
        moods.append("muted")
    if sat > 0.42:
        moods.append("vivid")
    if f["contrast"] > 62:
        moods.append("high contrast")
    if f["detail"] < 6:
        moods.append("still")
    if f["detail"] > 16:
        moods.append("busy")
    if f["greenness"] > 0.02:
        moods.append("green")
    if f["blueness"] > 0.03:
        moods.append("open sky or water")
    if not moods:
        moods.append("even")

    scene = (
        f"A {'dark' if bright < 60 else 'bright' if bright > 170 else 'mid-light'} frame, "
        f"mostly {palette}, with {'a lot of' if f['detail'] > 14 else 'little'} detail in it. "
        "Read from colour and light only — no key needed for this one."
    )
    return time_of_day, palette, energy, moods[:5], scene


def score_buckets(f, energy):
    bright, warm, sat = f["brightness"], f["warmth"], f["saturation"]
    s = {
        "golden_hour": 40 + warm * 190 - abs(bright - 140) * 0.34,
        "night_city":  40 + (70 - bright) * 0.85 + f["contrast"] * 0.22,
        "monsoon":     34 + (0.22 - sat) * 150 + (-warm) * 90 - abs(bright - 115) * 0.2,
        "nature_calm": 32 + f["greenness"] * 340 + (14 - min(f["detail"], 14)) * 1.2,
        "beach_summer": 30 + f["blueness"] * 300 + max(0, bright - 150) * 0.32,
        "party":       18 + sat * 95 + f["detail"] * 2.1 + max(0, 90 - bright) * 0.18,
        "romantic":    34 + warm * 110 + (14 - min(f["detail"], 14)) * 1.5,
        "melancholy":  30 + (0.20 - sat) * 160 + (140 - bright) * 0.22,
        "travel":      30 + f["contrast"] * 0.32 + max(0, bright - 130) * 0.22,
        "festive":     16 + sat * 120 + f["detail"] * 1.7 + warm * 70,
    }
    return sorted(s.items(), key=lambda kv: kv[1], reverse=True)


def local_analyze(raw, count=3, lang="any", era="any", steer="", energy_floor=0, nonce=0):
    f = photo_features(raw)
    time_of_day, palette, energy, moods, scene = describe(f)
    target = max(energy, energy_floor or 0)

    ranked = score_buckets(f, energy)
    lib = library()

    pool = []
    for rank, (bucket, score) in enumerate(ranked[:4]):
        for song in lib[bucket]["songs"]:
            fit = score - rank * 6 - abs(song["energy"] - target) * 0.55
            if energy_floor and song["energy"] < energy_floor:
                fit -= 45
            if lang != "any" and lang.lower().split()[0] not in song["lang"].lower():
                fit -= 60
            if era != "any":
                y = song["year"]
                want = (
                    y >= 2022 if "last three" in era else
                    2010 <= y <= 2019 if "2010s" in era else
                    1990 <= y <= 2009 if "1990s" in era else
                    1960 <= y <= 1989 if "1960s" in era else True
                )
                if not want:
                    fit -= 35
            pool.append((fit, bucket, song))

    seed = int(hashlib.sha1(raw[:4096]).hexdigest()[:8], 16) + int(nonce)
    rng = random.Random(seed)
    pool.sort(key=lambda t: t[0] + rng.uniform(0, 7), reverse=True)

    picked, artists = [], set()
    for fit, bucket, song in pool:
        if song["artist"] in artists:
            continue
        artists.add(song["artist"])
        picked.append({
            "title": song["title"],
            "artist": song["artist"],
            "year": song["year"],
            "language": song["lang"],
            "why": why_line(song, bucket, f, target, rng),
        })
        if len(picked) >= count:
            break

    return {
        "scene": scene, "timeOfDay": time_of_day, "palette": palette,
        "energy": energy, "moods": moods, "songs": picked,
    }


def why_line(song, bucket, f, target, rng):
    label = library()[bucket]["label"]
    gap = song["energy"] - target
    pace = ("lifts the frame" if gap > 14 else
            "sits right at the pace of the frame" if abs(gap) <= 14 else
            "slows the frame down")
    openers = [
        f"Reads as {label} — this one {pace}.",
        f"The light and colour here say {label}; the track {pace}.",
        f"Matched on {label}. It {pace}.",
    ]
    return rng.choice(openers)


# ---------------------------------------------------------------------------
# 2. gemini — Google AI Studio free tier
# ---------------------------------------------------------------------------

def gemini_analyze(raw, media_type, count, lang, era, steer, energy_floor):
    key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not key:
        raise AnalyzerError("GOOGLE_API_KEY isn't set.")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": media_type,
                                 "data": base64.b64encode(raw).decode()}},
                {"text": build_prompt(count, lang, era, steer, energy_floor)},
            ],
        }],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 1600,
                             "responseMimeType": "application/json"},
    }
    try:
        r = requests.post(url, params={"key": key}, json=body, timeout=90)
    except requests.RequestException as exc:
        raise AnalyzerError(f"Couldn't reach Google: {exc}")
    if r.status_code >= 400:
        raise AnalyzerError(f"Gemini returned {r.status_code}: {_err(r)}")
    try:
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise AnalyzerError("Gemini sent an empty answer.")
    return _shape(parse_json_loose(text), count)


# ---------------------------------------------------------------------------
# 3. openrouter — free vision models
# ---------------------------------------------------------------------------

def openrouter_analyze(raw, media_type, count, lang, era, steer, energy_floor):
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise AnalyzerError("OPENROUTER_API_KEY isn't set.")
    model = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.2-11b-vision-instruct:free").strip()
    data_uri = f"data:{media_type};base64,{base64.b64encode(raw).decode()}"
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "max_tokens": 1600,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": build_prompt(count, lang, era, steer, energy_floor)},
                ]}],
            },
            timeout=120,
        )
    except requests.RequestException as exc:
        raise AnalyzerError(f"Couldn't reach OpenRouter: {exc}")
    if r.status_code >= 400:
        raise AnalyzerError(f"OpenRouter returned {r.status_code}: {_err(r)}")
    try:
        text = r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise AnalyzerError("OpenRouter sent an empty answer.")
    return _shape(parse_json_loose(text), count)


# ---------------------------------------------------------------------------
# 4. anthropic — Claude
# ---------------------------------------------------------------------------

def anthropic_analyze(raw, media_type, count, lang, era, steer, energy_floor):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise AnalyzerError("ANTHROPIC_API_KEY isn't set.")
    text = anthropic_text(
        [{"type": "image", "source": {"type": "base64", "media_type": media_type,
                                      "data": base64.b64encode(raw).decode()}},
         {"type": "text", "text": build_prompt(count, lang, era, steer, energy_floor)}],
        max_tokens=1600)
    return _shape(parse_json_loose(text), count)


def anthropic_text(content, max_tokens=800):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise AnalyzerError("ANTHROPIC_API_KEY isn't set.")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5").strip()
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": content}]},
            timeout=90,
        )
    except requests.RequestException as exc:
        raise AnalyzerError(f"Couldn't reach Anthropic: {exc}")
    if r.status_code >= 400:
        raise AnalyzerError(f"Claude returned {r.status_code}: {_err(r)}")
    blocks = r.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def _err(resp):
    try:
        body = resp.json()
        return (body.get("error", {}) or {}).get("message") or str(body)[:200]
    except Exception:
        return resp.text[:200]


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------

LABELS = {
    "local": "offline reader (free, unlimited)",
    "gemini": "Google Gemini",
    "openrouter": "OpenRouter",
    "anthropic": "Claude",
}


def default_order():
    raw = os.environ.get("ANALYZER_ORDER", "gemini,openrouter,anthropic,local")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def available():
    return {
        "local": Image is not None,
        "gemini": bool(os.environ.get("GOOGLE_API_KEY", "").strip()),
        "openrouter": bool(os.environ.get("OPENROUTER_API_KEY", "").strip()),
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
    }


def analyze(raw, media_type, count=3, lang="any", era="any", steer="", energy_floor=0, nonce=0):
    """Try each configured back end in turn. Returns (data, provider, tried)."""
    tried = []
    for name in default_order():
        try:
            if name == "local":
                data = local_analyze(raw, count, lang, era, steer, energy_floor, nonce)
            elif name == "gemini":
                data = gemini_analyze(raw, media_type, count, lang, era, steer, energy_floor)
            elif name == "openrouter":
                data = openrouter_analyze(raw, media_type, count, lang, era, steer, energy_floor)
            elif name == "anthropic":
                data = anthropic_analyze(raw, media_type, count, lang, era, steer, energy_floor)
            else:
                continue
            return data, name, tried
        except AnalyzerError as exc:
            tried.append({"provider": name, "error": str(exc)})
        except Exception as exc:  # never let one back end take the request down
            tried.append({"provider": name, "error": f"{type(exc).__name__}: {exc}"})

    # The chain is built so this can only happen if 'local' was left out of
    # ANALYZER_ORDER or Pillow is missing.
    try:
        return local_analyze(raw, count, lang, era, steer, energy_floor, nonce), "local", tried
    except AnalyzerError as exc:
        tried.append({"provider": "local", "error": str(exc)})
        raise AnalyzerError("; ".join(f"{t['provider']}: {t['error']}" for t in tried))
