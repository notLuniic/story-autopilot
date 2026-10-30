"""
A small stand-in for Supabase (database REST API, storage and password login), for tests.
It covers only what Story Autopilot and supabase-js use. Run on its own for browser testing:

    python tests/fake_supabase.py 54321
"""
import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import urllib.parse
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE_KEY = "sb_secret_test"
ANON_KEY = "sb_publishable_test"
USERS = {"oliver@example.com": {"id": "11111111-1111-1111-1111-111111111111", "password": "correct horse"}}
APP_USERS = {"11111111-1111-1111-1111-111111111111"}
PKEYS = {"clients": "slug", "posts": "id", "occurrences": "key", "lockins": "client", "log": "id", "autopilot_status": "id"}
APP_WRITABLE = {"posts", "clients"}


def now():
    fake = os.environ.get("AUTOPILOT_NOW")     # tests can pretend it's another time
    t = dt.datetime.fromisoformat(fake) if fake else dt.datetime.now(dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).isoformat()


def jwt(sub, email):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    exp = int(dt.datetime.now().timestamp()) + 3600
    return f"{enc({'alg': 'HS256', 'typ': 'JWT'})}.{enc({'sub': sub, 'email': email, 'role': 'authenticated', 'aud': 'authenticated', 'exp': exp})}.sig"


class DB:
    def __init__(self):
        self.lock = threading.RLock()
        self.tables = {t: [] for t in PKEYS}
        self.objects = {}          # path -> {"data", "mimetype", "created_at", "etag"}
        self.tokens = {}           # access token -> user id
        self.signed = {}           # token -> path
        self.next_log = 1
        self.requests = []         # (method, path) log for assertions

    def seed_client(self, slug="gwc", **kw):
        row = {"slug": slug, "name": "Greta Workers Club", "timezone": "Australia/Sydney",
               "platforms": ["instagram", "facebook"], "paused": False, "grace_hours": 3,
               "schedule_changed_at": now(), "created_at": now()}
        row.update(kw)
        self.tables["clients"].append(row)

    def put_object(self, path, data, mimetype, created_at=None):
        self.objects[path] = {"data": data, "mimetype": mimetype, "created_at": created_at or now(),
                              "etag": hashlib.md5(data).hexdigest()}


def match(row, params):
    for col, cond in params.items():
        if col in ("select", "order", "limit", "offset", "on_conflict", "columns"):
            continue
        op, _, val = cond.partition(".")
        v = row.get(col)
        sv = "" if v is None else (str(v).lower() if isinstance(v, bool) else str(v))
        if op == "eq" and sv != val: return False
        if op == "neq" and sv == val: return False
        if op == "gte" and not sv >= val: return False
        if op == "gt" and not sv > val: return False
        if op == "lte" and not (float(sv) <= float(val) if col == "id" else sv <= val): return False
        if op == "lt" and not sv < val: return False
        if op == "in" and sv not in [x.strip('"') for x in val.strip("()").split(",")]: return False
        if op == "is" and not (val == "null" and v is None): return False
    return True


