#!/usr/bin/env python3
"""
Voxa - المنصة المتكاملة مع إشعارات المكالمات الحية وحل مشكلة صور البروفايل
التشغيل: python voxa_app.py --invite 1234
"""
import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# --- الإعدادات الأساسية ---
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "voxa_data")
UP = os.path.join(DATA, "uploads")
DBF = os.path.join(DATA, "voxa.db")
MAXUP = 300 * 1024 * 1024
TYPES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov"
}
IMAGES = {k: v for k, v in TYPES.items() if k.startswith("image/")}
VIDEOS = {k: v for k, v in TYPES.items() if k.startswith("video/")}
AUDIO = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/mpeg": ".mp3", "audio/wav": ".wav"}
ARGS = None
ICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#c2410c"/><text x="256" y="360" font-size="300" font-weight="700" text-anchor="middle" fill="#fff" font-family="Arial,sans-serif">V</text></svg>'
CALL_SIGNALS = {}  # uid -> قائمة إشارات (طابور) # تخزين إشارات المكالمات المباشرة بين المستخدمين

# --- قاعدة البيانات ---
def db():
    c = sqlite3.connect(DBF, timeout=10)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    os.makedirs(UP, exist_ok=True)
    c = db()
    c.executescript("""
    create table if not exists users(
        id integer primary key, 
        username text unique, 
        display_name text, 
        avatar text,
        salt blob, 
        pw blob, 
        ts integer
    );
    create table if not exists sessions(tok text primary key, uid integer, ts integer);
    create table if not exists posts(id integer primary key, uid integer, kind text, caption text, file text, ts integer);
    create table if not exists likes(pid integer, uid integer, ts integer, primary key(pid, uid));
    create table if not exists follows(follower_id integer, following_id integer, ts integer, primary key(follower_id, following_id));
    create table if not exists comments(id integer primary key, pid integer, uid integer, text text, ts integer);
    create table if not exists msgs(
        id integer primary key, 
        a integer, 
        b integer, 
        text text, 
        ts integer, 
        kind text not null default 'text', 
        file text
    );
    """)
    
    cols = {r["name"] for r in c.execute("pragma table_info(users)").fetchall()}
    if "avatar" not in cols:
        c.execute("alter table users add column avatar text")
    if "dur" not in {r["name"] for r in c.execute("pragma table_info(posts)").fetchall()}:
        c.execute("alter table posts add column dur integer")
    if "banner" not in {r["name"] for r in c.execute("pragma table_info(users)").fetchall()}:
        c.execute("alter table users add column banner text")
        
    c.commit()
    c.close()

def hpw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200000)

