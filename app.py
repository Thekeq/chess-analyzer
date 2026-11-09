import json
import mimetypes
import re
import urllib.error
import urllib.request

from flask import Flask, abort, jsonify, request, send_from_directory

import chess

from analysis import BOOK, _score, build_report, judge_move, parse_game, win_prob

mimetypes.add_type("application/wasm", ".wasm")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024
UA = {"User-Agent": "chessanalyze/1.0"}


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
        return jsonify(error="Партия или игрок не найдены"), 404
    return jsonify(error=f"Сайт с партией не ответил: {e.reason}"), 502


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.post("/api/game")
def game():
    text = (request.get_json(force=True).get("input") or "").strip()
    is_url = re.fullmatch(r"\S+\.\S+/\S*", text)  # PGN headers also contain links, so only a bare URL counts
    if is_url and (m := re.search(r"lichess\.org/(?:game/export/)?([A-Za-z0-9]{8})", text)):
        text = fetch(f"https://lichess.org/game/export/{m[1]}?clocks=false&evals=false", "application/x-chess-pgn")
    elif is_url and "chess.com/" in text:
        raise ValueError("Chess.com не отдаёт партию по ссылке — введите ник ниже и выберите партию из списка")
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
        raise ValueError("Нелегальный ход")
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
