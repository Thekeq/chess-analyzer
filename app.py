import bisect
import csv
import gzip
import json
import mimetypes
import os
import random
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, Response, abort, g, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

import chess

from analysis import BOOK, _score, build_report, judge_move, parse_game, win_prob

mimetypes.add_type("application/wasm", ".wasm")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024
UA = {"User-Agent": "chessanalyze/1.0"}

# --- Accounts: SQLite + signed session cookie. var/ holds the DB and the session key (not in git, survives deploys).
VAR = Path(__file__).parent / "var"
VAR.mkdir(exist_ok=True)
KEY = VAR / "secret_key"
if not KEY.exists():  # gunicorn workers start together: os.link is an atomic create-if-missing, so all share one key
    tmp = VAR / f"secret_key.{os.getpid()}"
    tmp.write_text(secrets.token_hex(32))
    try:
        os.link(tmp, KEY)
    except FileExistsError:
        pass
    tmp.unlink()
app.secret_key = KEY.read_text()
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=90 * 86400)
with sqlite3.connect(VAR / "app.db") as c:
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, pw_hash TEXT NOT NULL,
        site TEXT, nick TEXT, puzzle_rating INTEGER NOT NULL DEFAULT 1200, puzzle_games INTEGER NOT NULL DEFAULT 0,
        rush_best INTEGER NOT NULL DEFAULT 0, seen TEXT NOT NULL DEFAULT '[]')""")


def db():
    if "db" not in g:
        g.db = sqlite3.connect(VAR / "app.db")
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    if (conn := g.pop("db", None)) is not None:
        conn.close()


def user():
    return db().execute("SELECT * FROM users WHERE id = ?", (session.get("uid"),)).fetchone()


# Guests use everything; their progress lives in the signed session cookie and moves into the account on sign-up.
GUEST = {"site": None, "nick": None, "puzzle_rating": 1200, "puzzle_games": 0, "rush_best": 0, "seen": "[]"}
SEEN_KEEP = {True: 500, False: 60}  # recently played puzzles to skip: account / guest (the cookie must stay < 4 KB)


def profile():
    u = user()
    return dict(u) if u else {**GUEST, **session.get("guest", {}), "username": None}


def save_profile(**fields):
    if user():
        with db():
            db().execute(f"UPDATE users SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?",
                         (*fields.values(), session["uid"]))
    else:
        session.permanent = True
        session["guest"] = {**session.get("guest", {}), **fields}


def _credentials():
    d = request.get_json(force=True)
    return (d.get("username") or "").strip(), d.get("password") or ""


def _sign_in(uid):
    pz = session.get("pz")
    session.clear()
    if pz:
        session["pz"] = pz
    session.permanent = True
    session["uid"] = uid
    return me()


@app.post("/api/auth/register")
def register():
    name, pw = _credentials()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,20}", name):
        raise ValueError("Username: 3–20 characters — letters, digits, _ and -")
    if len(pw) < 6:
        raise ValueError("Password: at least 6 characters")
    guest = profile()  # progress made as a guest carries over into the new account
    try:
        with db():
            uid = db().execute(
                "INSERT INTO users (username, pw_hash, site, nick, puzzle_rating, puzzle_games, rush_best, seen)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (name, generate_password_hash(pw), guest["site"], guest["nick"], guest["puzzle_rating"],
                 guest["puzzle_games"], guest["rush_best"], guest["seen"])).lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("This username is taken")
    return _sign_in(uid)


@app.post("/api/auth/login")
def login():
    name, pw = _credentials()
    row = db().execute("SELECT id, pw_hash FROM users WHERE username = ?", (name,)).fetchone()
    if row is None or not check_password_hash(row["pw_hash"], pw):
        time.sleep(1)  # slows down password guessing
        raise ValueError("Wrong username or password")
    return _sign_in(row["id"])


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify()


@app.get("/api/me")
def me():
    """Account or guest profile; username is null for guests."""
    p = profile()
    return jsonify({k: p[k] for k in ("username", "site", "nick", "puzzle_rating", "puzzle_games", "rush_best")})


@app.post("/api/me")
def link_account():
    """Link (or with an empty nick, unlink) the chess.com / lichess nick for the "My games" panel."""
    d = request.get_json(force=True)
    site, nick = d.get("site"), (d.get("nick") or "").strip()
    if nick and (site not in ("chesscom", "lichess") or not re.fullmatch(r"[A-Za-z0-9_-]{2,30}", nick)):
        raise ValueError("Invalid username")
    save_profile(site=site if nick else None, nick=nick or None)
    return me()


@app.after_request
def isolate(resp):
    # cross-origin isolation enables SharedArrayBuffer, needed for multi-threaded Stockfish
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    resp.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
    return resp


def fetch(url, accept="application/json"):
    req = urllib.request.Request(url, headers={**UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode()


@app.errorhandler(ValueError)
def bad_input(e):
    return jsonify(error=str(e)), 400


@app.errorhandler(urllib.error.URLError)
def upstream(e):
    if getattr(e, "code", None) == 404:
        return jsonify(error="Game or player not found"), 404
    return jsonify(error=f"The chess site didn't respond: {e.reason}"), 502


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/robots.txt")
def robots():
    return Response(f"User-agent: *\nAllow: /\nDisallow: /api/\nSitemap: https://{request.host}/sitemap.xml\n", mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap():
    return Response('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<url><loc>https://{request.host}/</loc></url></urlset>\n", mimetype="application/xml")


@app.post("/api/game")
def game():
    text = (request.get_json(force=True).get("input") or "").strip()
    is_url = re.fullmatch(r"\S+\.\S+/\S*", text)  # PGN headers also contain links, so only a bare URL counts
    if is_url and (m := re.search(r"lichess\.org/(?:game/export/)?([A-Za-z0-9]{8})", text)):
        text = fetch(f"https://lichess.org/game/export/{m[1]}?clocks=false&evals=false", "application/x-chess-pgn")
    elif is_url and "chess.com/" in text:
        raise ValueError("Chess.com doesn't share games by link — link your chess.com username and pick the game from \"My games\"")
    return jsonify(parse_game(text))


@app.post("/api/report")
def report():
    data = request.get_json(force=True)
    return jsonify(build_report(data["pgn"], data["evals"]))


@app.post("/api/judge")
def judge():
    """Classify a move the user plays on the board: {fen, uci, before, after, prev_cls?, prev_uci?} (evals as in /api/report)."""
    d = request.get_json(force=True)
    before, move = chess.Board(d["fen"]), chess.Move.from_uci(d["uci"])
    if move not in before.legal_moves:
        raise ValueError("Illegal move")
    after = before.copy(stack=False)
    after.push(move)
    score = _score(after, d.get("after"))
    if after.epd() in BOOK:
        cls, best_san, best_uci = "book", None, None
    else:
        prev = d.get("prev_uci")
        recapture = bool(prev) and before.is_capture(move) and move.to_square == chess.parse_square(prev[2:4])
        cls, best_san, best_uci = judge_move(before, move, d.get("before"), score, d.get("prev_cls"), recapture)
    return jsonify(cls=cls, best_san=best_san, best_uci=best_uci, san=before.san(move), fen=after.fen(),
                   score=dict(score, wp=round(win_prob(score), 4)), opening=BOOK.get(after.epd()))


# Lichess puzzle subset (CC0), sorted by rating; rebuild with tools/build_puzzles.py
with gzip.open(Path(__file__).parent / "data" / "puzzles.csv.gz", "rt", encoding="utf-8") as f:
    PUZZLES = list(csv.DictReader(f))
PUZZLE_RATINGS = [int(p["rating"]) for p in PUZZLES]
PUZZLE_BY_ID = {p["id"]: p for p in PUZZLES}


@app.get("/api/puzzle")
def puzzle():
    """Random unseen puzzle near the player's puzzle rating, or near ?rating= with ?short=1 (1-2 moves, puzzle rush)."""
    u = profile()
    rating = request.args.get("rating", u["puzzle_rating"], type=int)
    rating = min(max(rating, PUZZLE_RATINGS[0]), PUZZLE_RATINGS[-1])
    short, seen = request.args.get("short") == "1", set(json.loads(u["seen"]))
    for spread in (50, 100, 200, 400, 3000):
        lo = bisect.bisect_left(PUZZLE_RATINGS, rating - spread)
        hi = bisect.bisect_right(PUZZLE_RATINGS, rating + spread)
        pool = [p for p in PUZZLES[lo:hi] if p["id"] not in seen and (not short or len(p["moves"].split()) <= 4)]
        if pool:
            p = random.choice(pool)
            session["pz"] = p["id"]  # the only puzzle a rated result is accepted for
            return jsonify(p)
    abort(404)