# --- معالج الطلبات (Backend) ---
class VoxaHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a): pass

    def send_json(self, code, body, extra_headers=None):
        if not isinstance(body, bytes):
            body = (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def get_user(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            tok = cookie.get("voxa").value
        except (AttributeError, KeyError):
            return None
        c = db()
        row = c.execute("select id, username, display_name, avatar from sessions s join users u on u.id=s.uid where s.tok=?", (tok,)).fetchone()
        c.close()
        return row

    def read_json(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0 or n > 65536: raise ValueError
        d = json.loads(self.rfile.read(n).decode())
        if not isinstance(d, dict): raise ValueError
        return d

    def create_session(self, c, uid):
        tok = secrets.token_hex(24)
        c.execute("insert into sessions values(?,?,?)", (tok, uid, int(time.time())))
        c.commit()
        return {"Set-Cookie": f"voxa={tok}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000"}

    # --- GET Routes ---
    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        p = u.path

        if p == "/":
            return self.send_response_html(200, PAGE)
        if p == "/api/info":
            return self.send_json(200, {"invite": bool(ARGS.invite)})
        if p == "/manifest.json":
            return self.send_json(200, {"name": "Voxa", "short_name": "Voxa", "start_url": "/", "display": "standalone",
                                        "background_color": "#faf9f5", "theme_color": "#c2410c", "lang": "ar", "dir": "rtl",
                                        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]})
        if p == "/icon.svg":
            b = ICON_SVG.encode()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
        if p.startswith("/media/"):
            return self.serve_media(p[7:])

        me = self.get_user()
        if not me:
            return self.send_json(401, {"ok": False})

        c = db()
        try:
            if p == "/api/me":
                avatar_path = ("/media/" + me["avatar"]) if me["avatar"] else None
                return self.send_json(200, {"id": me["id"], "username": me["username"], "display_name": me["display_name"], "avatar": avatar_path})
            
            if p == "/api/call_signal":
                sigs = CALL_SIGNALS.pop(me["id"], [])
                return self.send_json(200, {"ok": True, "signals": sigs})

            if p == "/api/profile":
                username = qs.get("username", [""])[0].strip().lower()
                target_id = qs.get("id", [None])[0]
                
                if target_id:
                    user_row = c.execute("select id, username, display_name, avatar, banner from users where id=?", (target_id,)).fetchone()
                elif username:
                    user_row = c.execute("select id, username, display_name, avatar, banner from users where username=?", (username,)).fetchone()
                else:
                    user_row = c.execute("select id, username, display_name, avatar, banner from users where id=?", (me["id"],)).fetchone()

                if not user_row:
                    return self.send_json(404, {"ok": False, "error": "المستخدم غير موجود"})

                uid = user_row["id"]
                followers_cnt = c.execute("select count(*) from follows where following_id=?", (uid,)).fetchone()[0]
                following_cnt = c.execute("select count(*) from follows where follower_id=?", (uid,)).fetchone()[0]
                posts_cnt = c.execute("select count(*) from posts where uid=?", (uid,)).fetchone()[0]
                is_following = bool(c.execute("select 1 from follows where follower_id=? and following_id=?", (me["id"], uid)).fetchone())

                posts = c.execute("""select p.id, p.kind, p.caption, p.file, p.ts,
                                     (select count(*) from likes where pid=p.id) lk,
                                     exists(select 1 from likes where pid=p.id and uid=?) liked
                                     from posts p where p.uid=? order by p.id desc""", (me["id"], uid)).fetchall()

                avatar_url = ("/media/" + user_row["avatar"]) if user_row["avatar"] else None

                res_posts = []
                for rp in posts:
                    pd = dict(rp)
                    res_posts.append(pd)

                return self.send_json(200, {
                    "id": user_row["id"],
                    "username": user_row["username"],
                    "display_name": user_row["display_name"],
                    "avatar": avatar_url,
                    "banner": ("/media/" + user_row["banner"]) if user_row["banner"] else None,
                    "is_me": user_row["id"] == me["id"],
                    "is_following": is_following,
                    "followers": followers_cnt,
                    "following": following_cnt,
                    "posts_count": posts_cnt,
                    "posts": res_posts
                })

            if p == "/api/posts":
                kind = qs.get("kind", ["home"])[0]
                search = qs.get("q", [""])[0].strip()
                
                sql = """select p.id,p.kind,p.caption,p.file,p.ts, p.uid, u.display_name as name, u.username, u.avatar, p.dur,
                         (select count(*) from comments where pid=p.id) cc,
                         (select count(*) from likes where pid=p.id) lk,
                         exists(select 1 from likes where pid=p.id and uid=?) liked
                         from posts p join users u on u.id=p.uid """
                args = [me["id"]]
                conditions = []
                
                if kind in ("photo", "video", "reel"):
                    conditions.append("p.kind=?")
                    args.append(kind)
                if kind == "anyvideo":
                    conditions.append("p.kind in ('video','reel')")
                by = qs.get("by", ["all"])[0]
                if search and by == "user":
                    conditions.append("(u.display_name like ? or u.username like ?)")
                    args.extend([f"%{search}%"] * 2)
                elif search:
                    conditions.append("(u.display_name like ? or u.username like ? or p.caption like ?)")
                    args.extend([f"%{search}%"] * 3)
                try:
                    dmin, dmax = int(qs.get("dmin", ["0"])[0]), int(qs.get("dmax", ["0"])[0])
                except ValueError:
                    dmin = dmax = 0
                if dmin > 0:
                    conditions.append("p.kind!='photo' and coalesce(p.dur,0)>=?")
                    args.append(dmin)
                if dmax > 0:
                    conditions.append("p.kind!='photo' and coalesce(p.dur,0)<=?")
                    args.append(dmax)
                
                if conditions:
                    sql += "where " + " and ".join(conditions) + " "
                
                sql += "order by p.id desc limit 50"
                rows = c.execute(sql, args).fetchall()
                
                res_posts = []
                for r in rows:
                    rd = dict(r)
                    if rd.get("avatar"):
                        rd["avatar"] = "/media/" + rd["avatar"]
                    res_posts.append(rd)
                return self.send_json(200, res_posts)

            if p == "/api/comments":
                pid = int(qs.get("pid", ["0"])[0])
                rows = c.execute("""select c.id, c.text, c.ts, u.display_name as name, u.username 
                                    from comments c join users u on u.id=c.uid 
                                    where c.pid=? order by c.id asc""", (pid,)).fetchall()
                return self.send_json(200, [dict(r) for r in rows])

            if p == "/api/users":
                search = qs.get("q", [""])[0].strip()
                if search:
                    rows = c.execute("""select id, username, display_name, avatar from users 
                                        where id!=? and (username like ? or display_name like ?) order by display_name""", 
                                     (me["id"], f"%{search}%", f"%{search}%")).fetchall()
                else:
                    rows = c.execute("select id, username, display_name, avatar, banner from users where id!=? order by display_name", (me["id"],)).fetchall()
                
                res_users = []
                for r in rows:
                    rd = dict(r)
                    if rd.get("avatar"):
                        rd["avatar"] = "/media/" + rd["avatar"]
                    res_users.append(rd)
                return self.send_json(200, res_users)

            if p == "/api/msgs":
                target = int(qs.get("with", ["0"])[0])
                rows = c.execute("""select id,a,text,ts,kind,file from msgs 
                                    where (a=? and b=?) or (a=? and b=?) order by id asc limit 200""",
                                 (me["id"], target, target, me["id"])).fetchall()
                return self.send_json(200, [{
                    "id": r["id"], "text": r["text"], "ts": r["ts"],
                    "mine": r["a"] == me["id"], "kind": r["kind"],
                    "file": ("/media/" + r["file"]) if r["file"] else None
                } for r in rows])

            if p == "/api/notifs":
                rows = c.execute("""select u.display_name as name,p.kind,l.ts from likes l 
                                    join posts p on p.id=l.pid join users u on u.id=l.uid
                                    where p.uid=? and l.uid!=? order by l.ts desc limit 30""",
                                 (me["id"], me["id"])).fetchall()
                return self.send_json(200, [dict(r) for r in rows])
        except (ValueError, sqlite3.Error) as err:
            return self.send_json(400, {"ok": False, "error": str(err)})
        finally:
            c.close()
        self.send_json(404, {"ok": False})

    def send_response_html(self, code, html):
        b = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def serve_media(self, name):
        if not re.fullmatch(r"[a-f0-9]{32}\.[a-z0-9]{3,4}", name):
            return self.send_json(404, {"ok": False})
        path = os.path.join(UP, name)
        if not os.path.isfile(path):
            return self.send_json(404, {"ok": False})

        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200

        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end: return self.send_json(416, {"ok": False})
            code = 206

        self.send_response(code)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "private, max-age=86400")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk: break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except OSError:
            self.close_connection = True

    # --- POST Routes ---
    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        qs = parse_qs(u.query)

        if self.headers.get("X-Voxa") != "1":
            return self.send_json(403, {"ok": False})

        if p == "/upload":
            return self.handle_upload(qs)
        if p == "/voice":
            return self.handle_voice_upload(qs)
        if p == "/chat_upload":
            return self.handle_chat_upload(qs)
        if p == "/avatar_upload":
            return self.handle_avatar_upload()
        if p == "/banner_upload":
            return self.handle_banner_upload()

        try:
            d = self.read_json()
        except (ValueError, UnicodeDecodeError):
            return self.send_json(400, {"ok": False})

        c = db()
        try:
            if p in ("/api/register", "/api/login"):
                username = str(d.get("username", "")).strip().lower()
                display_name = str(d.get("display_name", "")).strip()
                pw = str(d.get("pw", ""))
                
                if p == "/api/register":
                    if ARGS.invite and not hmac.compare_digest(str(d.get("invite", "")).encode(), ARGS.invite.encode()):
                        return self.send_json(403, {"error": "كود الدعوة غير صحيح"})
                    if not re.fullmatch(r"[a-z0-9_]{3,20}", username):
                        return self.send_json(400, {"error": "اسم المستخدم يجب أن يكون من 3 إلى 20 حرف"})
                    if not display_name:
                        return self.send_json(400, {"error": "الرجاء إدخال الاسم الظاهر"})
                    if len(pw) < 6:
                        return self.send_json(400, {"error": "كلمة السر يجب أن تكون 6 أحرف على الأقل"})
                    
                    salt = secrets.token_bytes(16)
                    try:
                        cur = c.execute("insert into users(username, display_name, salt, pw, ts) values(?,?,?,?,?)", 
                                        (username, display_name, salt, hpw(pw, salt), int(time.time())))
                    except sqlite3.IntegrityError:
                        return self.send_json(400, {"error": "اسم المستخدم محجوز بالفعل"})
                    c.commit()
                    return self.send_json(200, {"ok": True}, self.create_session(c, cur.lastrowid))
                
                r = c.execute("select id, salt, pw from users where username=?", (username,)).fetchone()
                if not r or not hmac.compare_digest(hpw(pw, r["salt"]), r["pw"]):
                    time.sleep(0.7)
                    return self.send_json(403, {"error": "اسم المستخدم أو كلمة السر غير صحيحة"})
                return self.send_json(200, {"ok": True}, self.create_session(c, r["id"]))

            me = self.get_user()
            if not me: return self.send_json(401, {"ok": False})

            if p == "/api/call_signal":
                to = int(d.get("to"))
                q = CALL_SIGNALS.setdefault(to, [])
                q.append({"from": me["id"], "from_name": me["display_name"], "type": str(d.get("type"))[:20], "data": d.get("data"), "video": bool(d.get("video"))})
                del q[:-300]
                return self.send_json(200, {"ok": True})

            if p == "/api/update_profile":
                new_name = str(d.get("display_name", "")).strip()[:40]
                new_user = str(d.get("username", "")).strip().lower()
                if not new_name:
                    return self.send_json(400, {"error": "الاسم الظاهر لا يمكن أن يكون فارغاً"})
                if not re.fullmatch(r"[a-z0-9_]{3,20}", new_user):
                    return self.send_json(400, {"error": "اسم المستخدم 3 إلى 20 حرف إنجليزي أو أرقام أو _"})
                try:
                    c.execute("update users set display_name=?, username=? where id=?", (new_name, new_user, me["id"]))
                except sqlite3.IntegrityError:
                    return self.send_json(400, {"error": "اسم المستخدم محجوز بالفعل"})
                c.commit()
                return self.send_json(200, {"ok": True, "display_name": new_name, "username": new_user})

            if p == "/api/follow":
                target_id = int(d.get("id"))
                if target_id == me["id"]:
                    return self.send_json(400, {"error": "لا يمكنك متابعة نفسك"})
                
                is_following = bool(c.execute("select 1 from follows where follower_id=? and following_id=?", (me["id"], target_id)).fetchone())
                if is_following:
                    c.execute("delete from follows where follower_id=? and following_id=?", (me["id"], target_id))
                else:
                    c.execute("insert into follows values(?,?,?)", (me["id"], target_id, int(time.time())))
                c.commit()
                
                followers_cnt = c.execute("select count(*) from follows where following_id=?", (target_id,)).fetchone()[0]
                return self.send_json(200, {"ok": True, "is_following": not is_following, "followers": followers_cnt})

            if p == "/api/like":
                pid = int(d.get("id"))
                if c.execute("select 1 from likes where pid=? and uid=?", (pid, me["id"])).fetchone():
                    c.execute("delete from likes where pid=? and uid=?", (pid, me["id"]))
                else:
                    c.execute("insert into likes values(?,?,?)", (pid, me["id"], int(time.time())))
                c.commit()
                n = c.execute("select count(*) from likes where pid=?", (pid,)).fetchone()[0]
                return self.send_json(200, {"ok": True, "lk": n})

            if p == "/api/comment":
                pid = int(d.get("pid"))
                text = str(d.get("text", "")).strip()[:300]
                if not text: return self.send_json(400, {"ok": False})
                c.execute("insert into comments(pid, uid, text, ts) values(?,?,?,?)", (pid, me["id"], text, int(time.time())))
                c.commit()
                return self.send_json(200, {"ok": True})

            if p == "/api/send":
                to, text = int(d.get("to")), str(d.get("text", "")).strip()[:1000]
                if not text or not c.execute("select 1 from users where id=?", (to,)).fetchone():
                    return self.send_json(400, {"ok": False})
                c.execute("insert into msgs(a,b,text,ts) values(?,?,?,?)", (me["id"], to, text, int(time.time())))
                c.commit()
                return self.send_json(200, {"ok": True})

            if p == "/api/del":
                mid = int(d.get("id"))
                old = c.execute("select file from msgs where id=? and a=?", (mid, me["id"])).fetchone()
                c.execute("delete from msgs where id=? and a=?", (mid, me["id"]))
                c.commit()
                if old and old["file"]:
                    try: os.remove(os.path.join(UP, old["file"]))
                    except OSError: pass
                return self.send_json(200, {"ok": True})

            if p == "/api/logout":
                c.execute("delete from sessions where uid=?", (me["id"],))
                c.commit()
                return self.send_json(200, {"ok": True})
        except (TypeError, ValueError, sqlite3.Error) as err:
            return self.send_json(400, {"ok": False, "error": str(err)})
        finally:
            c.close()
        self.send_json(404, {"ok": False})

    def save_body(self, allowed, limit):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = allowed.get(ctype)
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            n = 0
        if not ext or n <= 0 or n > limit:
            self.close_connection = True
            self.send_json(400, {"error": "نوع الملف أو حجمه غير مقبول"})
            return None
        name = secrets.token_hex(16) + ext
        path = os.path.join(UP, name)
        left = n
        with open(path, "wb") as f:
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            os.remove(path)
            self.close_connection = True
            self.send_json(400, {"ok": False})
            return None
        return name

    def handle_avatar_upload(self):
        me = self.get_user()
        if not me:
            return self.send_json(401, {"ok": False})
        name = self.save_body(IMAGES, 10 * 1024 * 1024)
        if not name:
            return
        c = db()
        old = c.execute("select avatar from users where id=?", (me["id"],)).fetchone()
        c.execute("update users set avatar=? where id=?", (name, me["id"]))
        c.commit()
        c.close()
        if old and old["avatar"]:
            try:
                os.remove(os.path.join(UP, old["avatar"]))
            except OSError:
                pass
        self.send_json(200, {"ok": True, "avatar": "/media/" + name})

    def handle_banner_upload(self):
        me = self.get_user()
        if not me:
            return self.send_json(401, {"ok": False})
        name = self.save_body(IMAGES, 15 * 1024 * 1024)
        if not name:
            return
        c = db()
        old = c.execute("select banner from users where id=?", (me["id"],)).fetchone()
        c.execute("update users set banner=? where id=?", (name, me["id"]))
        c.commit()
        c.close()
        if old and old["banner"]:
            try:
                os.remove(os.path.join(UP, old["banner"]))
            except OSError:
                pass
        self.send_json(200, {"ok": True, "banner": "/media/" + name})

    def msg_file(self, qs, allowed, kind, limit):
        me = self.get_user()
        if not me:
            return self.send_json(401, {"ok": False})
        try:
            to = int(qs.get("to", ["0"])[0])
        except ValueError:
            to = 0
        c = db()
        ok = c.execute("select 1 from users where id=? and id!=?", (to, me["id"])).fetchone()
        c.close()
        if not ok:
            self.close_connection = True
            return self.send_json(400, {"ok": False})
        name = self.save_body(allowed, limit)
        if not name:
            return
        try:
            text = str(max(0, min(int(float(qs.get("dur", ["0"])[0])), 3600))) if kind == "audio" else ""
        except ValueError:
            text = "0"
        c = db()
        c.execute("insert into msgs(a,b,text,ts,kind,file) values(?,?,?,?,?,?)", (me["id"], to, text, int(time.time()), kind, name))
        c.commit()
        c.close()
        self.send_json(200, {"ok": True})

    def handle_voice_upload(self, qs):
        self.msg_file(qs, AUDIO, "audio", 20 * 1024 * 1024)

    def handle_chat_upload(self, qs):
        self.msg_file(qs, IMAGES, "image", 15 * 1024 * 1024)

    def handle_upload(self, qs):
        me = self.get_user()
        if not me:
            return self.send_json(401, {"ok": False})
        kind = qs.get("kind", [""])[0]
        if kind not in ("photo", "video", "reel"):
            self.close_connection = True
            return self.send_json(400, {"error": "نوع المنشور غير صحيح"})
        name = self.save_body(IMAGES if kind == "photo" else VIDEOS, MAXUP)
        if not name:
            return
        try:
            dur = max(0, min(int(float(qs.get("dur", ["0"])[0])), 86400))
        except ValueError:
            dur = 0
        c = db()
        c.execute("insert into posts(uid,kind,caption,file,ts,dur) values(?,?,?,?,?,?)",
                  (me["id"], kind, qs.get("caption", [""])[0][:300], name, int(time.time()), dur))
        c.commit()
        c.close()
        self.send_json(200, {"ok": True})


PAGE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Voxa — المنصة</title>
    <meta name="theme-color" content="#c2410c">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <link rel="manifest" href="/manifest.json">
    <link rel="icon" href="/icon.svg">
    <link href="https://fonts.googleapis.com/css2?family=Baloo+Bhaijaan+2:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #faf9f5;
            --card-bg: #ffffff;
            --text: #16181d;
            --muted: #5a5e66;
            --border: #e3e0d6;
            --primary: #c2410c;
            --primary-hover: #9a3412;
            --input-bg: #efefef;
        }
        [data-theme="dark"] {
            --bg: #121212;
            --card-bg: #1e1e1e;
            --text: #e0e0e0;
            --muted: #a0a0a0;
            --border: #333333;
            --primary: #ea580c;
            --primary-hover: #c2410c;
            --input-bg: #2a2a2a;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: var(--bg); color: var(--text); font-family: 'Baloo Bhaijaan 2', Tahoma, sans-serif; display: flex; height: 100vh; overflow: hidden; transition: background 0.3s, color 0.3s; }
        
        .sidebar { width: 260px; background: var(--card-bg); border-left: 1px solid var(--border); display: none; flex-direction: column; padding: 24px 16px; flex-shrink: 0; }
        .brand { font-size: 34px; font-weight: 700; color: var(--primary); padding: 0 16px 18px; text-align: center; }
        .nav-link { display: flex; align-items: center; gap: 14px; padding: 12px 16px; border-radius: 14px; color: var(--text); text-decoration: none; font-size: 17px; font-weight: 600; transition: background 0.2s; cursor: pointer; border: none; background: none; width: 100%; text-align: right; font-family: inherit; }
        .nav-link:hover { background: var(--input-bg); }
        .nav-link.active { background: var(--input-bg); color: var(--primary); }
        .nav-icon { width: 24px; height: 24px; fill: none; stroke: currentColor; stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round; }
        .spacer { flex: 1; }
        .create-btn { border: 0; background: var(--primary); color: #fff; font-family: inherit; font-size: 17px; font-weight: 700; border-radius: 14px; min-height: 48px; margin-bottom: 10px; cursor: pointer; }
        .divider { height: 1px; background: var(--border); margin: 4px 0; }
        .account-link { display: flex; align-items: center; gap: 14px; padding: 12px 16px; border-radius: 14px; text-decoration: none; color: var(--text); font-weight: 600; font-size: 17px; cursor: pointer; }
        .account-link:hover { background: var(--input-bg); }
        .account-avatar { width: 32px; height: 32px; border-radius: 50%; background: var(--primary); color: #fff; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 14px; overflow: hidden; }
        .account-avatar img { width: 100%; height: 100%; object-fit: cover; }

        .main-content { flex: 1; overflow-y: auto; padding: 20px 40px; display: flex; flex-direction: column; gap: 16px; align-items: center; }
        
        h2 { margin: 0; color: var(--primary); width: 100%; max-width: 600px; text-align: right; }
        .muted { color: var(--muted); }
        
        .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 16px; display: flex; flex-direction: column; margin-bottom: 24px; width: 100%; max-width: 470px; overflow: hidden; }
        .card-header { display: flex; align-items: center; gap: 10px; padding: 12px 16px; border-bottom: 1px solid var(--border); cursor: pointer; }
        .avatar-circle { width: 36px; height: 36px; border-radius: 50%; background: var(--primary); display: flex; align-items: center; justify-content: center; color: white; font-weight: bold; font-size: 15px; overflow: hidden; flex-shrink: 0; }
        .avatar-circle img { width: 100%; height: 100%; object-fit: cover; }
        .card-media { width: 100%; background: #000; display: flex; justify-content: center; }
        .card-media img, .card-media video { width: 100%; max-height: 585px; object-fit: contain; }
        .card-actions { display: flex; gap: 16px; padding: 12px 16px 4px; }
        .action-btn { background: none; border: none; font-size: 24px; cursor: pointer; color: var(--text); }
        .action-btn.liked { color: #ed4956; }
        .card-caption { padding: 4px 16px 16px; font-size: 14px; }
        
        .settings-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 16px; padding: 24px; width: 100%; max-width: 500px; display: flex; flex-direction: column; gap: 16px; }
        .settings-row { display: flex; justify-content: space-between; align-items: center; font-size: 16px; font-weight: 600; }
        .settings-row select, .settings-row input[type="color"] { padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border); background: var(--input-bg); color: var(--text); outline: none; }

        .auth-container { display: flex; justify-content: center; align-items: center; width: 100%; height: 100%; min-height: 80vh; }
        .auth-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 20px; padding: 32px 28px; max-width: 400px; width: 100%; display: flex; flex-direction: column; gap: 16px; text-align: center; }
        .auth-card input { width: 100%; padding: 12px 16px; border: 1px solid var(--border); border-radius: 12px; font-family: inherit; font-size: 15px; outline: none; background: var(--input-bg); color: var(--text); }
        .auth-card button.btn-primary { background: var(--primary); color: #fff; border: none; padding: 12px; border-radius: 12px; font-family: inherit; font-size: 16px; font-weight: 700; cursor: pointer; }
        .auth-card button.btn-secondary { background: transparent; color: var(--primary); border: 1px solid var(--primary); padding: 10px; border-radius: 12px; font-family: inherit; font-size: 15px; cursor: pointer; }

        .profile-container { width: 100%; max-width: 650px; background: var(--card-bg); border: 1px solid var(--border); border-radius: 20px; padding: 28px; display: flex; flex-direction: column; gap: 20px; }
        .profile-header { display: flex; align-items: center; gap: 24px; border-bottom: 1px solid var(--border); padding-bottom: 20px; }
        .profile-avatar { width: 90px; height: 90px; border-radius: 50%; background: var(--primary); color: white; display: flex; align-items: center; justify-content: center; font-size: 38px; font-weight: bold; flex-shrink: 0; overflow: hidden; cursor: pointer; }
        .profile-avatar img { width: 100%; height: 100%; object-fit: cover; }
        .profile-info { flex: 1; display: flex; flex-direction: column; gap: 8px; }
        .profile-stats { display: flex; gap: 20px; }
        .follow-btn { background: var(--primary); color: white; border: none; padding: 8px 20px; border-radius: 10px; font-weight: 700; cursor: pointer; }
        .follow-btn.following { background: var(--input-bg); color: var(--text); border: 1px solid var(--border); }
        .profile-posts-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; width: 100%; }
        .grid-item { aspect-ratio: 1; background: #000; overflow: hidden; border-radius: 8px; }
        .grid-item img, .grid-item video { width: 100%; height: 100%; object-fit: cover; }
        
        .chat-layout { display: flex; width: 100%; max-width: 900px; height: calc(100vh - 100px); border: 1px solid var(--border); border-radius: 16px; background: var(--card-bg); overflow: hidden; }
        .inbox-panel { width: 320px; border-left: 1px solid var(--border); display: flex; flex-direction: column; }
        .inbox-header { padding: 16px 20px; font-size: 18px; font-weight: 700; border-bottom: 1px solid var(--border); }
        .inbox-search { margin: 10px 16px; padding: 8px 12px; border-radius: 10px; border: none; background: var(--input-bg); color: var(--text); outline: none; }
        .inbox-list { flex: 1; overflow-y: auto; }
        .inbox-item { display: flex; align-items: center; gap: 12px; padding: 12px 16px; cursor: pointer; }
        .inbox-item:hover, .inbox-item.active { background: var(--input-bg); }

        .chat-panel { flex: 1; display: flex; flex-direction: column; background: var(--card-bg); }
        .chat-header { display: flex; align-items: center; justify-content: space-between; padding: 12px 20px; border-bottom: 1px solid var(--border); }
        .chat-user-info { display: flex; align-items: center; gap: 12px; cursor: pointer; }
        .chat-call-btns { display: flex; gap: 10px; }
        .call-btn { background: var(--input-bg); border: 1px solid var(--border); color: var(--text); width: 40px; height: 40px; border-radius: 50%; cursor: pointer; display: flex; align-items: center; justify-content: center; }
        .call-btn:hover { background: var(--primary); color: white; }

        .chat-messages { flex: 1; padding: 20px; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; }
        .msg-row { display: flex; align-items: flex-end; gap: 6px; width: 100%; }
        .msg-row.mine { justify-content: flex-start; }
        .msg-row.other { justify-content: flex-end; }
        .bubble { max-width: 70%; border-radius: 20px; padding: 12px 18px; font-size: 15px; word-break: break-word; }
        .mine .bubble { background: var(--input-bg); color: var(--text); border-bottom-right-radius: 4px; }
        .other .bubble { background: var(--primary); color: #fff; border-bottom-left-radius: 4px; }
        
        .chat-inputs { display: flex; gap: 8px; align-items: center; padding: 16px 20px; border-top: 1px solid var(--border); }
        .chat-inputs input[type="text"] { flex: 1; border-radius: 20px; padding: 12px 16px; border: 1px solid var(--border); outline: none; background: var(--input-bg); color: var(--text); font-family: inherit; }
        .icon-btn { width: 40px; height: 40px; border-radius: 50%; border: none; background: transparent; cursor: pointer; font-size: 20px; color: var(--text); display: flex; align-items: center; justify-content: center; }
        .icon-btn.send { color: var(--primary); font-weight: bold; }
        .icon-btn.recording { color: red; }

        /* شاشة المكالمات المنبثقة والإشعارات */
        .call-overlay { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0,0,0,0.9); display: none; flex-direction: column; align-items: center; justify-content: center; z-index: 1000; gap: 20px; color: white; }
        .video-container { display: flex; gap: 20px; flex-wrap: wrap; justify-content: center; }
        .video-container video { width: 320px; height: 240px; background: #222; border-radius: 12px; object-fit: cover; }
        .call-controls { display: flex; gap: 20px; }
        .call-control-btn { width: 60px; height: 60px; border-radius: 50%; border: none; font-size: 24px; cursor: pointer; display: flex; align-items: center; justify-content: center; }
        .end-call { background: #dc2626; color: white; }
        .accept-call { background: #16a34a; color: white; }

        /* لمعة (انعكاس) على كل الأزرار */
        .create-btn, .follow-btn, .btn-primary, .btn-secondary, .call-btn, .icon-btn.send, .call-control-btn, .nav-link.active, .chip, .filter-btn, .vbtn, .small-btn {
            position: relative; overflow: hidden;
            background-image: linear-gradient(180deg, rgba(255,255,255,.34) 0, rgba(255,255,255,0) 55%);
            box-shadow: inset 0 1px 0 rgba(255,255,255,.5), 0 6px 14px rgba(0,0,0,.14);
        }
        .btn-secondary, .call-btn, .chip, .filter-btn, .follow-btn.following, .nav-link.active { box-shadow: inset 0 1px 0 rgba(255,255,255,.7), 0 3px 8px rgba(0,0,0,.08); }
        .create-btn:active, .follow-btn:active, .btn-primary:active, .small-btn:active { transform: translateY(1px); }
        .searchbar { display: flex; gap: 8px; width: 100%; max-width: 600px; }
        .searchbar input { flex: 1; padding: 12px 18px; border-radius: 24px; border: 1px solid var(--border); background: var(--input-bg); color: var(--text); font-family: inherit; font-size: 15px; outline: none; }
        .filter-btn { width: 46px; height: 46px; border-radius: 14px; border: 1px solid var(--border); background-color: var(--card-bg); color: var(--text); cursor: pointer; display: flex; align-items: center; justify-content: center; }
        .filter-panel { width: 100%; max-width: 600px; background: var(--card-bg); border: 1px solid var(--border); border-radius: 16px; padding: 16px; display: none; flex-direction: column; gap: 12px; }
        .chips { display: flex; flex-wrap: wrap; gap: 8px; }
        .chip { border: 1px solid var(--border); background-color: var(--card-bg); color: var(--text); padding: 8px 16px; border-radius: 20px; font-family: inherit; cursor: pointer; }
        .chip.on { background-color: var(--primary); color: #fff; border-color: var(--primary); }
        .dur-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
        .dur-row input { width: 90px; padding: 8px 12px; border-radius: 10px; border: 1px solid var(--border); background: var(--input-bg); color: var(--text); font-family: inherit; }
        .dur-badge { position: absolute; left: 10px; bottom: 10px; background: rgba(0,0,0,.7); color: #fff; font-size: 12px; padding: 2px 8px; border-radius: 8px; }
        .card-media { position: relative; }
        .small { font-size: 12px; }
        .cbox { padding: 0 16px 14px; display: none; flex-direction: column; gap: 8px; }
        .crow { font-size: 14px; word-break: break-word; }
        .cform { display: flex; gap: 8px; }
        .cform input { flex: 1; padding: 10px 14px; border-radius: 20px; border: 1px solid var(--border); background: var(--input-bg); color: var(--text); font-family: inherit; outline: none; }
        .small-btn { border: 0; background-color: var(--primary); color: #fff; padding: 0 18px; min-height: 40px; border-radius: 20px; font-family: inherit; font-weight: 700; cursor: pointer; }
        .small-btn.light { background-color: var(--input-bg); color: var(--text); }
        .btn-row { display: flex; gap: 8px; flex-wrap: wrap; }
        .edit-panel { display: none; flex-direction: column; gap: 12px; background: var(--input-bg); border-radius: 16px; padding: 18px; }
        .edit-panel label { font-size: 13px; color: var(--muted); font-weight: 600; }
        .edit-panel input[type=text] { padding: 11px 14px; border-radius: 12px; border: 1px solid var(--border); background: var(--card-bg); color: var(--text); font-family: inherit; font-size: 15px; outline: none; width: 100%; }
        .edit-top { display: flex; align-items: center; gap: 14px; }
        .vbub { display: flex; align-items: center; gap: 10px; min-width: 210px; }
        .vbtn { width: 38px; height: 38px; border-radius: 50%; border: 0; background-color: var(--primary); color: #fff; cursor: pointer; font-size: 14px; flex: none; }
        .other .vbtn { background-color: #fff; color: var(--primary); }
        .wave { display: flex; align-items: center; gap: 2px; height: 28px; flex: 1; cursor: pointer; }
        .wave i { display: block; width: 3px; border-radius: 2px; background: currentColor; opacity: .3; }
        .wave i.on { opacity: 1; }
        .vtime { font-size: 12px; min-width: 32px; }
        .chat-img { max-width: 220px; width: 100%; border-radius: 12px; display: block; }
        .msg-del { background: none; border: 0; cursor: pointer; color: var(--muted); font-size: 16px; min-height: 36px; opacity: .7; }
        .icon-btn.recording { color: #dc2626; animation: pulse 1s infinite; }
        @keyframes pulse { 50% { opacity: .4; } }

        /* أيقونات + عناصر مظبوطة + نسخة الموبايل */
        .ico { width: 22px; height: 22px; fill: none; stroke: currentColor; stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round; flex: none; }
        .nav-link .ico { width: 24px; height: 24px; }
        .create-btn { display: flex; align-items: center; justify-content: center; gap: 8px; }
        .action-btn { display: inline-flex; align-items: center; gap: 6px; }
        .action-btn .ico { width: 26px; height: 26px; } .cnt { font-size: 15px; }
        input[type=search]::-webkit-search-cancel-button, input[type=search]::-webkit-search-decoration { -webkit-appearance: none; display: none; }
        input[type=number] { -moz-appearance: textfield; appearance: textfield; }
        input[type=number]::-webkit-inner-spin-button, input[type=number]::-webkit-outer-spin-button { -webkit-appearance: none; margin: 0; }
        .sbox { position: relative; flex: 1; min-width: 0; }
        .sbox input { width: 100%; padding-inline-end: 46px; }
        .inbox-panel .sbox { margin: 10px 16px; flex: none; } .sbox .inbox-search { margin: 0; width: 100%; }
        .sclear { position: absolute; inset-inline-end: 10px; top: 50%; transform: translateY(-50%); width: 28px; height: 28px; border-radius: 50%; border: 0; background-color: var(--border); color: var(--text); display: none; align-items: center; justify-content: center; cursor: pointer; padding: 0; }
        .sclear .ico { width: 16px; height: 16px; stroke-width: 2.6; }
        .stepper { display: flex; align-items: center; gap: 6px; } .stepper input { width: 64px; text-align: center; }
        .step-btn, .icon-btn, .sclear, .mtb { position: relative; overflow: hidden; background-image: linear-gradient(180deg, rgba(255,255,255,.3) 0, rgba(255,255,255,0) 55%); box-shadow: inset 0 1px 0 rgba(255,255,255,.45), 0 3px 8px rgba(0,0,0,.12); }
        .step-btn { width: 36px; height: 36px; border-radius: 50%; border: 1px solid var(--border); background-color: var(--card-bg); color: var(--text); cursor: pointer; display: flex; align-items: center; justify-content: center; padding: 0; }
        .step-btn .ico { width: 16px; height: 16px; stroke-width: 2.8; }
        .icon-btn { background-color: var(--input-bg); border: 1px solid var(--border); }
        .icon-btn.send { background-color: var(--primary); color: #fff; border-color: var(--primary); }
        .icon-btn.send .ico { transform: scaleX(-1); }
        .call-btn .ico { width: 20px; height: 20px; }
        .call-control-btn .ico { width: 28px; height: 28px; stroke-width: 2.6; }
        .vbtn .ico { width: 16px; height: 16px; fill: currentColor; stroke-width: 2.6; }
        .msg-del .ico { width: 18px; height: 18px; }
        .back-btn { display: none; }
        .mtop { display: none; }
        @media (max-width: 800px) {
            body { flex-direction: column; height: 100vh; height: 100dvh; }
            .mtop { display: flex; order: 1; align-items: center; justify-content: space-between; padding: 8px 14px; background: var(--card-bg); border-bottom: 1px solid var(--border); flex: none; }
            .sidebar[style*="none"] ~ .mtop { display: none; }
            .mbrand { font-size: 28px; font-weight: 700; color: var(--primary); }
            .mtop-btns { display: flex; gap: 8px; }
            .mtb { width: 44px; height: 44px; border-radius: 50%; border: 1px solid var(--border); background-color: var(--input-bg); color: var(--text); display: flex; align-items: center; justify-content: center; cursor: pointer; padding: 0; }
            .main-content { order: 2; padding: 12px; }
            .sidebar { order: 3; width: 100%; flex-direction: row !important; align-items: center; justify-content: space-around; padding: 6px 6px calc(6px + env(safe-area-inset-bottom)); border-left: 0; border-top: 1px solid var(--border); gap: 2px; }
            .sidebar .brand, .sidebar .spacer, .sidebar .divider, .nav-link[data-k=profile], .nav-link[data-k=photo], .nav-link[data-k=notifs], .nav-link[data-k=settings] { display: none; }
            .nav-link { flex-direction: column; gap: 2px; padding: 6px 2px; font-size: 11px; width: auto; flex: 1; text-align: center; align-items: center; justify-content: center; min-height: 52px; }
            .create-btn { flex: none; width: 52px; height: 52px; border-radius: 50%; margin: 0; padding: 0; } .create-btn span { display: none; }
            .account-link { flex: 1; justify-content: center; padding: 4px; } .account-link span { display: none; }
            h2 { font-size: 22px; } .card { margin-bottom: 16px; } .card-media img, .card-media video { max-height: 70vh; }
            .searchbar, .filter-panel { max-width: none; }
            .profile-container { padding: 16px; } .profile-header { flex-wrap: wrap; gap: 14px; } .profile-posts-grid { gap: 4px; }
            .chat-layout { height: calc(100dvh - 235px); border-radius: 12px; }
            .inbox-panel { width: 100%; border-left: 0; }
            .chat-layout.has-peer .inbox-panel, .chat-layout:not(.has-peer) .chat-panel { display: none; }
            .back-btn { display: flex; } .bubble { max-width: 84%; } .chat-messages { padding: 12px; } .chat-inputs { padding: 10px; gap: 6px; }
            .vbub { min-width: 180px; } .video-container video { width: 44vw; height: 28vh; }
        }

        .action-btn.liked .ico { fill: currentColor; }
        .account-link.active { background: var(--input-bg); color: var(--primary); }
        .profile-container { overflow: hidden; }
        .banner-wrap { position: relative; height: 160px; margin: -28px -28px 0; background: linear-gradient(135deg, var(--primary), #fb923c); }
        .banner-img { position: absolute; inset: 0; background-size: cover; background-position: center; }
        .banner-wrap::after { content: ''; position: absolute; inset: 0; background: linear-gradient(to bottom, transparent 30%, var(--card-bg) 100%); }
        .profile-header { position: relative; z-index: 1; margin-top: -56px; align-items: flex-start; }
        .profile-info { padding-top: 62px; } .profile-avatar { border: 4px solid var(--card-bg); }
        .edit-top { flex-wrap: wrap; }
        .lightbox { position: fixed; inset: 0; background: rgba(0,0,0,.85); z-index: 3000; display: flex; align-items: center; justify-content: center; opacity: 0; transition: opacity .2s; cursor: zoom-out; }
        .lightbox img { max-width: 92vw; max-height: 88vh; border-radius: 18px; transform: scale(.6); transition: transform .25s; }
        .lightbox.on { opacity: 1; } .lightbox.on img { transform: scale(1); }
        .callpanel { display: flex; flex-direction: column; gap: 10px; padding: 12px; background: var(--bg); border-bottom: 1px solid var(--border); }
        .call-tiles { display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }
        .ctile { position: relative; width: 210px; height: 130px; border-radius: 14px; background: #1b1b1b; overflow: hidden; display: flex; align-items: center; justify-content: center; color: #fff; }
        .ctile video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; display: none; }
        .cav { width: 58px; height: 58px; font-size: 22px; }
        .cname { position: absolute; bottom: 6px; right: 8px; font-size: 12px; background: rgba(0,0,0,.6); padding: 1px 8px; border-radius: 8px; }
        .cmic { position: absolute; top: 6px; left: 8px; width: 24px; height: 24px; border-radius: 50%; background: #dc2626; display: none; align-items: center; justify-content: center; }
        .cmic .ico { width: 14px; height: 14px; }
        .call-status { text-align: center; font-size: 14px; color: var(--muted); }
        .call-bar { display: flex; gap: 12px; justify-content: center; }
        .cbtn, .cbubble { background-image: linear-gradient(180deg, rgba(255,255,255,.3) 0, rgba(255,255,255,0) 55%); box-shadow: inset 0 1px 0 rgba(255,255,255,.45), 0 3px 8px rgba(0,0,0,.15); }
        .cbtn { width: 48px; height: 48px; border-radius: 50%; border: 1px solid var(--border); background-color: var(--input-bg); color: var(--text); cursor: pointer; display: flex; align-items: center; justify-content: center; padding: 0; }
        .cbtn.off { background-color: var(--text); color: var(--bg); } .cbtn.end, .cbtn.ok { color: #fff; border-color: transparent; } .cbtn.end { background-color: #dc2626; } .cbtn.ok { background-color: #16a34a; }
        .incoming { position: fixed; top: 16px; left: 50%; transform: translateX(-50%); z-index: 2000; background: var(--card-bg); border: 1px solid var(--border); border-radius: 18px; padding: 14px 20px; display: flex; flex-direction: column; gap: 10px; align-items: center; box-shadow: 0 10px 30px rgba(0,0,0,.3); }
        .cbubble { position: fixed; left: 18px; bottom: 24px; width: 60px; height: 60px; border-radius: 50%; border: 0; background-color: #16a34a; color: #fff; display: none; align-items: center; justify-content: center; cursor: pointer; z-index: 1500; animation: ring 1.6s infinite; }
        .cbubble .ico { width: 28px; height: 28px; }
        @keyframes ring { 0% { box-shadow: 0 0 0 0 rgba(34,197,94,.7); } 70% { box-shadow: 0 0 0 16px rgba(34,197,94,0); } 100% { box-shadow: 0 0 0 0 rgba(34,197,94,0); } }
        @media (max-width: 800px) { .banner-wrap { margin: -16px -16px 0; height: 130px; } .cbubble { bottom: 92px; } .ctile { width: 44vw; height: 110px; } }
    </style>
</head>
<body>

    <aside class="sidebar" id="sidebar">
        <div class="brand">Voxa</div>
        <button class="nav-link active" onclick="showPage('home')">الرئيسية</button>
        <button class="nav-link" onclick="showPage('long')">الفيديوهات</button>
        <button class="nav-link" onclick="showPage('reel')">ريلز</button>
        <button class="nav-link" onclick="showPage('photo')">الصور</button>
        <button class="nav-link" onclick="showPage('msgs')">الرسائل</button>
        <button class="nav-link" onclick="showPage('notifs')">الإشعارات</button>
        <button class="nav-link" onclick="showPage('settings')">الإعدادات</button>
        <div class="spacer"></div>
        <button class="create-btn" onclick="showPage('new')">+ إنشاء</button>
        <div class="divider"></div>
        <div class="account-link" onclick="showPage('profile')">
            <div class="account-avatar" id="acc-avatar"></div>
            <span id="acc-name">الحساب</span>
        </div>
    </aside>

    <main class="main-content" id="main-content"></main>

    <script>
        const $ = s => document.querySelector(s);
        const ICON = {
            home: '<path d="M3 10.5L12 3l9 7.5V20a1 1 0 01-1 1h-5v-6H9v6H4a1 1 0 01-1-1z"/>',
            notifs: '<path d="M6 17v-6a6 6 0 0112 0v6l2 2H4z"/><path d="M10 21a2 2 0 004 0"/>',
            long: '<rect x="2.5" y="5" width="19" height="14" rx="4"/><path d="M10 9.5v5l4.5-2.5z"/>',
            reel: '<rect x="3" y="3" width="18" height="18" rx="5"/><path d="M3 9h18M9 3l3 6M15 3l3 6"/><path d="M10.5 12.5v5l4-2.5z"/>',
            photo: '<rect x="3" y="3" width="18" height="18" rx="5"/><circle cx="9" cy="9" r="1.8"/><path d="M3.5 17l5.5-5 4 4 3-3 4.5 4.5"/>',
            msgs: '<path d="M21 3L10 14"/><path d="M21 3l-7 18-4-7-7-4z"/>',
            profile: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/>',
            settings: '<path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/>',
            plus: '<path d="M12 5v14M5 12h14"/>', minus: '<path d="M5 12h14"/>', x: '<path d="M6 6l12 12M18 6L6 18"/>',
            check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>', back: '<path d="M9 6l6 6-6 6"/>',
            phone: '<path d="M5 4h4l2 5-2.5 1.5a11 11 0 005 5L15 13l5 2v4a2 2 0 01-2 2A16 16 0 013 6a2 2 0 012-2z"/>',
            hangup: '<g transform="rotate(135 12 12)"><path d="M5 4h4l2 5-2.5 1.5a11 11 0 005 5L15 13l5 2v4a2 2 0 01-2 2A16 16 0 013 6a2 2 0 012-2z"/></g>',
            video: '<rect x="2.5" y="6" width="13" height="12" rx="3"/><path d="M15.5 10.5l6-3.5v10l-6-3.5z"/>',
            mic: '<rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0014 0M12 18v3"/>',
            stop: '<rect x="6" y="6" width="12" height="12" rx="2.5"/>',
            clip: '<path d="M20 11.5l-7.5 7.5a5 5 0 01-7-7L13 4.5a3.3 3.3 0 014.7 4.7L10 17a1.7 1.7 0 01-2.4-2.4L14.5 7.7"/>',
            send: '<path d="M21 3L10 14"/><path d="M21 3l-7 18-4-7-7-4z"/>',
            trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
            comment: '<path d="M21 12a8.5 8.5 0 01-12.2 7.6L3.5 21l1.4-4.6A8.5 8.5 0 1121 12z"/>',
            heart: '<path d="M12 21s-8-5-8-11a4.5 4.5 0 018-2.8A4.5 4.5 0 0120 10c0 6-8 11-8 11z"/>',
            micoff: '<rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0014 0M12 18v3M4 4l16 16"/>',
            videooff: '<rect x="2.5" y="6" width="13" height="12" rx="3"/><path d="M15.5 10.5l6-3.5v10l-6-3.5zM3 3l18 18"/>',
            play: '<path d="M8 5.5v13l11-6.5z"/>', pause: '<path d="M8 5v14M16 5v14"/>'
        };
        const ic = n => '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">' + ICON[n] + '</svg>';
        function cbSet(b, n) { b.innerHTML = ic('comment') + '<span class="cnt">' + (+n || 0) + '</span>'; }
        function stepper(ph, onchange) {
            const w = el('div', 'stepper'), i = el('input'); i.type = 'number'; i.min = 0; i.placeholder = ph; i.inputMode = 'numeric';
            const mk = (n, d) => { const b = el('button', 'step-btn'); b.type = 'button'; b.setAttribute('aria-label', d > 0 ? 'زيادة' : 'نقصان'); b.innerHTML = ic(n); b.onclick = () => { i.value = Math.max(0, (+i.value || 0) + d); onchange(); }; return b; };
            w.append(mk('minus', -1), i, mk('plus', 1));
            return { w, i };
        }
        function withClear(inp) {
            const w = el('div', 'sbox'), b = el('button', 'sclear'); b.type = 'button'; b.setAttribute('aria-label', 'مسح'); b.innerHTML = ic('x');
            const upd = () => { b.style.display = inp.value ? 'flex' : 'none'; };
            inp.addEventListener('input', upd);
            b.onclick = () => { inp.value = ''; inp.dispatchEvent(new Event('input')); inp.focus(); };
            w.append(inp, b); upd();
            return w;
        }
        function setupMobile() {
            document.querySelectorAll('.nav-link').forEach(n => {
                const k = (n.getAttribute('onclick').match(/'(\w+)'/) || [])[1]; if (!k || !ICON[k]) return;
                const t = n.textContent; n.dataset.k = k; n.title = t; n.innerHTML = ic(k) + '<span>' + t + '</span>';
            });
            const cr = document.querySelector('.create-btn'); cr.innerHTML = ic('plus') + '<span>إنشاء</span>'; cr.setAttribute('aria-label', 'إنشاء');
            const top = document.createElement('div'); top.className = 'mtop'; top.innerHTML = '<span class="mbrand">Voxa</span><div class="mtop-btns"></div>';
            [['photo', 'الصور'], ['notifs', 'الإشعارات'], ['settings', 'الإعدادات']].forEach(([k, t]) => {
                const b = el('button', 'mtb'); b.innerHTML = ic(k); b.title = t; b.setAttribute('aria-label', t); b.onclick = () => showPage(k); top.querySelector('.mtop-btns').append(b);
            });
            $('#sidebar').after(top);
        }
        const el = (t, c, x) => { const e = document.createElement(t); if(c) e.className = c; if(x != null) e.textContent = x; return e; };
        
        async function api(p, b) {
            const r = await fetch(p, {
                method: b ? 'POST' : 'GET',
                headers: { 'Content-Type': 'application/json', 'X-Voxa': '1' },
                body: b ? JSON.stringify(b) : undefined
            });
            let d = {};
            try { d = await r.json(); } catch (e) {}
            return { ok: r.ok, d };
        }

        let me = null, peer = null, timer = null, callPollTimer = null;
        let navs = document.querySelectorAll('.nav-link');
        let currentSearch = '';
        let localStream = null, isCalling = false;

        function applySettings() {
            const theme = localStorage.getItem('voxa_theme') || 'light';
            const primaryColor = localStorage.getItem('voxa_color') || '#c2410c';
            if (theme === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
            else document.documentElement.removeAttribute('data-theme');
            document.documentElement.style.setProperty('--primary', primaryColor);
        }
        applySettings();

        function renderAvatar(targetEl, userObj) {
            targetEl.innerHTML = '';
            if (userObj.avatar) {
                const img = el('img'); 
                img.src = userObj.avatar;
                targetEl.append(img);
            } else {
                targetEl.textContent = (userObj.display_name || userObj.name || 'U').charAt(0).toUpperCase();
            }
        }

        async function boot() {
            const r = await api('/api/me');
            if (r.ok) {
                me = r.d;
                $('#acc-name').textContent = me.display_name;
                renderAvatar($('#acc-avatar'), me);
                $('#sidebar').style.display = 'flex';
                showPage('home');
                startCallPolling();
            } else {
                const i = (await api('/api/info')).d;
                auth(i.invite);
            }
        }

        function auth(inv) {
            $('#sidebar').style.display = 'none';
            const main = $('#main-content');
            main.innerHTML = '';
            
            const container = el('div', 'auth-container');
            const card = el('div', 'auth-card');
            
            card.append(el('div', 'brand', 'Voxa'));
            card.append(el('div', 'muted', 'مرحباً بك! سجل الدخول أو أنشئ حسابك لبدء المشاركة'));
            
            const dispName = el('input'); dispName.placeholder = 'الاسم الظاهر';
            const userName = el('input'); userName.placeholder = 'اسم المستخدم (إنجليزي وأرقام)'; userName.autocapitalize = 'none';
            const p = el('input'); p.type = 'password'; p.placeholder = 'كلمة السر';
            const v = el('input'); v.placeholder = 'كود الدعوة';
            const e = el('div'); e.id = 'err'; e.style.color = '#dc2626';
            
            let isRegisterMode = false;
            dispName.style.display = 'none';
            if (!inv) v.style.display = 'none';

            const submitBtn = el('button', 'btn-primary', 'تسجيل الدخول');
            const switchBtn = el('button', 'btn-secondary', 'ليس لديك حساب؟ إنشاء حساب جديد');

            switchBtn.onclick = () => {
                isRegisterMode = !isRegisterMode;
                dispName.style.display = isRegisterMode ? 'block' : 'none';
                if (inv) v.style.display = isRegisterMode ? 'block' : 'none';
                submitBtn.textContent = isRegisterMode ? 'إنشاء الحساب' : 'تسجيل الدخول';
                switchBtn.textContent = isRegisterMode ? 'لديك حساب بالفعل؟ تسجيل الدخول' : 'ليس لديك حساب؟ إنشاء حساب جديد';
                e.textContent = '';
            };

            submitBtn.onclick = async () => {
                e.textContent = '';
                const route = isRegisterMode ? '/api/register' : '/api/login';
                const r = await api(route, { 
                    username: userName.value.trim().toLowerCase(), 
                    display_name: dispName.value.trim(), 
                    pw: p.value, 
                    invite: v.value 
                });
                if (r.ok) boot(); 
                else e.textContent = r.d.error || 'حدث خطأ في البيانات';
            };
            
            card.append(dispName, userName, p);
            if (inv) card.append(v);
            card.append(e, submitBtn, switchBtn);
            container.append(card);
            main.append(container);
        }

        function setActiveNav(k) {
            navs.forEach(n => n.classList.remove('active'));
            const al = document.querySelector('.account-link'); if (al) al.classList.toggle('active', k === 'profile');
            const btn = Array.from(navs).find(n => n.getAttribute('onclick')?.includes(`'${k}'`));
            if (btn) btn.classList.add('active');
        }

        function showPage(k, targetUserId = null) {
            clearInterval(timer);
            setActiveNav(k);
            const main = $('#main-content');
            main.innerHTML = '';
            
            if (k === 'profile') return renderProfile(main, targetUserId);
            if (k === 'msgs') return renderMsgs(main);
            if (k === 'notifs') return renderNotifs(main);
            if (k === 'new') return renderCreate(main);
            if (k === 'settings') return renderSettings(main);
            renderFeed(main, k);
        }

        function renderSettings(main) {
            main.append(el('h2', '', 'الإعدادات'));
            const card = el('div', 'settings-card');

            const row1 = el('div', 'settings-row');
            row1.append(el('span', '', 'مظهر التطبيق'));
            const themeSel = el('select');
            [['light', 'الوضع الفاتح'], ['dark', 'الوضع المظلم']].forEach(([v, t]) => {
                const opt = el('option', '', t); opt.value = v;
                if ((localStorage.getItem('voxa_theme') || 'light') === v) opt.selected = true;
                themeSel.append(opt);
            });
            themeSel.onchange = () => {
                localStorage.setItem('voxa_theme', themeSel.value);
                applySettings();
            };
            row1.append(themeSel);

            const row2 = el('div', 'settings-row');
            row2.append(el('span', '', 'لون التطبيق الرئيسي'));
            const colorInp = el('input'); colorInp.type = 'color';
            colorInp.value = localStorage.getItem('voxa_color') || '#c2410c';
            colorInp.onchange = () => {
                localStorage.setItem('voxa_color', colorInp.value);
                applySettings();
            };
            row2.append(colorInp);

            card.append(row1, row2);
            main.append(card);
        }

        function buildEdit(u, avatarInp, toggleBtn, bannerInp) {
            const p = el('div', 'edit-panel');
            const top = el('div', 'edit-top');
            const pic = el('div', 'avatar-circle'); pic.style.cssText = 'width:56px;height:56px;font-size:22px'; renderAvatar(pic, u);
            const chg = el('button', 'small-btn', 'تغيير الصورة'); chg.onclick = () => avatarInp.click();
            const chb = el('button', 'small-btn light', 'تغيير البانر'); chb.onclick = () => bannerInp.click();
            top.append(pic, chg, chb);
            const l1 = el('label', '', 'الاسم الظاهر'), n = el('input'); n.type = 'text'; n.value = u.display_name; n.maxLength = 40;
            const l2 = el('label', '', 'اسم المستخدم (يوزر نيم)'), un = el('input'); un.type = 'text'; un.value = u.username; un.maxLength = 20; un.autocapitalize = 'none';
            const st = el('div', 'small'), row = el('div', 'btn-row');
            const save = el('button', 'small-btn', 'حفظ التغييرات'), cancel = el('button', 'small-btn light', 'إلغاء');
            row.append(save, cancel);
            save.onclick = async () => {
                const r = await api('/api/update_profile', { display_name: n.value, username: un.value });
                if (r.ok) { me.display_name = r.d.display_name; me.username = r.d.username; $('#acc-name').textContent = me.display_name; showPage('profile'); }
                else { st.style.color = '#dc2626'; st.textContent = r.d.error || 'تعذر الحفظ'; }
            };
            cancel.onclick = () => { p.style.display = 'none'; };
            toggleBtn.onclick = () => { p.style.display = p.style.display === 'flex' ? 'none' : 'flex'; };
            p.append(el('b', '', 'تعديل الملف الشخصي'), top, l1, n, l2, un, st, row);
            return p;
        }

        async function renderProfile(main, uid) {
            const url = uid ? `/api/profile?id=${uid}` : `/api/profile`;
            const r = await api(url);
            
            if (!r.ok) {
                main.append(el('p', 'muted', r.d.error || 'تعذر تحميل الملف الشخصي'));
                return;
            }

            const u = r.d; let pendingEdit = null;
            const container = el('div', 'profile-container');
            const bwrap = el('div', 'banner-wrap'), bimg = el('div', 'banner-img');
            if (u.banner) bimg.style.backgroundImage = 'url("' + u.banner + '")';
            bwrap.append(bimg);
            const bannerInp = el('input'); bannerInp.type = 'file'; bannerInp.accept = 'image/*'; bannerInp.style.display = 'none';
            bannerInp.onchange = async () => {
                const f = bannerInp.files[0]; if (!f) return;
                const res = await fetch('/banner_upload', { method: 'POST', headers: { 'X-Voxa': '1', 'Content-Type': f.type }, body: f });
                let d = {}; try { d = await res.json(); } catch (e) {}
                if (res.ok) { u.banner = d.banner; bimg.style.backgroundImage = 'url("' + d.banner + '")'; } else alert(d.error || 'فشل رفع البانر');
            };
            container.append(bwrap, bannerInp);
            
            const header = el('div', 'profile-header');
            const avatar = el('div', 'profile-avatar');
            renderAvatar(avatar, u);

            const avatarInp = el('input'); avatarInp.type = 'file'; avatarInp.accept = 'image/*'; avatarInp.style.display = 'none';
            avatar.onclick = () => { if (u.avatar) zoomImg(u.avatar); };
            if (u.is_me) {
                
                avatarInp.onchange = async () => {
                    const file = avatarInp.files[0];
                    if (!file) return;
                    const res = await fetch('/avatar_upload', {
                        method: 'POST', headers: { 'X-Voxa': '1', 'Content-Type': file.type }, body: file
                    });
                    const d = await res.json();
                    if (res.ok) {
                        u.avatar = d.avatar;
                        renderAvatar(avatar, u);
                        renderAvatar($('#acc-avatar'), u);
                    } else alert(d.error || 'فشل رفع الصورة');
                };
            }
            
            const info = el('div', 'profile-info');
            info.append(el('div', '', u.display_name));
            info.append(el('div', 'muted', '@' + u.username));
            
            const stats = el('div', 'profile-stats');
            stats.append(stat(u.posts_count, 'منشورات'));
            const followersEl = stat(u.followers, 'متابِعين');
            stats.append(followersEl);
            stats.append(stat(u.following, 'يتابع'));
            info.append(stats);

            if (!u.is_me) {
                const followBtn = el('button', 'follow-btn' + (u.is_following ? ' following' : ''), u.is_following ? 'تتابعه' : 'متابعة');
                followBtn.onclick = async () => {
                    const res = await api('/api/follow', { id: u.id });
                    if (res.ok) {
                        u.is_following = res.d.is_following;
                        followBtn.className = 'follow-btn' + (u.is_following ? ' following' : '');
                        followBtn.textContent = u.is_following ? 'تتابعه' : 'متابعة';
                        followersEl.firstChild.textContent = res.d.followers;
                    }
                };
                info.append(followBtn);
            } else {
                const row = el('div', 'btn-row');
                const editBtn = el('button', 'follow-btn', 'تعديل الملف');
                const logoutBtn = el('button', 'follow-btn following', 'تسجيل الخروج');
                logoutBtn.onclick = logout;
                row.append(editBtn, logoutBtn); info.append(row);
                pendingEdit = buildEdit(u, avatarInp, editBtn, bannerInp);
            }

            header.append(avatar, avatarInp, info);
            container.append(header);
            if (pendingEdit) container.append(pendingEdit);

            container.append(el('h3', '', 'المنشورات'));
            if (!u.posts || !u.posts.length) {
                container.append(el('p', 'muted', 'لا توجد منشورات.'));
            } else {
                const grid = el('div', 'profile-posts-grid');
                u.posts.forEach(p => {
                    const item = el('div', 'grid-item');
                    if (p.kind === 'photo') {
                        const img = el('img'); img.src = '/media/' + p.file;
                        item.append(img);
                    } else {
                        const vid = el('video'); vid.src = '/media/' + p.file;
                        item.append(vid);
                    }
                    grid.append(item);
                });
                container.append(grid);
            }

            main.append(container);
        }

        const FILT = { type: 'all', dmin: 0, dmax: 0 };
        const fmt = s => { s = Math.round(s || 0); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
        function zoomImg(src) {
            const o = el('div', 'lightbox'), i = el('img'); i.src = src; i.alt = 'صورة مكبرة'; o.append(i);
            const k = e => { if (e.key === 'Escape') close(); };
            const close = () => { o.classList.remove('on'); setTimeout(() => o.remove(), 250); document.removeEventListener('keydown', k); };
            document.addEventListener('keydown', k); o.onclick = close; document.body.append(o);
            requestAnimationFrame(() => o.classList.add('on'));
        }
        function stat(n, l) { const d = el('div', 'muted'); d.append(el('b', '', n), ' ' + l); return d; }
        function getDur(file) {
            return new Promise(res => {
                const v = document.createElement('video'); v.preload = 'metadata';
                v.onloadedmetadata = () => { URL.revokeObjectURL(v.src); res(v.duration || 0); };
                v.onerror = () => res(0);
                v.src = URL.createObjectURL(file);
            });
        }

        function postCard(p) {
            const c = el('div', 'card');
            const header = el('div', 'card-header');
            header.onclick = () => showPage('profile', p.uid);
            const av = el('div', 'avatar-circle'); renderAvatar(av, { display_name: p.name, avatar: p.avatar });
            const nameDiv = el('div'); nameDiv.append(el('div', '', p.name), el('div', 'muted', '@' + p.username));
            header.append(av, nameDiv); c.append(header);

            const media = el('div', 'card-media');
            const m = p.kind === 'photo' ? el('img') : el('video');
            if (p.kind !== 'photo') { m.controls = true; m.playsInline = true; m.preload = 'metadata'; }
            m.src = '/media/' + p.file; media.append(m);
            if (p.kind !== 'photo' && p.dur) media.append(el('span', 'dur-badge', fmt(p.dur)));
            c.append(media);

            const actions = el('div', 'card-actions');
            const l = el('button', 'action-btn');
            const likeSet = () => { l.className = 'action-btn' + (p.liked ? ' liked' : ''); l.innerHTML = ic('heart') + '<span class="cnt">' + p.lk + '</span>'; l.setAttribute('aria-label', 'إعجاب'); };
            likeSet();
            l.onclick = async () => { const q = await api('/api/like', { id: p.id }); if (q.ok) { p.liked = !p.liked; p.lk = q.d.lk; likeSet(); } };
            const cb = el('button', 'action-btn'); cbSet(cb, p.cc || 0);
            actions.append(l, cb); c.append(actions);

            const cap = el('div', 'card-caption'); cap.append(el('b', '', p.name), ' ' + (p.caption || ''), );
            c.append(cap);

            const box = el('div', 'cbox'), list = el('div'), form = el('div', 'cform');
            const inp = el('input'); inp.placeholder = 'اكتب تعليقاً...'; inp.maxLength = 300;
            const send = el('button', 'small-btn', 'نشر');
            form.append(inp, send); box.append(list, form); c.append(box);
            async function loadC() {
                const r = await api('/api/comments?pid=' + p.id);
                list.textContent = '';
                (r.d || []).forEach(x => { const row = el('div', 'crow'); row.append(el('b', '', x.name), ' ' + x.text); list.append(row); });
                if (!r.d || !r.d.length) list.append(el('div', 'muted small', 'كن أول من يعلّق'));
                p.cc = (r.d || []).length; cbSet(cb, p.cc);
            }
            cb.onclick = () => { if (box.style.display === 'flex') box.style.display = 'none'; else { box.style.display = 'flex'; loadC(); } };
            const post = async () => { const t = inp.value.trim(); if (!t) return; inp.value = ''; await api('/api/comment', { pid: p.id, text: t }); loadC(); };
            send.onclick = post; inp.onkeypress = e => { if (e.key === 'Enter') post(); };
            return c;
        }

        async function renderFeed(main, k) {
            const titles = { home: 'الرئيسية', long: 'الفيديوهات الطويلة', reel: 'ريلز', photo: 'الصور' };
            main.append(el('h2', '', titles[k] || 'الرئيسية'));
            const bar = el('div', 'searchbar'), inp = el('input'); inp.type = 'search'; inp.placeholder = 'ابحث عن أي حاجة...'; inp.value = currentSearch;
            const fb = el('button', 'filter-btn'); fb.setAttribute('aria-label', 'تحديد نوع البحث');
            fb.innerHTML = '<svg class="nav-icon" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="5"/><circle cx="8.5" cy="8.5" r="1.2"/><circle cx="15.5" cy="8.5" r="1.2"/><circle cx="8.5" cy="15.5" r="1.2"/><circle cx="15.5" cy="15.5" r="1.2"/></svg>';
            bar.append(withClear(inp), fb); main.append(bar);
            const panel = el('div', 'filter-panel'); main.append(panel);
            panel.append(el('div', 'muted', 'ابحث في:'));
            const chips = el('div', 'chips'), durRow = el('div', 'dur-row');
            [['all', 'كل حاجة'], ['user', 'يوزر نيم'], ['video', 'فيديو'], ['long', 'فيديو طويل'], ['reel', 'ريلز'], ['photo', 'صورة']].forEach(([v, t]) => {
                const b = el('button', 'chip' + (FILT.type === v ? ' on' : ''), t);
                b.onclick = () => { FILT.type = v; chips.querySelectorAll('.chip').forEach(x => x.classList.remove('on')); b.classList.add('on'); sync(); load(); };
                chips.append(b);
            });
            const upd = () => { FILT.dmin = Math.round((+mn.value || 0) * 60); FILT.dmax = Math.round((+mx.value || 0) * 60); load(); };
            const s1 = stepper('دقائق', upd), s2 = stepper('دقائق', upd), mn = s1.i, mx = s2.i;
            durRow.append(el('span', '', 'المدة: أكثر من'), s1.w, el('span', '', 'وأقل من'), s2.w);
            [mn, mx].forEach(i => i.oninput = upd);
            panel.append(chips, durRow);
            const sync = () => { durRow.style.display = ['video', 'long', 'reel'].includes(FILT.type) ? 'flex' : 'none'; if (durRow.style.display === 'none') { FILT.dmin = FILT.dmax = 0; mn.value = mx.value = ''; } };
            sync();
            fb.onclick = () => { panel.style.display = panel.style.display === 'flex' ? 'none' : 'flex'; };
            const listBox = el('div'); listBox.style.cssText = 'width:100%;display:flex;flex-direction:column;align-items:center';
            main.append(listBox);
            let tm = null, seq = 0;
            async function load() {
                const my = ++seq;
                const t = FILT.type;
                const kind = t === 'video' ? 'anyvideo' : t === 'long' ? 'video' : (t === 'reel' || t === 'photo') ? t : (k === 'long' ? 'video' : k);
                const qs = new URLSearchParams({ kind, q: inp.value.trim(), by: t === 'user' ? 'user' : 'all', dmin: FILT.dmin, dmax: FILT.dmax });
                const r = await api('/api/posts?' + qs);
                if (my !== seq) return;
                listBox.textContent = '';
                if (!r.d || !r.d.length) { listBox.append(el('p', 'muted', 'لا توجد نتائج.')); return; }
                r.d.forEach(p => listBox.append(postCard(p)));
            }
            inp.oninput = () => { currentSearch = inp.value; clearTimeout(tm); tm = setTimeout(load, 300); };
            load();
        }

        function renderCreate(main) {
            main.append(el('h2', '', 'إنشاء منشور جديد'));
            const b = el('div', 'card');
            b.style.padding = '20px';
            b.style.gap = '12px';
            
            const s = el('select');
            [['photo', 'صورة'], ['video', 'فيديو طويل'], ['reel', 'ريلز']].forEach(([v, t]) => { const o = el('option', '', t); o.value = v; s.append(o) });
            
            const f = el('input'); f.type = 'file'; f.accept = 'image/*,video/mp4,video/webm,video/quicktime';
            const cap = el('input'); cap.placeholder = 'وصف المنشور...'; cap.maxLength = 300;
            const st = el('div', 'muted');
            const go = el('button', 'create-btn', 'نشر');
            
            go.onclick = async () => {
                const file = f.files[0];
                if (!file) { st.textContent = 'اختر ملفاً أولاً'; return; }
                if ((s.value === 'photo') !== file.type.startsWith('image/')) { st.textContent = 'نوع الملف لا يناسب نوع المنشور'; return; }
                st.textContent = 'جاري الرفع...'; go.disabled = true;
                const dur = file.type.startsWith('video/') ? Math.round(await getDur(file)) : 0;
                
                const r = await fetch('/upload?kind=' + s.value + '&dur=' + dur + '&caption=' + encodeURIComponent(cap.value), { 
                    method: 'POST', headers: { 'X-Voxa': '1', 'Content-Type': file.type }, body: file 
                });
                if (r.ok) showPage(s.value === 'photo' ? 'photo' : s.value);
                else { let d = {}; try { d = await r.json(); } catch (e) {} st.textContent = d.error || 'فشل الرفع'; go.disabled = false; }
            };
            
            b.append(s, f, cap, go, st);
            main.append(b);
        }

        async function renderNotifs(main) {
            main.append(el('h2', '', 'الإشعارات'));
            const r = await api('/api/notifs');
            if (!r.d || !r.d.length) {
                main.append(el('p', 'muted', 'لا توجد إشعارات حتى الآن'));
                return;
            }
            r.d.forEach(n => {
                const c = el('div', 'card');
                c.style.padding = '16px';
                c.textContent = n.name + ' أعجب بـ' + ({ photo: ' صورتك', video: ' فيديوك', reel: ' الريلز بتاعك' }[n.kind]);
                main.append(c);
            });
        }

        function voiceBubble(m) {
            const w = el('div', 'vbub'), a = new Audio(m.file); a.preload = 'metadata';
            const b = el('button', 'vbtn'); b.innerHTML = ic('play'); const wave = el('div', 'wave'), tm = el('span', 'vtime', fmt(+m.text || 0));
            const bars = [];
            for (let i = 0; i < 28; i++) { const x = el('i'); x.style.height = (6 + Math.abs(Math.sin(i * 1.7 + m.id)) * 18) + 'px'; bars.push(x); wave.append(x); }
            const paint = f => bars.forEach((x, i) => x.classList.toggle('on', i / bars.length < f));
            b.onclick = () => { a.paused ? a.play() : a.pause(); };
            a.onplay = () => b.innerHTML = ic('pause'); a.onpause = () => b.innerHTML = ic('play');
            a.onended = () => { b.innerHTML = ic('play'); paint(0); tm.textContent = fmt(+m.text || 0); };
            a.ontimeupdate = () => { const d = +m.text || (isFinite(a.duration) ? a.duration : 0); if (d) paint(a.currentTime / d); tm.textContent = fmt(a.currentTime); };
            wave.onclick = e => { const d = +m.text || a.duration; if (!isFinite(d)) return; const r = wave.getBoundingClientRect(); a.currentTime = d * (1 - (e.clientX - r.left) / r.width); };
            w.append(b, wave, tm);
            return w;
        }

        async function renderMsgs(main) {
            main.append(el('h2', '', 'الرسائل والمكالمات'));
            const w = el('div', 'chat-layout'), inboxPanel = el('div', 'inbox-panel');
            inboxPanel.append(el('div', 'inbox-header', me.display_name));
            const search = el('input', 'inbox-search'); search.type = 'search'; search.placeholder = 'ابحث عن أشخاص...';
            const inboxList = el('div', 'inbox-list');
            inboxPanel.append(withClear(search), inboxList);
            const chatPanel = el('div', 'chat-panel');
            w.append(inboxPanel, chatPanel); main.append(w); if (peer) w.classList.add('has-peer');

            async function loadUsers() {
                const users = (await api('/api/users?q=' + encodeURIComponent(search.value.trim()))).d || [];
                inboxList.textContent = '';
                if (!users.length) inboxList.append(el('p', 'muted', 'لا يوجد أشخاص'));
                users.forEach(x => {
                    const item = el('div', 'inbox-item' + (peer === x.id ? ' active' : ''));
                    const av = el('div', 'avatar-circle'); renderAvatar(av, x);
                    const info = el('div'); info.append(el('div', '', x.display_name), el('div', 'muted', '@' + x.username));
                    item.append(av, info);
                    item.onclick = () => { peer = x.id; showPage('msgs'); };
                    inboxList.append(item);
                });
            }
            let st = null; search.oninput = () => { clearTimeout(st); st = setTimeout(loadUsers, 250); };
            await loadUsers();

            if (!peer) { chatPanel.append(el('div', 'muted', 'اختر محادثة لبدء الدردشة والمكالمات')); chatPanel.style.cssText = 'align-items:center;justify-content:center'; return; }
            const tu = (await api('/api/profile?id=' + peer)).d || {};
            const chatHeader = el('div', 'chat-header'), uInfo = el('div', 'chat-user-info');
            uInfo.onclick = () => showPage('profile', peer);
            const av = el('div', 'avatar-circle'); renderAvatar(av, tu);
            const hInfo = el('div'); hInfo.append(el('div', '', tu.display_name || ''), el('div', 'muted', '@' + (tu.username || '')));
            uInfo.append(av, hInfo);
            const callBtns = el('div', 'chat-call-btns');
            const vc = el('button', 'call-btn'); vc.innerHTML = ic('phone'); vc.title = 'مكالمة صوتية'; vc.onclick = () => initiateCall(peer, false, tu);
            const vv = el('button', 'call-btn'); vv.innerHTML = ic('video'); vv.title = 'مكالمة فيديو'; vv.onclick = () => initiateCall(peer, true, tu);
            callBtns.append(vc, vv);
            const back = el('button', 'icon-btn back-btn'); back.innerHTML = ic('back'); back.setAttribute('aria-label', 'رجوع'); back.onclick = () => { peer = null; showPage('msgs'); };
            chatHeader.append(back, uInfo, callBtns);

            const log = el('div', 'chat-messages'), inputs = el('div', 'chat-inputs');
            const attach = el('button', 'icon-btn'), file = el('input'); file.type = 'file'; file.accept = 'image/*'; file.style.display = 'none';
            const mic = el('button', 'icon-btn'), textInp = el('input'); textInp.type = 'text'; textInp.placeholder = 'اكتب رسالة...';
            const sendBtn = el('button', 'icon-btn send');
            attach.innerHTML = ic('clip'); mic.innerHTML = ic('mic'); sendBtn.innerHTML = ic('send');
            attach.setAttribute('aria-label', 'إرفاق صورة'); mic.setAttribute('aria-label', 'رسالة صوتية'); sendBtn.setAttribute('aria-label', 'إرسال');
            inputs.append(attach, file, mic, textInp, sendBtn);
            chatPanel.append(chatHeader, log, inputs);
            chatHeaderEl = chatHeader; chatPeerId = peer; dockCall();

            const myPeer = peer; let last = '';
            async function load() {
                const r = await api('/api/msgs?with=' + myPeer);
                if (!r.ok || myPeer !== peer) return;
                const s = JSON.stringify(r.d); if (s === last) return; last = s;
                log.textContent = '';
                r.d.forEach(m => {
                    const row = el('div', 'msg-row ' + (m.mine ? 'mine' : 'other')), b = el('div', 'bubble');
                    if (m.kind === 'audio') b.append(voiceBubble(m));
                    else if (m.kind === 'image') { const i = el('img', 'chat-img'); i.src = m.file; b.append(i); }
                    else b.textContent = m.text;
                    row.append(b);
                    if (m.mine) { const d = el('button', 'msg-del'); d.innerHTML = ic('trash'); d.setAttribute('aria-label', 'حذف'); d.title = 'حذف'; d.onclick = async () => { await api('/api/del', { id: m.id }); last = ''; load(); }; row.append(d); }
                    log.append(row);
                });
                log.scrollTop = log.scrollHeight;
            }
            const sendMsg = async () => { const t = textInp.value.trim(); if (!t) return; textInp.value = ''; await api('/api/send', { to: myPeer, text: t }); last = ''; load(); };
            sendBtn.onclick = sendMsg; textInp.onkeypress = e => { if (e.key === 'Enter') sendMsg(); };
            attach.onclick = () => file.click();
            file.onchange = async () => { const f = file.files[0]; if (!f) return; await fetch('/chat_upload?to=' + myPeer, { method: 'POST', headers: { 'X-Voxa': '1', 'Content-Type': f.type }, body: f }); file.value = ''; last = ''; load(); };
            let rec = null, chunks = [], t0 = 0;
            mic.onclick = async () => {
                if (rec) { rec.stop(); return; }
                try {
                    const s = await navigator.mediaDevices.getUserMedia({ audio: true });
                    rec = new MediaRecorder(s); chunks = []; t0 = Date.now();
                    rec.ondataavailable = e => chunks.push(e.data);
                    rec.onstop = async () => {
                        s.getTracks().forEach(x => x.stop());
                        const dur = (Date.now() - t0) / 1000, type = (rec.mimeType || 'audio/webm').split(';')[0];
                        rec = null; mic.classList.remove('recording'); mic.innerHTML = ic('mic');
                        if (dur < 0.7) return;
                        await fetch('/voice?to=' + myPeer + '&dur=' + Math.round(dur), { method: 'POST', headers: { 'X-Voxa': '1', 'Content-Type': type }, body: new Blob(chunks, { type }) });
                        last = ''; load();
                    };
                    rec.start(); mic.classList.add('recording'); mic.innerHTML = ic('stop');
                } catch (e) { alert('لازم تسمح باستخدام الميكروفون'); }
            };
            await load();
            timer = setInterval(load, 1500);
        }

        // ===== المكالمات (WebRTC) - لوحة فوق الدردشة زي ديسكورد =====
        let peerConnection = null, callPeer = null, callInfo = null, pendingCands = [], remoteSet = false, ringTimeout = null;
        let callPanelEl = null, chatHeaderEl = null, chatPeerId = null, tick = null;
        const rt = { muted: false, cam: false, rcam: false, rmic: true, t0: 0, vs: null, remote: null, state: 'idle' };
        const rtcConfig = { iceServers: [{ urls: 'stun:stun.l.google.com:19302' }, { urls: 'stun:stun1.l.google.com:19302' }] };
        const sig = (to, type, data, video) => api('/api/call_signal', { to, type, data, video: !!video });
        const ra = document.createElement('audio'); ra.autoplay = true; document.body.append(ra);
        const bubble = el('button', 'cbubble'); bubble.innerHTML = ic('phone'); bubble.setAttribute('aria-label', 'العودة للمكالمة'); bubble.title = 'العودة للمكالمة';
        bubble.onclick = () => { peer = callPeer; showPage('msgs'); }; document.body.append(bubble);
        const statusText = () => rt.state === 'live' ? fmt((Date.now() - rt.t0) / 1000) : rt.state === 'ringing' ? 'يرن...' : 'جاري الاتصال...';
        function syncCallUI() {
            bubble.style.display = (isCalling && rt.state !== 'ringing' && !(callPanelEl && callPanelEl.isConnected)) ? 'flex' : 'none';
            if (callPanelEl && callPanelEl._r) callPanelEl._r.st.textContent = statusText();
        }
        setInterval(syncCallUI, 500);

        function tileEl() {
            const t = el('div', 'ctile'), v = document.createElement('video'); v.autoplay = true; v.playsInline = true; v.muted = true;
            const av = el('div', 'avatar-circle cav'), nm = el('div', 'cname'), mi = el('div', 'cmic'); mi.innerHTML = ic('micoff');
            t.append(v, av, nm, mi); return { t, v, av, nm, mi };
        }
        function buildPanel() {
            const p = el('div', 'callpanel'), tiles = el('div', 'call-tiles'), pe = tileEl(), mm = tileEl();
            tiles.append(pe.t, mm.t);
            const st = el('div', 'call-status'), bar = el('div', 'call-bar');
            const bm = el('button', 'cbtn'), bc = el('button', 'cbtn'), be = el('button', 'cbtn end');
            bm.onclick = toggleMute; bc.onclick = toggleCam; be.onclick = () => endCall(); be.innerHTML = ic('hangup');
            bm.title = 'ميوت'; bc.title = 'الكاميرا'; be.title = 'إنهاء';
            bar.append(bm, bc, be); p.append(tiles, st, bar); p._r = { pe, me: mm, st, bm, bc }; callPanelEl = p;
        }
        function renderCall() {
            const p = callPanelEl; if (!p || !isCalling) return; const r = p._r;
            r.pe.nm.textContent = callInfo ? callInfo.name : ''; renderAvatar(r.pe.av, { display_name: callInfo && callInfo.name, avatar: callInfo && callInfo.avatar });
            r.me.nm.textContent = 'أنت'; renderAvatar(r.me.av, me);
            r.me.v.srcObject = localStream; r.pe.v.srcObject = rt.remote;
            const show = (x, on) => { x.v.style.display = on ? 'block' : 'none'; x.av.style.display = on ? 'none' : 'flex'; };
            show(r.me, rt.cam); show(r.pe, rt.rcam);
            r.me.mi.style.display = rt.muted ? 'flex' : 'none'; r.pe.mi.style.display = rt.rmic ? 'none' : 'flex';
            r.bm.innerHTML = ic(rt.muted ? 'micoff' : 'mic'); r.bm.classList.toggle('off', rt.muted);
            r.bc.innerHTML = ic(rt.cam ? 'video' : 'videooff'); r.bc.classList.toggle('off', !rt.cam);
            syncCallUI();
        }
        function dockCall() { if (callPanelEl && chatHeaderEl && chatHeaderEl.isConnected && chatPeerId === callPeer) chatHeaderEl.after(callPanelEl); syncCallUI(); }
        const stateOut = () => sig(callPeer, 'state', { cam: rt.cam, mic: !rt.muted });

        function toggleMute() {
            rt.muted = !rt.muted; if (localStream) localStream.getAudioTracks().forEach(x => x.enabled = !rt.muted);
            stateOut(); renderCall();
        }
        async function toggleCam() {
            if (!peerConnection) return;
            if (rt.cam) {
                const tr = localStream.getVideoTracks()[0]; if (tr) { tr.stop(); localStream.removeTrack(tr); }
                if (rt.vs) await rt.vs.replaceTrack(null);
                rt.cam = false;
            } else {
                try {
                    const s = await navigator.mediaDevices.getUserMedia({ video: true }), tr = s.getVideoTracks()[0];
                    localStream.addTrack(tr);
                    if (rt.vs) await rt.vs.replaceTrack(tr);
                    else { rt.vs = peerConnection.addTrack(tr, localStream); const o = await peerConnection.createOffer(); await peerConnection.setLocalDescription(o); await sig(callPeer, 'offer', o, true); }
                    rt.cam = true;
                } catch (e) { alert('تعذر فتح الكاميرا'); return; }
            }
            stateOut(); renderCall();
        }

        function startCallPolling() {
            if (callPollTimer) clearInterval(callPollTimer);
            let busy = false;
            callPollTimer = setInterval(async () => {
                if (!me || busy) return; busy = true;
                try { const r = await api('/api/call_signal'); if (r.ok) for (const s of (r.d.signals || [])) await handleSignal(s); }
                catch (e) {} finally { busy = false; }
            }, 700);
        }
        async function flushCands() { while (pendingCands.length && peerConnection && remoteSet) { try { await peerConnection.addIceCandidate(pendingCands.shift()); } catch (e) {} } }

        async function handleSignal(s) {
            if (s.type === 'offer') {
                if (peerConnection && s.from === callPeer) {   // فتح كاميرا في نص المكالمة
                    await peerConnection.setRemoteDescription(new RTCSessionDescription(s.data));
                    const a = await peerConnection.createAnswer(); await peerConnection.setLocalDescription(a); sig(s.from, 'answer', a); return;
                }
                if (isCalling) { sig(s.from, 'end'); return; }
                showIncoming(s);
            } else if (s.type === 'answer' && peerConnection && s.from === callPeer) {
                clearTimeout(ringTimeout);
                await peerConnection.setRemoteDescription(new RTCSessionDescription(s.data)); remoteSet = true; await flushCands();
            } else if (s.type === 'candidate' && s.data && s.from === callPeer) {
                pendingCands.push(new RTCIceCandidate(s.data)); await flushCands();
            } else if (s.type === 'state' && s.from === callPeer && s.data) {
                rt.rcam = !!s.data.cam; rt.rmic = !!s.data.mic; renderCall();
            } else if (s.type === 'end' && s.from === callPeer) { endCall(false); }
        }

        function makePC(target) {
            const pc = new RTCPeerConnection(rtcConfig);
            localStream.getTracks().forEach(x => { const sn = pc.addTrack(x, localStream); if (x.kind === 'video') rt.vs = sn; });
            pc.ontrack = e => { rt.remote = e.streams[0]; ra.srcObject = rt.remote; ra.play().catch(() => {}); renderCall(); };
            pc.onicecandidate = e => { if (e.candidate) sig(target, 'candidate', e.candidate); };
            pc.onconnectionstatechange = () => {
                if (pc.connectionState === 'connected') { rt.state = 'live'; rt.t0 = Date.now(); renderCall(); }
                if (pc.connectionState === 'failed') { alert('تعذر الاتصال'); endCall(true); }
            };
            return pc;
        }
        function resetRt(video) { Object.assign(rt, { muted: false, cam: !!video, rcam: !!video, rmic: true, t0: 0, vs: null, remote: null, state: 'calling' }); pendingCands = []; remoteSet = false; }

        async function initiateCall(target, video, info) {
            if (isCalling) return;
            try { localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: !!video }); }
            catch (err) { alert('لازم تسمح بالميكروفون' + (video ? ' والكاميرا' : '') + '، والمكالمات بتشتغل بس على رابط https أو localhost'); return; }
            isCalling = true; callPeer = target; callInfo = { id: target, name: info.display_name, avatar: info.avatar }; resetRt(video);
            buildPanel(); dockCall(); renderCall();
            peerConnection = makePC(target);
            const offer = await peerConnection.createOffer(); await peerConnection.setLocalDescription(offer);
            await sig(target, 'offer', offer, video);
            ringTimeout = setTimeout(() => { if (rt.state !== 'live') { alert('مفيش رد'); endCall(true); } }, 45000);
        }

        function showIncoming(s) {
            isCalling = true; callPeer = s.from; callInfo = { id: s.from, name: s.from_name, avatar: null }; resetRt(false); rt.state = 'ringing'; rt.rcam = !!s.video;
            api('/api/profile?id=' + s.from).then(r => { if (r.ok && callInfo && callInfo.id === s.from) { callInfo.avatar = r.d.avatar; renderCall(); } });
            const tst = el('div', 'incoming'); tst.id = 'incoming';
            tst.append(el('div', '', s.from_name + ' — مكالمة ' + (s.video ? 'فيديو' : 'صوتية')));
            const row = el('div', 'call-bar'), ok = el('button', 'cbtn ok'), no = el('button', 'cbtn end');
            ok.innerHTML = ic('check'); no.innerHTML = ic('hangup'); ok.title = 'قبول'; no.title = 'رفض';
            ok.onclick = () => acceptCall(s); no.onclick = () => endCall(); row.append(ok, no); tst.append(row); document.body.append(tst);
        }

        async function acceptCall(s) {
            const tst = $('#incoming'); if (tst) tst.remove();
            try { localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: !!s.video }); }
            catch (err) { alert('فشل تشغيل الميكروفون/الكاميرا'); endCall(true); return; }
            rt.state = 'calling'; rt.cam = !!s.video; buildPanel();
            try {
                peerConnection = makePC(s.from);
                await peerConnection.setRemoteDescription(new RTCSessionDescription(s.data)); remoteSet = true; await flushCands();
                const a = await peerConnection.createAnswer(); await peerConnection.setLocalDescription(a); await sig(s.from, 'answer', a);
            } catch (err) { endCall(true); return; }
            peer = s.from; showPage('msgs');
        }

        function endCall(notify = true) {
            clearTimeout(ringTimeout);
            if (notify && callPeer) sig(callPeer, 'end');
            if (localStream) { localStream.getTracks().forEach(x => x.stop()); localStream = null; }
            if (peerConnection) { peerConnection.close(); peerConnection = null; }
            const tst = $('#incoming'); if (tst) tst.remove();
            if (callPanelEl) { callPanelEl.remove(); callPanelEl = null; }
            ra.srcObject = null; isCalling = false; callPeer = null; callInfo = null; rt.state = 'idle'; pendingCands = []; remoteSet = false;
            syncCallUI();
        }

        async function logout() {
            await api('/api/logout', {});
            me = null;
            $('#sidebar').style.display = 'none';
            boot();
        }

        setupMobile();
        boot();
    </script>
</body>
</html>
"""

def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()

def main():
    global ARGS
    parser = argparse.ArgumentParser(description="Voxa Server")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--invite", default="")
    ARGS = parser.parse_args()
    
    init_db()
    server = ThreadingHTTPServer((ARGS.host, ARGS.port), VoxaHandler)
    server.daemon_threads = True if hasattr(server, 'daemon_threads') else None
    
    print("="*50)
    print("🚀 Voxa Server is Running!")
    print("="*50)
    print(f"🌐 Local URL:    http://localhost:{ARGS.port}")
    print(f"📱 Network URL:  http://{get_lan_ip()}:{ARGS.port}")
    print(f"🔑 Invite Code:  {ARGS.invite if ARGS.invite else 'No code'}")
    print("="*50)
    print("Press Ctrl+C to stop the server.\n")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Server stopped.")

if __name__ == "__main__":
    main()