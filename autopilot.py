#!/usr/bin/env python3
"""
Story Autopilot: the posting engine.

Stories are scheduled in the web app (docs/), which saves them to Supabase: the media in a
private storage bucket, the schedule in the `posts` table. GitHub Actions runs this script
every 15 minutes. Each run, for every client:

  1. Lock-in. When the week changes (first run on Monday) or the schedule has changed, every
     story due this week and next week is checked: media present and in a format Meta accepts,
     platforms enabled, Meta account reachable, token valid. Result: `lockins` table.
  2. Posting. Anything due (time reached, inside the grace window, not yet posted) is published
     to Instagram and/or Facebook Page stories. Meta is given a signed link to the media that
     works for one hour; the media is never public.
  3. Logging. Lock-in problems, failed posts and missed posts go to the `log` table (the app's
     error log).
  4. Tidying. Media and one-off stories more than 8 days past their last date are removed, so
     the free 1 GB of storage doesn't fill up.

Meta's API publishes stories immediately; it cannot hold a scheduled story in Business Suite.
So the schedule lives here and each story is published at its time.

Commands:
  run      Lock-in if needed, post anything due, tidy (what the schedule runs)
  check    Re-check this week and next week now and print the result
  week     Print this week and next week
  ids      List the Pages and Instagram accounts a Meta token can reach
  post     Post one story immediately: --post p_xxxx

Needs SUPABASE_URL and SUPABASE_SERVICE_KEY, plus per client <SLUG>_META_TOKEN, <SLUG>_IG_ID
and <SLUG>_PAGE_ID (e.g. GWC_META_TOKEN), all as GitHub Secrets.
"""

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v25.0")
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
BUCKET = "stories"
LOG_LIMIT = 400
KEEP_DAYS = 8            # keep media/one-off stories this long after their last date (for the app's week view)
KEEP_RECORDS_DAYS = 35
SIGNED_URL_SECONDS = 3600

IMAGE_MAX = 8 * 1024 * 1024
VIDEO_MAX = 50 * 1024 * 1024     # Supabase free plan per-file limit (Meta allows 100 MB)
VIDEO_MIN_S, VIDEO_MAX_S = 3, 60
NEEDED_SCOPES = {"instagram": {"instagram_basic", "instagram_content_publish"},
                 "facebook": {"pages_show_list", "pages_manage_posts", "pages_read_engagement"}}


class ConfigError(Exception):
    pass


class MetaError(Exception):
    pass


class StoreError(Exception):
    pass


def log(msg):
    print(msg, flush=True)