@app.post("/api/puzzle/result")
def puzzle_result():
    """First attempt at the last served puzzle: Elo update against the puzzle's rating."""
    d = request.get_json(force=True)
    p = PUZZLE_BY_ID.get(d.get("id"))
    if p is None or session.pop("pz", None) != p["id"]:
        raise ValueError("This puzzle's result is already counted")
    u = profile()
    expected = 1 / (1 + 10 ** ((int(p["rating"]) - u["puzzle_rating"]) / 400))
    delta = round((40 if u["puzzle_games"] < 30 else 20) * ((1 if d.get("win") else 0) - expected))  # faster at first
    rating = max(100, u["puzzle_rating"] + delta)
    seen = (json.loads(u["seen"]) + [p["id"]])[-SEEN_KEEP[u["username"] is not None]:]
    save_profile(puzzle_rating=rating, puzzle_games=u["puzzle_games"] + 1, seen=json.dumps(seen))
    return jsonify(delta=delta, rating=rating, games=u["puzzle_games"] + 1)


@app.post("/api/rush/result")
def rush_result():
    score = int(request.get_json(force=True).get("score"))
    if not 0 <= score <= 500:
        raise ValueError("Invalid score")
    best = profile()["rush_best"]
    if score > best:
        save_profile(rush_best=score)
    return jsonify(best=max(score, best), record=score > best)


