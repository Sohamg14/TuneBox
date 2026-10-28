"""Tunebox - a small music sharing app for AWS EC2 (Flask + S3 + SQLite)."""
import os
import re
import secrets
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "tunebox.db"))
BUCKET = os.environ.get("BUCKET_NAME", "")          # empty = save files locally (dev only)
REGION = os.environ.get("AWS_REGION", "ap-south-1")
LOCAL_DIR = os.path.join(BASE_DIR, "uploads")
ALLOWED = {"mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg", "m4a": "audio/mp4"}
PUBLIC_ENDPOINTS = {"login", "register", "static"}

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
    MAX_CONTENT_LENGTH=30 * 1024 * 1024,             # 30 MB per upload
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# On EC2 the IAM role supplies credentials automatically - no keys in code.
s3 = boto3.client("s3", region_name=REGION,
                  config=Config(signature_version="s3v4")) if BUCKET else None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS songs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    artist TEXT,
    genre TEXT,
    file_key TEXT NOT NULL,
    uploader_id INTEGER NOT NULL REFERENCES users(id),
    plays INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS likes (
    user_id INTEGER NOT NULL,
    song_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, song_id)
);
"""


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)


init_db()


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- storage (S3 in production, local folder for quick testing) ----------

def save_file(stream, key, content_type):
    if BUCKET:
        s3.upload_fileobj(stream, BUCKET, key, ExtraArgs={
            "ContentType": content_type,
            "ServerSideEncryption": "AES256",        # encryption at rest
        })
    else:
        path = os.path.join(LOCAL_DIR, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as out:
            shutil.copyfileobj(stream, out)


def delete_file(key):
    try:
        if BUCKET:
            s3.delete_object(Bucket=BUCKET, Key=key)
        else:
            os.remove(os.path.join(LOCAL_DIR, key))
    except (BotoCoreError, ClientError, OSError) as exc:
        app.logger.warning("Could not delete %s: %s", key, exc)


def file_url(key):
    if BUCKET:   # private bucket: hand out a temporary signed link (1 hour)
        return s3.generate_presigned_url(
            "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=3600)
    return url_for("media", key=key)


# ---------- request hooks ----------

@app.before_request
def guard():
    uid = session.get("uid")
    g.user = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None

    if request.method == "POST":                      # simple CSRF protection
        token = request.form.get("csrf") or request.headers.get("X-CSRF")
        if not token or token != session.get("csrf"):
            abort(400, "Invalid form token. Reload the page and try again.")

    if request.endpoint and request.endpoint not in PUBLIC_ENDPOINTS and not g.user:
        return redirect(url_for("login"))


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return {"csrf_token": session["csrf"], "user": g.get("user")}


@app.errorhandler(413)
def too_large(_e):
    flash("That file is too big. The limit is 30 MB.", "error")
    return redirect(url_for("upload"))


# ---------- auth ----------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("username", "").strip().lower()
        pw = request.form.get("password", "")
        if not re.fullmatch(r"[a-z0-9_]{3,20}", name):
            flash("Username must be 3-20 characters: letters, numbers or underscore.", "error")
        elif len(pw) < 6:
            flash("Password must be at least 6 characters.", "error")
        else:
            try:
                cur = db().execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                    (name, generate_password_hash(pw), now()))
                db().commit()
                session["uid"] = cur.lastrowid
                return redirect(url_for("index"))
            except sqlite3.IntegrityError:
                flash("That username is taken. Try another.", "error")
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name = request.form.get("username", "").strip().lower()
        row = db().execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
        if row and check_password_hash(row["password_hash"], request.form.get("password", "")):
            session["uid"] = row["id"]
            return redirect(url_for("index"))
        flash("Wrong username or password.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("uid", None)
    return redirect(url_for("login"))


# ---------- music ----------

@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    view = request.args.get("view", "all")
    uid = g.user["id"]
    sql = """SELECT s.*, u.username AS uploader,
               (SELECT COUNT(*) FROM likes l WHERE l.song_id = s.id) AS like_count,
               EXISTS(SELECT 1 FROM likes l WHERE l.song_id = s.id AND l.user_id = ?) AS liked
             FROM songs s JOIN users u ON u.id = s.uploader_id"""
    params, where = [uid], []
    if q:
        where.append("(s.title LIKE ? OR s.artist LIKE ? OR s.genre LIKE ?)")
        params += [f"%{q}%"] * 3
    if view == "mine":
        where.append("s.uploader_id = ?")
        params.append(uid)
    elif view == "liked":
        where.append("EXISTS(SELECT 1 FROM likes l2 WHERE l2.song_id = s.id AND l2.user_id = ?)")
        params.append(uid)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY s.id DESC"
    songs = db().execute(sql, params).fetchall()
    return render_template("index.html", songs=songs, q=q, view=view)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        f = request.files.get("audio")
        title = request.form.get("title", "").strip()[:100]
        artist = request.form.get("artist", "").strip()[:100]
        genre = request.form.get("genre", "").strip()[:30]
        ext = f.filename.rsplit(".", 1)[-1].lower() if f and f.filename and "." in f.filename else ""
        if not title:
            flash("Give the song a title.", "error")
        elif ext not in ALLOWED:
            flash("Choose an MP3, WAV, OGG or M4A file.", "error")
        else:
            key = f"songs/{uuid.uuid4().hex}.{ext}"
            try:
                save_file(f.stream, key, ALLOWED[ext])
            except (BotoCoreError, ClientError) as exc:
                app.logger.error("S3 upload failed: %s", exc)
                flash("Upload failed. Check the S3 bucket name and the instance's IAM role.", "error")
            else:
                db().execute(
                    "INSERT INTO songs (title, artist, genre, file_key, uploader_id, created_at) "
                    "VALUES (?,?,?,?,?,?)", (title, artist, genre, key, g.user["id"], now()))
                db().commit()
                flash(f"“{title}” is now shared.", "ok")
                return redirect(url_for("index"))
    return render_template("upload.html")


@app.route("/stream/<int:song_id>")
def stream(song_id):
    song = db().execute("SELECT * FROM songs WHERE id=?", (song_id,)).fetchone()
    if not song:
        abort(404)
    db().execute("UPDATE songs SET plays = plays + 1 WHERE id=?", (song_id,))
    db().commit()
    return jsonify(url=file_url(song["file_key"]), title=song["title"],
                   artist=song["artist"] or "Unknown artist")


@app.route("/like/<int:song_id>", methods=["POST"])
def like(song_id):
    conn = db()
    if not conn.execute("SELECT 1 FROM songs WHERE id=?", (song_id,)).fetchone():
        abort(404)
    uid = g.user["id"]
    if conn.execute("SELECT 1 FROM likes WHERE user_id=? AND song_id=?", (uid, song_id)).fetchone():
        conn.execute("DELETE FROM likes WHERE user_id=? AND song_id=?", (uid, song_id))
        liked = False
    else:
        conn.execute("INSERT INTO likes (user_id, song_id) VALUES (?,?)", (uid, song_id))
        liked = True
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM likes WHERE song_id=?", (song_id,)).fetchone()[0]
    return jsonify(liked=liked, count=count)


@app.route("/delete/<int:song_id>", methods=["POST"])
def delete(song_id):
    conn = db()
    song = conn.execute("SELECT * FROM songs WHERE id=?", (song_id,)).fetchone()
    if not song:
        abort(404)
    if song["uploader_id"] != g.user["id"]:
        abort(403)
    delete_file(song["file_key"])
    conn.execute("DELETE FROM likes WHERE song_id=?", (song_id,))
    conn.execute("DELETE FROM songs WHERE id=?", (song_id,))
    conn.commit()
    flash("Song deleted.", "ok")
    return redirect(url_for("index"))


@app.route("/media/<path:key>")
def media(key):
    if BUCKET:
        abort(404)
    return send_from_directory(LOCAL_DIR, key)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