def now_utc():
    fake = os.environ.get("AUTOPILOT_NOW")          # for testing: pretend it's this time
    if fake:
        return dt.datetime.fromisoformat(fake).astimezone(dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.isoformat(timespec="seconds")


def parse_ts(s):
    if not s:
        return None
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def secret(name):
    if not name:
        return None
    if os.environ.get(name):
        return os.environ[name]
    blob = os.environ.get("SECRETS_JSON")
    if blob:
        try:
            return json.loads(blob).get(name)
        except json.JSONDecodeError:
            return None
    return None


# ------------------------------------------------------------------- store ----

class SupabaseStore:
    """The Supabase database (REST) and storage bucket, used with the service key."""

    def __init__(self, url, key, session=None):
        if not url or not key:
            raise ConfigError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set as GitHub Secrets")
        self.url = url.rstrip("/")
        self.key = key
        self.http = session or requests.Session()
        self.headers = {"apikey": key}
        if key.startswith("eyJ"):                     # legacy JWT service_role key
            self.headers["Authorization"] = f"Bearer {key}"

    def _req(self, method, path, params=None, body=None, headers=None, raw=False):
        h = dict(self.headers, **(headers or {}))
        r = self.http.request(method, f"{self.url}{path}", params=params,
                              json=body if body is not None else None, headers=h, timeout=60)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message") or r.text
            except ValueError:
                msg = r.text
            raise StoreError(f"Supabase {method} {path.split('?')[0]} failed ({r.status_code}): {str(msg)[:200]}")
        if raw:
            return r.content
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def _rest(self, method, table, params=None, body=None, prefer=None):
        return self._req(method, f"/rest/v1/{table}", params=params, body=body,
                         headers={"Prefer": prefer} if prefer else None)

    # database
    def clients(self):
        return self._rest("GET", "clients", {"select": "*", "order": "slug"}) or []

    def posts(self, client):
        return self._rest("GET", "posts", {"select": "*", "client": f"eq.{client}"}) or []

    def delete_post(self, post_id):
        self._rest("DELETE", "posts", {"id": f"eq.{post_id}"})

    def occurrences(self, client, since):
        rows = self._rest("GET", "occurrences", {"select": "*", "client": f"eq.{client}",
                                                  "date": f"gte.{since.isoformat()}"}) or []
        return {r["key"]: r for r in rows}

    def save_occurrence(self, row):
        self._rest("POST", "occurrences", body=dict(row, updated_at=iso(now_utc())),
                   prefer="resolution=merge-duplicates,return=minimal")

    def prune_occurrences(self, client, before):
        self._rest("DELETE", "occurrences", {"client": f"eq.{client}", "date": f"lt.{before.isoformat()}"})

    def lockin(self, client):
        rows = self._rest("GET", "lockins", {"select": "*", "client": f"eq.{client}"}) or []
        return rows[0] if rows else {}

    def save_lockin(self, row):
        self._rest("POST", "lockins", body=row, prefer="resolution=merge-duplicates,return=minimal")

    def recent_log(self, client, n=60):
        return self._rest("GET", "log", {"select": "*", "client": f"eq.{client}", "order": "id.desc",
                                         "limit": str(n)}) or []

    def add_log(self, row):
        self._rest("POST", "log", body=row, prefer="return=minimal")

    def bump_log(self, log_id, count, last_at):
        self._rest("PATCH", "log", {"id": f"eq.{log_id}"}, body={"count": count, "last_at": last_at},
                   prefer="return=minimal")

    def prune_log(self, client, keep=LOG_LIMIT):
        rows = self._rest("GET", "log", {"select": "id", "client": f"eq.{client}", "order": "id.desc",
                                         "offset": str(keep), "limit": "1"}) or []
        if rows:
            self._rest("DELETE", "log", {"client": f"eq.{client}", "id": f"lte.{rows[0]['id']}"})

    def heartbeat(self, ok, message):
        self._rest("POST", "autopilot_status", body={"id": 1, "last_run_at": iso(now_utc()), "ok": ok,
                                                     "message": message[:500]},
                   prefer="resolution=merge-duplicates,return=minimal")

    # storage
    def list_media(self, client):
        """{path: {"size", "mimetype", "etag"}} for everything in the client's folder."""
        out, offset = {}, 0
        while True:
            rows = self._req("POST", f"/storage/v1/object/list/{BUCKET}",
                             body={"prefix": client, "limit": 1000, "offset": offset}) or []
            for r in rows:
                if r.get("id") is None:               # a sub-folder, not a file
                    continue
                md = r.get("metadata") or {}
                out[f"{client}/{r['name']}"] = {"size": md.get("size") or md.get("contentLength"),
                                                "mimetype": md.get("mimetype"), "etag": md.get("eTag"),
                                                "created_at": r.get("created_at")}
            if len(rows) < 1000:
                return out
            offset += 1000

    def download(self, path):
        return self._req("GET", f"/storage/v1/object/{BUCKET}/{urllib.parse.quote(path)}", raw=True)

    def sign(self, path, seconds=SIGNED_URL_SECONDS):
        body = self._req("POST", f"/storage/v1/object/sign/{BUCKET}/{urllib.parse.quote(path)}",
                         body={"expiresIn": seconds})
        signed = (body or {}).get("signedURL") or (body or {}).get("signedUrl")
        if not signed:
            raise StoreError(f"Supabase didn't return a link for {path}")
        return signed if signed.startswith("http") else f"{self.url}/storage/v1{signed}"

    def delete_media(self, paths):
        if paths:
            self._req("DELETE", f"/storage/v1/object/{BUCKET}", body={"prefixes": list(paths)})


def store_from_env():
    return SupabaseStore(secret("SUPABASE_URL"), secret("SUPABASE_SERVICE_KEY"))


# ----------------------------------------------------------------- clients ----

class Client:
    def __init__(self, row):
        self.row = row
        self.slug = row["slug"]
        self.name = row.get("name") or self.slug
        self.tz = ZoneInfo(row.get("timezone") or "Australia/Sydney")
        self.platforms = list(row.get("platforms") or ["instagram", "facebook"])
        self.grace = dt.timedelta(hours=float(row.get("grace_hours") or 3))
        self.paused = bool(row.get("paused"))
        self.schedule_changed_at = parse_ts(row.get("schedule_changed_at"))
        prefix = self.slug.upper().replace("-", "_")
        self.token = secret(f"{prefix}_META_TOKEN")
        self.ig_id = secret(f"{prefix}_IG_ID")
        self.page_id = secret(f"{prefix}_PAGE_ID")
        self.secret_names = {"token": f"{prefix}_META_TOKEN", "Instagram account ID": f"{prefix}_IG_ID",
                             "Facebook Page ID": f"{prefix}_PAGE_ID"}

    def missing_meta(self):
        need = {"token": self.token}
        if "instagram" in self.platforms:
            need["Instagram account ID"] = self.ig_id
        if "facebook" in self.platforms:
            need["Facebook Page ID"] = self.page_id
        return [f"{k} ({self.secret_names[k]})" for k, v in need.items() if not v]


def load_clients(store, only=None):
    rows = store.clients()
    if only:
        rows = [r for r in rows if r["slug"] == only]
        if not rows:
            raise ConfigError(f"no client '{only}' in the clients table")
    return [Client(r) for r in rows]


# ---------------------------------------------------------------- schedule ----

def week_start(day):
    return day - dt.timedelta(days=day.weekday())


def occurrences(posts, tz, start_date, end_date):
    """Every (post, date, when) between start_date and end_date inclusive, in time order."""
    out = []
    for p in posts:
        if p.get("paused"):
            continue
        try:
            first = dt.date.fromisoformat(str(p["date"]))
            hh, mm = (int(x) for x in p["time"].split(":"))
        except (KeyError, ValueError):
            continue
        if p.get("repeat") == "weekly":
            d = first
            if d < start_date:
                d += dt.timedelta(days=((start_date - d).days + 6) // 7 * 7)
            dates = []
            while d <= end_date:
                if d >= start_date:
                    dates.append(d)
                d += dt.timedelta(days=7)
            if p.get("until"):
                until = dt.date.fromisoformat(str(p["until"]))
                dates = [x for x in dates if x <= until]
        else:
            dates = [first] if start_date <= first <= end_date else []
        for d in dates:
            out.append((p, d, dt.datetime.combine(d, dt.time(hh, mm), tzinfo=tz)))
    out.sort(key=lambda x: x[2])
    return out


def occ_key(post, date):
    return f"{post['id']}@{date.isoformat()}"


def last_date(post):
    """The last date a story can post, or None if it repeats forever."""
    if post.get("repeat") == "weekly":
        return dt.date.fromisoformat(str(post["until"])) if post.get("until") else None
    return dt.date.fromisoformat(str(post["date"]))


# ------------------------------------------------------------------- media ----

def probe_video(path):
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
             "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout or "{}")
    except Exception:
        return None


def inspect_file(path, kind, size):
    """(problems, warnings) for a media file on disk."""
    problems, warnings = [], []
    path = Path(path)
    if kind == "video":
        if size > VIDEO_MAX:
            problems.append(f"video is {size / 1e6:.0f} MB, the limit is 50 MB")
        info = probe_video(path)
        if info:
            dur = float((info.get("format") or {}).get("duration") or 0)
            if dur and not (VIDEO_MIN_S <= dur <= VIDEO_MAX_S + 0.5):
                problems.append(f"video is {dur:.0f}s long, stories must be 3 to 60 seconds")
            v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
            if not v:
                problems.append("file has no video track")
            else:
                if v.get("codec_name") not in ("h264", "hevc"):
                    warnings.append(f"video codec is {v.get('codec_name')}; H.264 is safest")
                w, h = v.get("width") or 0, v.get("height") or 0
                if w and h and abs(w / h - 9 / 16) > 0.02:
                    warnings.append(f"{w}x{h} isn't 9:16, it may be cropped")
    else:
        if size > IMAGE_MAX:
            problems.append(f"image is {size / 1e6:.1f} MB, the limit is 8 MB")
        try:
            from PIL import Image
            with Image.open(path) as im:
                if im.format != "JPEG":
                    problems.append(f"file is really {im.format}, it needs to be a JPG")
                w, h = im.size
            if abs(w / h - 9 / 16) > 0.02:
                warnings.append(f"{w}x{h} isn't 9:16, it may be cropped")
        except ImportError:
            pass
        except Exception as e:
            problems.append(f"image can't be read ({e})")
    return problems, warnings


class MediaChecker:
    """Checks a post's media in storage. Results are cached by file + eTag, so unchanged
    files aren't downloaded again on every lock-in."""

    def __init__(self, store, client, cache=None):
        self.store, self.client = store, client
        self.cache = dict(cache or {})
        self._listing = None

    def listing(self):
        if self._listing is None:
            self._listing = self.store.list_media(self.client)
        return self._listing

    def check(self, post):
        path = post.get("media_path")
        if not path:
            return ["no media attached"], []
        info = self.listing().get(path)
        if not info:
            return ["media file is missing from storage"], []
        kind = post.get("type") or "image"
        mime = info.get("mimetype") or ""
        if kind == "image" and mime and mime != "image/jpeg":
            return [f"stored as {mime}, Instagram needs a JPG"], []
        if kind == "video" and mime and mime not in ("video/mp4", "video/quicktime"):
            return [f"stored as {mime}, video must be MP4 or MOV"], []
        tag = f"{info.get('etag')}:{info.get('size')}"
        hit = self.cache.get(path)
        if hit and hit.get("tag") == tag:
            return list(hit["problems"]), list(hit["warnings"])
        try:
            data = self.store.download(path)
        except StoreError as e:
            return [f"media can't be downloaded ({e})"], []
        with tempfile.NamedTemporaryFile(suffix=Path(path).suffix) as f:
            f.write(data)
            f.flush()
            problems, warnings = inspect_file(f.name, kind, len(data))
        self.cache[path] = {"tag": tag, "problems": problems, "warnings": warnings}
        return problems, warnings


# -------------------------------------------------------------------- meta ----

def graph(method, path, **params):
    resp = requests.request(method, f"{GRAPH}/{path}", data=params if method == "POST" else None,
                            params=params if method == "GET" else None, timeout=90)
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:300]}
    if resp.status_code >= 400 or (isinstance(body, dict) and "error" in body):
        err = body.get("error", body) if isinstance(body, dict) else {}
        msg = err.get("error_user_msg") or err.get("message") or str(err)
        raise MetaError(f"{msg} ({method} /{path.split('?')[0].split('/')[-1]}, HTTP {resp.status_code})")
    return body