PAGE = 5


@app.get("/api/games/<site>/<user>")
def user_games(site, user):
    """Standard games of a chess.com or lichess player, newest first, PAGE per page (?page=0,1,...)."""
    page = request.args.get("page", 0, type=int)
    if site not in ("chesscom", "lichess") or not re.fullmatch(r"[A-Za-z0-9_-]{2,30}", user) or not 0 <= page < 100:
        abort(400)
    need = (page + 1) * PAGE + 1  # one extra tells whether an older page exists

    if site == "lichess":
        perfs = "ultraBullet,bullet,blitz,rapid,classical,correspondence"  # standard chess only
        ndjson = fetch(f"https://lichess.org/api/games/user/{user}?max={need}&perfType={perfs}"
                       "&pgnInJson=true&clocks=false&evals=false", "application/x-ndjson")
        player = lambda g, c: g["players"][c].get("user", {}).get("name") or f'Stockfish {g["players"][c].get("aiLevel", "")}'
        games = [
            {"white": player(g, "white"), "black": player(g, "black"),
             "white_elo": g["players"]["white"].get("rating"), "black_elo": g["players"]["black"].get("rating"),
             "winner": g.get("winner"), "speed": g.get("speed"), "end": g["lastMoveAt"] // 1000, "pgn": g["pgn"]}
            for g in map(json.loads, filter(None, ndjson.splitlines())) if g.get("pgn")
        ]
    else:
        winner = lambda g: "white" if g["white"]["result"] == "win" else "black" if g["black"]["result"] == "win" else None
        archives = json.loads(fetch(f"https://api.chess.com/pub/player/{user.lower()}/games/archives"))["archives"]
        games = []
        for url in reversed(archives):  # ponytail: refetches past months per page, cache them if paging gets slow
            try:
                month = json.loads(fetch(url))["games"]
            except urllib.error.HTTPError:  # chess.com sometimes 404s a listed (current) month
                continue
            games += [
                {"white": g["white"]["username"], "black": g["black"]["username"],
                 "white_elo": g["white"].get("rating"), "black_elo": g["black"].get("rating"),
                 "winner": winner(g), "speed": g.get("time_class"), "end": g["end_time"], "pgn": g["pgn"]}
                for g in reversed(month) if g.get("pgn") and g.get("rules") == "chess"
            ]
            if len(games) >= need:
                break

    return jsonify(games=games[page * PAGE:(page + 1) * PAGE], more=len(games) > (page + 1) * PAGE)


if __name__ == "__main__":
    app.run(debug=True)