def make_handler(db):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,PUT,DELETE,OPTIONS,HEAD")
            self.send_header("Access-Control-Expose-Headers", "*")

        def send(self, code, body=None, ctype="application/json", raw=None):
            data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
            self.send_response(code)
            self._cors()
            if data or ctype != "application/json":
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_OPTIONS(self):
            self.send(204)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def who(self):
            """'service', a user id, or None (anon)."""
            if self.headers.get("apikey") == SERVICE_KEY:
                return "service"
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                tok = auth[7:]
                if tok == SERVICE_KEY:
                    return "service"
                return db.tokens.get(tok)
            return None

        def handle_any(self):
            u = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
            path = urllib.parse.unquote(u.path)
            db.requests.append((self.command, path))
            with db.lock:
                if path.startswith("/auth/v1/"):
                    return self.auth(path[9:], q)
                if path.startswith("/rest/v1/"):
                    return self.rest(path[9:], q)
                if path.startswith("/storage/v1/"):
                    return self.storage(path[12:], q)
            self.send(404, {"message": "not found"})

        do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = do_HEAD = handle_any

        # ---- auth
        def auth(self, path, q):
            if path == "token":
                b = json.loads(self.body() or b"{}")
                if q.get("grant_type") == "password":
                    u = USERS.get(b.get("email"))
                    if not u or u["password"] != b.get("password"):
                        return self.send(400, {"error": "invalid_grant", "error_description": "Invalid login credentials",
                                               "code": 400, "msg": "Invalid login credentials"})
                    uid, email = u["id"], b["email"]
                else:
                    uid = db.tokens.get(b.get("refresh_token"))
                    if not uid:
                        return self.send(400, {"error": "invalid_grant", "msg": "Invalid Refresh Token"})
                    email = next(e for e, x in USERS.items() if x["id"] == uid)
                at, rt = jwt(uid, email), secrets.token_hex(8)
                db.tokens[at] = uid
                db.tokens[rt] = uid
                user = {"id": uid, "email": email, "aud": "authenticated", "role": "authenticated",
                        "app_metadata": {}, "user_metadata": {}, "created_at": now()}
                return self.send(200, {"access_token": at, "token_type": "bearer", "expires_in": 3600,
                                       "expires_at": int(dt.datetime.now().timestamp()) + 3600,
                                       "refresh_token": rt, "user": user})
            if path == "user":
                uid = self.who()
                if not uid or uid == "service":
                    return self.send(401, {"msg": "invalid token"})
                email = next(e for e, x in USERS.items() if x["id"] == uid)
                return self.send(200, {"id": uid, "email": email, "aud": "authenticated", "role": "authenticated"})
            if path == "logout":
                return self.send(204)
            return self.send(404, {"msg": "not found"})

        # ---- database
        def rest(self, table, q):
            if table not in PKEYS:
                return self.send(404, {"message": f"relation {table} does not exist"})
            who = self.who()
            allowed_read = who == "service" or who in APP_USERS
            rows = db.tables[table]
            prefer = self.headers.get("Prefer", "")
            if self.command in ("GET", "HEAD"):
                if not allowed_read:
                    return self.send(200, [])                 # RLS: sees nothing
                out = [r for r in rows if match(r, q)]
                if "order" in q:
                    col, _, d = q["order"].partition(".")
                    out.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=d.startswith("desc"))
                off = int(q.get("offset", 0))
                out = out[off: off + int(q["limit"])] if "limit" in q else out[off:]
                return self.send(200, json.loads(json.dumps(out)))
            if who != "service" and not (who in APP_USERS and table in APP_WRITABLE):
                return self.send(403 if who else 401, {"message": f"new row violates row-level security policy for table \"{table}\""})
            pk = PKEYS[table]
            if self.command == "POST":
                payload = json.loads(self.body() or b"[]")
                payload = payload if isinstance(payload, list) else [payload]
                if table == "clients" and who != "service":
                    return self.send(403, {"message": "insert not allowed"})
                result = []
                for item in payload:
                    item = dict(item)
                    if table == "log" and "id" not in item:
                        item["id"] = db.next_log
                        db.next_log += 1
                    if table == "posts":
                        err = validate_post(item)
                        if err:
                            return self.send(400, {"message": err, "code": "23514"})
                        item.setdefault("created_at", now())
                        item.setdefault("paused", False)
                        item["updated_at"] = now()
                    existing = next((r for r in rows if str(r.get(pk)) == str(item.get(pk))), None)
                    if existing and "merge-duplicates" in prefer:
                        existing.update(item)
                        result.append(existing)
                    elif existing:
                        return self.send(409, {"message": "duplicate key value violates unique constraint", "code": "23505"})
                    else:
                        rows.append(item)
                        result.append(item)
                    if table == "posts":
                        touch_client(db, item["client"])
                if "return=representation" in prefer:
                    return self.send(201, result)
                return self.send(201)
            if self.command == "PATCH":
                patch = json.loads(self.body() or b"{}")
                hit = [r for r in rows if match(r, q)]
                for r in hit:
                    r.update(patch)
                    if table == "posts":
                        r["updated_at"] = now()
                        touch_client(db, r["client"])
                return self.send(200, hit) if "return=representation" in prefer else self.send(204)
            if self.command == "DELETE":
                hit = [r for r in rows if match(r, q)]
                for r in hit:
                    rows.remove(r)
                    if table == "posts":
                        touch_client(db, r["client"])
                return self.send(200, hit) if "return=representation" in prefer else self.send(204)
            return self.send(405, {"message": "method not allowed"})

        # ---- storage
        def storage(self, path, q):
            who = self.who()
            ok = who == "service" or who in APP_USERS
            m = re.match(r"object/sign/stories/?(.*)$", path)
            if m and self.command == "GET":                # a signed link being used (Meta, or <img>)
                obj_path = db.signed.get(q.get("token"))
                if not obj_path or obj_path != m.group(1) or obj_path not in db.objects:
                    return self.send(400, {"message": "invalid signature"})
                o = db.objects[obj_path]
                return self.send(200, raw=o["data"], ctype=o["mimetype"])
            if m and self.command == "POST":
                if not ok:
                    return self.send(400, {"message": "Object not found"})
                b = json.loads(self.body() or b"{}")
                def sign(p):
                    if p not in db.objects:
                        return None
                    t = secrets.token_hex(12)
                    db.signed[t] = p
                    return f"/object/sign/stories/{urllib.parse.quote(p)}?token={t}"
                if m.group(1):
                    s = sign(m.group(1))
                    return self.send(200, {"signedURL": s}) if s else self.send(400, {"message": "Object not found"})
                return self.send(200, [{"path": p, "signedURL": sign(p), "error": None if p in db.objects else "Object not found"}
                                       for p in b.get("paths", [])])
            if path == "object/list/stories" and self.command == "POST":
                if not ok:
                    return self.send(200, [])
                b = json.loads(self.body() or b"{}")
                prefix = b.get("prefix", "").strip("/") + "/"
                items = [{"name": p[len(prefix):], "id": hashlib.md5(p.encode()).hexdigest(), "created_at": o["created_at"],
                          "metadata": {"size": len(o["data"]), "mimetype": o["mimetype"], "eTag": f'"{o["etag"]}"'}}
                         for p, o in sorted(db.objects.items()) if p.startswith(prefix)]
                off, lim = b.get("offset", 0), b.get("limit", 100)
                return self.send(200, items[off: off + lim])
            if path == "object/stories" and self.command == "DELETE":
                if not ok:
                    return self.send(400, {"message": "not allowed"})
                b = json.loads(self.body() or b"{}")
                gone = [p for p in b.get("prefixes", []) if db.objects.pop(p, None) is not None]
                return self.send(200, [{"name": p} for p in gone])
            m = re.match(r"object/stories/(.+)$", path)
            if m and self.command in ("POST", "PUT"):
                if not ok:
                    return self.send(403, {"statusCode": "403", "error": "Unauthorized", "message": "new row violates row-level security policy"})
                p = m.group(1)
                if p in db.objects and self.headers.get("x-upsert") != "true":
                    return self.send(400, {"statusCode": "409", "error": "Duplicate", "message": "The resource already exists"})
                data, ctype = self.body(), self.headers.get("Content-Type", "")
                if ctype.startswith("multipart/form-data"):
                    msg = BytesParser(policy=email_policy).parsebytes(
                        f"Content-Type: {ctype}\r\n\r\n".encode() + data)
                    part = next(pt for pt in msg.iter_parts() if pt.get_filename() is not None or pt.get_param("name", header="content-disposition") == "")
                    data, ctype = part.get_payload(decode=True), part.get_content_type()
                if ctype not in ("image/jpeg", "video/mp4", "video/quicktime"):
                    return self.send(400, {"statusCode": "415", "error": "invalid_mime_type", "message": f"mime type {ctype} is not supported"})
                if len(data) > 52428800:
                    return self.send(400, {"statusCode": "413", "error": "Payload too large", "message": "The object exceeded the maximum allowed size"})
                db.put_object(p, data, ctype)
                return self.send(200, {"Key": f"stories/{p}", "Id": hashlib.md5(p.encode()).hexdigest()})
            if m and self.command == "GET":
                if not ok or m.group(1) not in db.objects:
                    return self.send(400, {"message": "Object not found"})
                o = db.objects[m.group(1)]
                return self.send(200, raw=o["data"], ctype=o["mimetype"])
            return self.send(404, {"message": f"no route {self.command} {path}"})
    return H


def validate_post(p):
    if p.get("type") not in ("image", "video"):
        return "type must be image or video"
    if not p.get("platforms") or not set(p["platforms"]) <= {"instagram", "facebook"}:
        return "violates check constraint posts_platforms_check"
    if not re.match(r"^([01][0-9]|2[0-3]):[0-5][0-9]$", p.get("time") or ""):
        return "violates check constraint posts_time_check"
    if p.get("repeat", "once") not in ("once", "weekly"):
        return "violates check constraint posts_repeat_check"
    return None


def touch_client(db, slug):
    for c in db.tables["clients"]:
        if c["slug"] == slug:
            c["schedule_changed_at"] = now()


def serve(port=0):
    db = DB()
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(db))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, db, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    srv, db, url = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 54321)
    db.seed_client(paused=True)
    db.tables["autopilot_status"].append({"id": 1, "last_run_at": now(), "ok": True, "message": "gwc: paused"})
    print(f"Fake Supabase at {url}  (login oliver@example.com / correct horse)", flush=True)
    threading.Event().wait()