class Meta:
    def __init__(self, client, sleep=time.sleep):
        self.c = client
        self.sleep = sleep
        self._page_token = None

    def page_token(self):
        if not self._page_token:
            body = graph("GET", self.c.page_id, fields="access_token", access_token=self.c.token)
            self._page_token = body.get("access_token") or self.c.token
        return self._page_token

    # Instagram: create a STORIES container, wait until it's processed, publish
    def instagram(self, post, url):
        video = post.get("type") == "video"
        c = graph("POST", f"{self.c.ig_id}/media", media_type="STORIES", access_token=self.c.token,
                  **{"video_url" if video else "image_url": url})
        cid = c["id"]
        for _ in range(100 if video else 20):
            st = graph("GET", cid, fields="status_code,status", access_token=self.c.token)
            code = st.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise MetaError(f"Instagram couldn't process the media: {st.get('status') or code}")
            self.sleep(5 if video else 3)
        else:
            raise MetaError("Instagram took too long to process the media")
        return graph("POST", f"{self.c.ig_id}/media_publish", creation_id=cid, access_token=self.c.token).get("id")

    # Facebook Page: photo uploaded unpublished then made a story; video via the 3-step upload
    def facebook(self, post, url):
        tok = self.page_token()
        if post.get("type") != "video":
            photo = graph("POST", f"{self.c.page_id}/photos", url=url, published="false", access_token=tok)
            out = graph("POST", f"{self.c.page_id}/photo_stories", photo_id=photo["id"], access_token=tok)
            return out.get("post_id") or out.get("id")
        start = graph("POST", f"{self.c.page_id}/video_stories", upload_phase="start", access_token=tok)
        vid, upload_url = start["video_id"], start["upload_url"]
        r = requests.post(upload_url, headers={"Authorization": f"OAuth {tok}", "file_url": url}, timeout=180)
        if r.status_code >= 400:
            raise MetaError(f"Facebook video upload failed (HTTP {r.status_code}): {r.text[:200]}")
        for _ in range(40):
            try:
                st = graph("GET", vid, fields="status", access_token=tok).get("status") or {}
            except MetaError:
                break
            phase = (st.get("uploading_phase") or {}).get("status")
            if phase in ("complete", None):
                break
            if phase == "error":
                raise MetaError("Facebook couldn't upload the video")
            self.sleep(3)
        out = graph("POST", f"{self.c.page_id}/video_stories", upload_phase="finish", video_id=vid, access_token=tok)
        if out.get("success") is False:
            raise MetaError("Facebook didn't accept the video story")
        return out.get("post_id") or vid

    def post(self, platform, post, url):
        return self.instagram(post, url) if platform == "instagram" else self.facebook(post, url)

    def account(self):
        info = {"ok": True, "problems": [], "warnings": []}
        try:
            if "instagram" in self.c.platforms:
                info["instagram"] = "@" + str(graph("GET", self.c.ig_id, fields="username",
                                                    access_token=self.c.token).get("username"))
            if "facebook" in self.c.platforms:
                info["facebook"] = graph("GET", self.c.page_id, fields="name", access_token=self.c.token).get("name")
                self.page_token()
            dbg = graph("GET", "debug_token", input_token=self.c.token, access_token=self.c.token).get("data", {})
            exp = dbg.get("expires_at")
            if exp == 0:
                info["token"] = "never expires"
            elif exp:
                when = dt.datetime.fromtimestamp(exp, dt.timezone.utc)
                info["token"] = f"expires {when:%d %b %Y}"
                if when - now_utc() < dt.timedelta(days=14):
                    info["problems"].append(f"Meta token expires {when:%d %b %Y}; make a never-expiring one")
            scopes = set(dbg.get("scopes") or [])
            if scopes:
                for p in self.c.platforms:
                    missing = NEEDED_SCOPES[p] - scopes
                    if missing:
                        info["problems"].append(f"token is missing permissions: {', '.join(sorted(missing))}")
        except (MetaError, requests.RequestException) as e:
            info["problems"].append(f"Meta: {e}")
        info["ok"] = not info["problems"]
        return info


