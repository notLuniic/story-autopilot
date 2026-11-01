"""
Engine tests: the real SupabaseStore talking HTTP to a stand-in Supabase, and a fake Meta that
downloads each signed link the way Meta would. Run from the repo root:  python tests/test_engine.py
Needs Pillow; video checks also need ffmpeg/ffprobe (skipped if missing).
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import autopilot as ap  # noqa: E402
import fake_supabase as fs  # noqa: E402
import requests  # noqa: E402
from PIL import Image  # noqa: E402


def jpg(w=1080, h=1920, fmt="JPEG"):
    b = io.BytesIO()
    Image.new("RGB", (w, h), (10, 30, 80)).save(b, fmt)
    return b.getvalue()


class FakeMeta:
    """Downloads the signed link like Meta does, then records the post."""
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail or {}

    def post(self, platform, post, url):
        r = requests.get(url, timeout=10)
        assert r.status_code == 200, f"Meta couldn't fetch {url}: {r.status_code}"
        key = (post["id"], platform)
        if self.fail.get(key, 0) > 0:
            self.fail[key] -= 1
            raise ap.MetaError("The media could not be fetched from URI")
        self.calls.append((post["id"], platform, len(r.content), r.headers.get("Content-Type")))
        return f"{platform}-{len(self.calls)}"

    def account(self):
        return {"ok": True, "problems": [], "instagram": "@gwc", "facebook": "GWC", "token": "never expires"}


def post(pid, name, date, time, repeat="once", platforms=("instagram", "facebook"), media=None, kind="image", **kw):
    return dict({"id": pid, "client": "gwc", "name": name, "media_path": media or f"gwc/{pid}.jpg", "type": kind,
                 "platforms": list(platforms), "repeat": repeat, "date": date, "time": time,
                 "created_at": "2026-10-29T00:00:00+00:00", "paused": False}, **kw)


def setup(posts=(), paused=False, objects=None):
    srv, db, url = fs.serve()
    db.seed_client(paused=paused)
    for p in posts:
        db.tables["posts"].append(p)
    for path, (data, mime) in (objects or {}).items():
        db.put_object(path, data, mime, created_at="2026-10-29T00:00:00+00:00")
    os.environ.update(GWC_META_TOKEN="tok", GWC_IG_ID="ig1", GWC_PAGE_ID="pg1")
    store = ap.SupabaseStore(url, fs.SERVICE_KEY)
    return srv, db, store


def run(store, meta, when):
    os.environ["AUTOPILOT_NOW"] = when
    errors = 0
    for c in ap.load_clients(store):
        e = ap.Engine(store, c, meta=meta)
        reason = e.needs_lockin()
        if reason:
            e.lockin(reason)
        e.post_due()
        e.tidy()
        errors += e.errors
    store.heartbeat(errors == 0, "test")
    return errors


TRIVIA = post("p_trivia", "Trivia", "2026-11-05", "16:30", "weekly")
CUP = post("p_cup", "Melbourne Cup", "2026-11-03", "12:00", platforms=("facebook",))
PNG = post("p_png", "PNG in disguise", "2026-11-06", "17:00", platforms=("instagram",))
GONE = post("p_gone", "Missing file", "2026-11-12", "17:00", "weekly", platforms=("instagram",))
GOOD = {"gwc/p_trivia.jpg": (jpg(), "image/jpeg"), "gwc/p_cup.jpg": (jpg(), "image/jpeg"),
        "gwc/p_png.jpg": (jpg(fmt="PNG"), "image/jpeg")}


def test_lockin_checks_this_week_and_next_and_caches_downloads():
    srv, db, store = setup([TRIVIA, CUP, PNG, GONE], objects=GOOD)
    run(store, FakeMeta(), "2026-11-02T00:05+11:00")
    lk = db.tables["lockins"][0]
    assert lk["week_start"] == "2026-11-02" and lk["reason"] == "new week"
    assert lk["weeks"] == [{"start": "2026-11-02", "ready": 2, "problems": 1}, {"start": "2026-11-09", "ready": 1, "problems": 1}]
    assert lk["items"]["p_png@2026-11-06"]["problems"] == ["file is really PNG, it needs to be a JPG"]
    assert lk["items"]["p_gone@2026-11-12"]["problems"] == ["media file is missing from storage"]
    assert [x["kind"] for x in db.tables["log"]].count("lock-in") == 2
    downloads = sum(1 for m, p in db.requests if m == "GET" and p.startswith("/storage/v1/object/stories/"))
    assert downloads == 3
    # a schedule change re-checks, but unchanged files aren't downloaded again
    os.environ["AUTOPILOT_NOW"] = "2026-11-02T09:00+11:00"         # you add a story at 9am
    db.tables["posts"].append(post("p_new", "New", "2026-11-04", "10:00", media="gwc/p_cup.jpg"))
    fs.touch_client(db, "gwc")
    run(store, FakeMeta(), "2026-11-02T09:05+11:00")
    assert db.tables["lockins"][0]["reason"] == "schedule changed"
    assert sum(1 for m, p in db.requests if m == "GET" and p.startswith("/storage/v1/object/stories/")) == 3
    assert len([x for x in db.tables["log"] if x["kind"] == "lock-in"]) == 2      # not logged twice
    srv.shutdown()


def test_posts_once_per_platform_with_signed_links():
    srv, db, store = setup([TRIVIA], objects=GOOD)
    meta = FakeMeta()
    run(store, meta, "2026-11-05T16:20+11:00")
    assert meta.calls == []
    run(store, meta, "2026-11-05T16:34+11:00")
    assert sorted(c[1] for c in meta.calls) == ["facebook", "instagram"]
    assert meta.calls[0][3] == "image/jpeg" and meta.calls[0][2] == len(GOOD["gwc/p_trivia.jpg"][0])
    run(store, meta, "2026-11-05T16:49+11:00")
    assert len(meta.calls) == 2                                   # no doubles
    run(store, meta, "2026-11-12T16:34+11:00")                    # weekly: next Thursday again
    assert len(meta.calls) == 4
    assert db.tables["autopilot_status"][0]["ok"] is True
    srv.shutdown()


def test_retry_then_success_and_missed_after_window():
    srv, db, store = setup([TRIVIA], objects=GOOD)
    meta = FakeMeta(fail={("p_trivia", "instagram"): 1})
    assert run(store, meta, "2026-11-05T16:34+11:00") == 1
    occ = db.tables["occurrences"][0]
    assert occ["platforms"]["instagram"]["status"] == "failed" and occ["platforms"]["facebook"]["status"] == "posted"
    run(store, meta, "2026-11-05T16:49+11:00")
    assert [(x["kind"], x["severity"]) for x in db.tables["log"]] == [("publish", "error"), ("publish", "info")]
    srv.shutdown()

    srv, db, store = setup([TRIVIA], objects=GOOD)
    meta = FakeMeta(fail={("p_trivia", "instagram"): 99})
    for t in ["16:34", "16:49", "17:04", "18:04", "19:04"]:
        run(store, meta, f"2026-11-05T{t}+11:00")
    run(store, meta, "2026-11-05T19:34+11:00")
    log = db.tables["log"]
    assert log[0]["kind"] == "publish" and log[0]["count"] == 5 and log[1]["kind"] == "missed"
    assert db.tables["autopilot_status"][0]["ok"] is False
    run(store, meta, "2026-11-05T19:49+11:00")
    assert len(db.tables["log"]) == 2
    srv.shutdown()


def test_paused_and_late_additions_post_nothing():
    late = dict(CUP, created_at="2026-11-03T05:00:00+00:00")       # added at 16:00, slot was 12:00
    srv, db, store = setup([late], objects=GOOD)
    meta = FakeMeta()
    run(store, meta, "2026-11-03T16:04+11:00")
    assert meta.calls == [] and not db.tables["log"][1:]
    srv.shutdown()
    srv, db, store = setup([TRIVIA], paused=True, objects=GOOD)
    run(store, meta, "2026-11-05T16:34+11:00")
    run(store, meta, "2026-11-05T21:00+11:00")
    assert meta.calls == [] and not [x for x in db.tables["log"] if x["kind"] == "missed"]
    assert db.tables["lockins"][0]["paused"] is True
    srv.shutdown()


def test_missing_meta_details_are_reported():
    srv, db, store = setup([TRIVIA], objects=GOOD)
    os.environ.pop("GWC_META_TOKEN")
    run(store, FakeMeta(), "2026-11-05T16:34+11:00")
    msgs = [x["message"] for x in db.tables["log"]]
    assert any("GWC_META_TOKEN" in m for m in msgs)
    assert db.tables["lockins"][0]["account"]["ok"] is False
    srv.shutdown()


def test_tidy_removes_finished_stories_and_orphans_but_not_fresh_uploads():
    old = post("p_old", "Old one-off", "2026-10-20", "10:00")
    srv, db, store = setup([TRIVIA, old], objects=dict(GOOD, **{"gwc/p_old.jpg": (jpg(), "image/jpeg"),
                                                               "gwc/orphan.jpg": (jpg(), "image/jpeg")}))
    db.put_object("gwc/just-uploading.jpg", jpg(), "image/jpeg")   # created now: may be mid-upload
    run(store, FakeMeta(), "2026-11-02T00:05+11:00")
    ids = {p["id"] for p in db.tables["posts"]}
    assert ids == {"p_trivia"}
    assert "gwc/p_old.jpg" not in db.objects and "gwc/orphan.jpg" not in db.objects
    assert "gwc/p_trivia.jpg" in db.objects and "gwc/just-uploading.jpg" in db.objects
    srv.shutdown()


def test_video_checks():
    if not shutil.which("ffmpeg"):
        return
    tmp = Path(tempfile.mkdtemp())
    for name, secs in (("ok.mp4", 5), ("long.mp4", 65)):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=navy:s=540x960:d={secs}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", str(tmp / name)], check=True)
    v_ok = post("p_v", "Video", "2026-11-04", "10:00", kind="video", media="gwc/p_v.mp4")
    v_long = post("p_l", "Long video", "2026-11-04", "11:00", kind="video", media="gwc/p_l.mp4")
    srv, db, store = setup([v_ok, v_long], objects={"gwc/p_v.mp4": ((tmp / "ok.mp4").read_bytes(), "video/mp4"),
                                                    "gwc/p_l.mp4": ((tmp / "long.mp4").read_bytes(), "video/mp4")})
    meta = FakeMeta()
    run(store, meta, "2026-11-04T10:04+11:00")
    items = db.tables["lockins"][0]["items"]
    assert items["p_v@2026-11-04"]["ok"] and "stories must be 3 to 60 seconds" in items["p_l@2026-11-04"]["problems"][0]
    assert meta.calls and meta.calls[0][3] == "video/mp4"
    srv.shutdown()


def test_post_command_counts_as_todays_post():
    srv, db, store = setup([TRIVIA], objects=GOOD)
    os.environ["AUTOPILOT_NOW"] = "2026-11-05T09:00+11:00"
    meta = FakeMeta()
    orig = ap.Meta
    ap.Meta = lambda c: meta
    try:
        assert ap.main(["post", "--post", "p_trivia"], store=store) == 0
    finally:
        ap.Meta = orig
    run(store, meta, "2026-11-05T16:34+11:00")
    assert len(meta.calls) == 2                                   # the manual post wasn't repeated at 16:30
    srv.shutdown()


def test_meta_calls_follow_graph_api_flow():
    srv, db, store = setup([])
    calls = []

    class R:
        def __init__(self, body, code=200):
            self.status_code, self._b, self.text = code, body, json.dumps(body)

        def json(self):
            return self._b

    def fake_request(method, url, data=None, params=None, timeout=None):
        path = url.split("/v25.0/")[1]
        args = data or params or {}
        calls.append((method, path, {k: v for k, v in args.items() if k != "access_token"}))
        table = {"ig1/media": {"id": "c1"}, "c1": {"status_code": "FINISHED"}, "ig1/media_publish": {"id": "igmedia1"},
                 "pg1/photos": {"id": "ph1"}, "pg1/photo_stories": {"success": True, "post_id": "fbstory1"},
                 "v1": {"status": {"uploading_phase": {"status": "complete"}}}}
        if path == "pg1" and args.get("fields") == "access_token":
            return R({"access_token": "pagetok"})
        if path == "pg1/video_stories":
            return R({"video_id": "v1", "upload_url": "https://rupload.facebook.com/video-upload/v1"}
                     if args.get("upload_phase") == "start" else {"success": True, "post_id": "fbvideo1"})
        return R(table[path]) if path in table else R({"error": {"message": f"unexpected {path}"}}, 400)

    def fake_post(url, headers=None, timeout=None):
        calls.append(("UPLOAD", url, {"file_url": headers.get("file_url"), "auth": headers.get("Authorization")}))
        return R({"success": True})

    orig = (ap.requests.request, ap.requests.post)
    ap.requests.request, ap.requests.post = fake_request, fake_post
    try:
        m = ap.Meta(ap.load_clients(store)[0], sleep=lambda s: None)
        assert m.post("instagram", {"type": "image"}, "https://x/a.jpg") == "igmedia1"
        assert calls[0] == ("POST", "ig1/media", {"media_type": "STORIES", "image_url": "https://x/a.jpg"})
        calls.clear()
        m.post("instagram", {"type": "video"}, "https://x/a.mp4")
        assert calls[0][2] == {"media_type": "STORIES", "video_url": "https://x/a.mp4"}
        calls.clear()
        assert m.post("facebook", {"type": "image"}, "https://x/a.jpg") == "fbstory1"
        assert [c[1] for c in calls] == ["pg1", "pg1/photos", "pg1/photo_stories"]
        calls.clear()
        assert m.post("facebook", {"type": "video"}, "https://x/a.mp4") == "fbvideo1"
        assert [c[1] for c in calls] == ["pg1/video_stories", "https://rupload.facebook.com/video-upload/v1", "v1", "pg1/video_stories"]
        assert calls[1][2] == {"file_url": "https://x/a.mp4", "auth": "OAuth pagetok"}
    finally:
        ap.requests.request, ap.requests.post = orig
    srv.shutdown()


def test_instagram_publish_retries_same_container_and_detects_published():
    """media_publish fails 'cannot be found': retry the same container; if the container turns out
    PUBLISHED, count it as posted instead of uploading again (no double posts)."""
    srv, db, store = setup([])
    calls, state = [], {"publish_fails": 2, "published_after_fail": False}

    class R:
        def __init__(self, body, code=200):
            self.status_code, self._b, self.text = code, body, json.dumps(body)

        def json(self):
            return self._b

    def fake_request(method, url, data=None, params=None, timeout=None):
        path = url.split("/v25.0/")[1]
        args = data or params or {}
        calls.append((method, path))
        if path == "ig1/media":
            return R({"id": "c1"})
        if path == "c1" and args.get("fields") == "status_code,status":
            return R({"status_code": "FINISHED"})
        if path == "c1" and args.get("fields") == "status_code":
            return R({"status_code": "PUBLISHED" if state["published_after_fail"] else "FINISHED"})
        if path == "ig1/media_publish":
            if state["publish_fails"] > 0:
                state["publish_fails"] -= 1
                return R({"error": {"message": "The media with ID c1 cannot be found."}}, 400)
            return R({"id": "igmedia1"})
        return R({"error": {"message": f"unexpected {path}"}}, 400)

    orig = ap.requests.request
    ap.requests.request = fake_request
    try:
        m = ap.Meta(ap.load_clients(store)[0], sleep=lambda s: None)
        assert m.post("instagram", {"type": "image"}, "https://x/a.jpg") == "igmedia1"     # 2 fails, then ok
        assert [c[1] for c in calls].count("ig1/media") == 1                                # one upload only
        calls.clear(); state.update(publish_fails=99, published_after_fail=True)
        assert m.post("instagram", {"type": "image"}, "https://x/a.jpg") == "c1"            # error, but it went out
        assert [c[1] for c in calls].count("ig1/media_publish") == 1
        calls.clear(); state.update(publish_fails=99, published_after_fail=False)
        try:
            m.post("instagram", {"type": "image"}, "https://x/a.jpg")
            raise AssertionError("should have failed")
        except ap.MetaError as e:
            assert "cannot be found" in str(e)
        assert [c[1] for c in calls].count("ig1/media_publish") == 4                        # retried, same container
    finally:
        ap.requests.request = orig
    srv.shutdown()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name, flush=True)