# ------------------------------------------------------------------ engine ----

class Engine:
    def __init__(self, store, client, meta=None):
        self.s, self.c = store, client
        self.meta = meta or Meta(client)
        self.posts = store.posts(client.slug)
        self.lock = store.lockin(client.slug) or {}
        self.checker = MediaChecker(store, client.slug, self.lock.get("media_checks"))
        self._recent = None
        self.errors = 0

    def today(self):
        return now_utc().astimezone(self.c.tz).date()

    # -- log --------------------------------------------------------------------
    def add_log(self, kind, message, severity="error", post=None, date=None, when=None, platform=None):
        if self._recent is None:
            self._recent = self.s.recent_log(self.c.slug)
        stamp = iso(now_utc())
        key = json.dumps([kind, message, post and post.get("id"), date and date.isoformat(), platform])
        for e in self._recent:
            if e.get("key") == key:                     # same problem again: count it, don't repeat it
                e["count"] = (e.get("count") or 1) + 1
                self.s.bump_log(e["id"], e["count"], stamp)
                break
        else:
            row = {"client": self.c.slug, "key": key, "at": stamp, "last_at": stamp, "count": 1, "kind": kind,
                   "severity": severity, "message": message, "post_id": post and post.get("id"),
                   "name": post and post.get("name"), "date": date and date.isoformat(),
                   "time": when and when.strftime("%H:%M"), "platform": platform}
            self.s.add_log(row)
            self._recent = None
        if severity == "error" and kind in ("publish", "missed"):
            self.errors += 1
        log(f"   [{severity}] {kind}: {message}" + (f" ({post.get('name')} {date} {platform or ''})" if post else ""))

    # -- lock-in ----------------------------------------------------------------
    def needs_lockin(self):
        if self.lock.get("week_start") != week_start(self.today()).isoformat():
            return "new week"
        checked = parse_ts(self.lock.get("checked_at"))
        if not checked or (self.c.schedule_changed_at and self.c.schedule_changed_at > checked):
            return "schedule changed"
        return None

    def lockin(self, reason):
        now, ws = now_utc(), week_start(self.today())
        old_items = self.lock.get("items") or {}
        missing = self.c.missing_meta()
        account = {"ok": False, "problems": ["Meta not connected yet: missing " + ", ".join(missing)]} \
            if missing else self.meta.account()
        for msg in account.get("problems", []):
            if msg not in ((self.lock.get("account") or {}).get("problems") or []):
                self.add_log("account", msg, "error")
        items, weeks = {}, []
        for w in range(2):
            start = ws + dt.timedelta(days=7 * w)
            ready = bad = 0
            for post, date, when in occurrences(self.posts, self.c.tz, start, start + dt.timedelta(days=6)):
                key = occ_key(post, date)
                problems, warnings = self.checker.check(post)
                off = [p for p in post.get("platforms") or [] if p not in self.c.platforms]
                if off:
                    problems.append(f"{', '.join(off)} isn't switched on for this client")
                items[key] = {"ok": not problems, "problems": problems, "warnings": warnings,
                              "date": date.isoformat(), "time": when.strftime("%H:%M")}
                if problems:
                    bad += 1
                    for m in problems:
                        if m not in (old_items.get(key) or {}).get("problems", []):
                            self.add_log("lock-in", m, "error", post, date, when)
                else:
                    ready += 1
            weeks.append({"start": start.isoformat(), "ready": ready, "problems": bad})
        live = {p.get("media_path") for p in self.posts}
        self.lock = {"client": self.c.slug, "checked_at": iso(now), "reason": reason, "week_start": ws.isoformat(),
                     "account": account, "weeks": weeks, "items": items, "paused": self.c.paused,
                     "media_checks": {k: v for k, v in self.checker.cache.items() if k in live}}
        self.s.save_lockin(self.lock)
        log(f"[{self.c.slug}] lock-in ({reason}): " + "; ".join(
            f"week of {w['start']}: {w['ready']} ready, {w['problems']} with problems" for w in weeks))

    # -- posting ----------------------------------------------------------------
    def post_due(self, dry_run=False):
        now, today = now_utc(), self.today()
        if self.c.paused:
            return
        recs = self.s.occurrences(self.c.slug, today - dt.timedelta(days=2))
        for post, date, when in occurrences(self.posts, self.c.tz, today - dt.timedelta(days=2), today):
            if when > now:
                continue
            created = parse_ts(post.get("created_at"))
            if created and created > when:
                continue                                   # scheduled after this slot had passed
            key = occ_key(post, date)
            rec = recs.get(key) or {"key": key, "client": self.c.slug, "post_id": post["id"], "name": post.get("name"),
                                    "date": date.isoformat(), "time": when.strftime("%H:%M"),
                                    "media_path": post.get("media_path"), "poster_path": post.get("poster_path"),
                                    "type": post.get("type"), "platforms": {}}
            plats = rec["platforms"]
            todo = [p for p in post.get("platforms") or [] if (plats.get(p) or {}).get("status") not in ("posted", "missed")]
            if not todo:
                continue
            if now >= when + self.c.grace:                 # window closed without a post
                for p in todo:
                    last = (plats.get(p) or {}).get("error")
                    plats[p] = {"status": "missed", "at": iso(now), "error": last}
                    self.add_log("missed", f"not posted within {self.c.grace.total_seconds() / 3600:g} hours of its time"
                                 + (f"; last error: {last}" if last else ""), "error", post, date, when, p)
                self.s.save_occurrence(rec)
                continue
            local = now.astimezone(self.c.tz).strftime("%a %H:%M")
            if dry_run:
                log(f"[{self.c.slug}] {local}: WOULD post '{post.get('name')}' to {', '.join(todo)}")
                continue
            problems, _ = self.checker.check(post)
            url = None
            for p in todo:
                slot = plats.setdefault(p, {"attempts": 0})
                slot["attempts"] = (slot.get("attempts") or 0) + 1
                try:
                    if self.c.missing_meta():
                        raise MetaError("Meta not connected: missing " + ", ".join(self.c.missing_meta()))
                    if problems:
                        raise MetaError("; ".join(problems))
                    url = url or self.s.sign(post["media_path"])
                    pid = self.meta.post(p, post, url)
                    slot.update(status="posted", at=iso(now_utc()), id=pid, error=None)
                    log(f"[{self.c.slug}] {local}: posted '{post.get('name')}' to {p} (id {pid})")
                    if slot["attempts"] > 1:
                        self.add_log("publish", f"posted on attempt {slot['attempts']}", "info", post, date, when, p)
                except (MetaError, StoreError, requests.RequestException) as e:
                    slot.update(status="failed", at=iso(now_utc()), error=str(e))
                    self.add_log("publish", str(e), "error", post, date, when, p)
                self.s.save_occurrence(rec)                # save after each platform, so nothing doubles

    # -- tidying ------------------------------------------------------------------
    def tidy(self):
        today = self.today()
        cutoff = today - dt.timedelta(days=KEEP_DAYS)
        for p in list(self.posts):
            end = last_date(p)
            if end and end < cutoff:
                self.s.delete_post(p["id"])
                self.posts.remove(p)
                log(f"[{self.c.slug}] tidied finished story '{p.get('name')}'")
        keep = {x for p in self.posts for x in (p.get("media_path"), p.get("poster_path")) if x}
        recent = self.s.occurrences(self.c.slug, cutoff)
        keep |= {x for r in recent.values() for x in (r.get("media_path"), r.get("poster_path")) if x}
        recent_upload = now_utc() - dt.timedelta(hours=6)        # never touch a file that may be mid-upload
        orphans = [path for path, info in self.checker.listing().items()
                   if path not in keep and (parse_ts(info.get("created_at")) or recent_upload) < recent_upload]
        if orphans:
            self.s.delete_media(orphans)
            log(f"[{self.c.slug}] removed {len(orphans)} old media file(s)")
        self.s.prune_occurrences(self.c.slug, today - dt.timedelta(days=KEEP_RECORDS_DAYS))
        self.s.prune_log(self.c.slug)


# ---------------------------------------------------------------- commands ----

def cmd_run(args, store):
    errors, notes = 0, []
    for c in load_clients(store, args.client):
        eng = Engine(store, c)
        reason = eng.needs_lockin()
        if reason:
            eng.lockin(reason)
        eng.post_due(dry_run=args.dry_run)
        if not args.dry_run:
            try:
                eng.tidy()
            except StoreError as e:
                log(f"[{c.slug}] tidying skipped: {e}")
        errors += eng.errors
        notes.append(f"{c.slug}: {'paused' if c.paused else 'live'}")
    if not args.dry_run:
        store.heartbeat(errors == 0, ", ".join(notes) + (f"; {errors} posting error(s)" if errors else ""))
    return 1 if errors else 0


def cmd_check(args, store):
    bad = 0
    for c in load_clients(store, args.client):
        eng = Engine(store, c)
        eng.lockin("manual")
        a = eng.lock["account"]
        log(f"== {c.name} ({c.slug}){' PAUSED' if c.paused else ''} ==")
        log(f"   Instagram: {a.get('instagram', '-')}  Facebook: {a.get('facebook', '-')}  Token: {a.get('token', '-')}")
        for m in a.get("problems", []):
            log(f"   PROBLEM {m}")
        for k, it in eng.lock["items"].items():
            for m in it["problems"]:
                log(f"   PROBLEM {it['date']} {it['time']} {k}: {m}")
            for m in it["warnings"]:
                log(f"   note    {it['date']} {it['time']} {k}: {m}")
        bad += sum(w["problems"] for w in eng.lock["weeks"]) + (0 if a["ok"] else 1)
    log("\nAll good." if not bad else f"\n{bad} problem(s).")
    return 1 if bad else 0


def cmd_week(args, store):
    for c in load_clients(store, args.client):
        posts = store.posts(c.slug)
        ws = week_start(now_utc().astimezone(c.tz).date())
        log(f"== {c.name} ({c.tz}){' PAUSED' if c.paused else ''} ==")
        for w in range(2):
            start = ws + dt.timedelta(days=7 * w)
            log(f"  Week of {start:%a %d %b}")
            occ = occurrences(posts, c.tz, start, start + dt.timedelta(days=6))
            if not occ:
                log("    (nothing scheduled)")
            for post, date, when in occ:
                log(f"    {when:%a %d %b %H:%M}  {post.get('name')}  [{', '.join(post.get('platforms') or [])}]"
                    f"  {post.get('type')}, {post.get('repeat', 'once')}  ({post['id']})")
    return 0


def cmd_ids(args, store=None):
    tok = secret(args.token_secret)
    if not tok:
        raise ConfigError(f"no token found in secret {args.token_secret}")
    pages = graph("GET", "me/accounts", fields="name,id,instagram_business_account{id,username}",
                  access_token=tok).get("data", [])
    if not pages:
        log("The token can't see any Pages. Check the Page is assigned to the system user.")
        return 1
    for p in pages:
        ig = p.get("instagram_business_account") or {}
        log(f"Page: {p['name']}\n   Page ID:      {p['id']}\n"
            f"   Instagram:    @{ig.get('username', '(none linked)')}\n   Instagram ID: {ig.get('id', '-')}")
    return 0


def cmd_post(args, store):
    if not args.post:
        raise ConfigError("post needs --post (the story's ID, shown in the app when you open a story)")
    for c in load_clients(store, args.client):
        post = next((p for p in store.posts(c.slug) if p["id"] == args.post), None)
        if not post:
            continue
        if c.missing_meta():
            raise ConfigError("missing " + ", ".join(c.missing_meta()))
        eng = Engine(store, c)
        problems, _ = eng.checker.check(post)
        if problems:
            raise ConfigError("; ".join(problems))
        url, today = store.sign(post["media_path"]), eng.today()
        due_today = [d for _, d, _ in occurrences([post], c.tz, today, today)]
        rec = store.occurrences(c.slug, today).get(occ_key(post, today)) if due_today else None
        for p in post.get("platforms") or []:
            pid = eng.meta.post(p, post, url)
            log(f"posted '{post.get('name')}' to {p} (id {pid})")
            if due_today:                                 # counts as today's post, so it won't go twice
                rec = rec or {"key": occ_key(post, today), "client": c.slug, "post_id": post["id"],
                              "name": post.get("name"), "date": today.isoformat(), "time": post["time"],
                              "media_path": post.get("media_path"), "poster_path": post.get("poster_path"),
                              "type": post.get("type"), "platforms": {}}
                rec["platforms"][p] = {"status": "posted", "at": iso(now_utc()), "id": pid, "attempts": 1}
                store.save_occurrence(rec)
        return 0
    raise ConfigError(f"no story with ID {args.post}")


def main(argv=None, store=None):
    ap = argparse.ArgumentParser(description="Post scheduled Instagram/Facebook stories.")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "check", "week", "ids", "post"])
    ap.add_argument("--client")
    ap.add_argument("--post", help="for post: the story ID")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--token-secret", default="META_TOKEN")
    args = ap.parse_args(argv)
    try:
        if args.command == "ids":
            return cmd_ids(args)
        store = store or store_from_env()
        return {"run": cmd_run, "check": cmd_check, "week": cmd_week, "post": cmd_post}[args.command](args, store)
    except (ConfigError, MetaError, StoreError) as e:
        log(f"ERROR: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
