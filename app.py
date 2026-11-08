import json
import mimetypes
import re
import urllib.error
import urllib.request

from flask import Flask, abort, jsonify, request, send_from_directory

from analysis import build_report, parse_game

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


@app.get("/api/games/<site>/<user>")
def user_games(site, user):
    """Last 5 standard games of a chess.com or lichess player."""
    if site not in ("chesscom", "lichess") or not re.fullmatch(r"[A-Za-z0-9_-]{2,30}", user):
        abort(400)
    if site == "lichess":
        ndjson = fetch(f"https://lichess.org/api/games/user/{user}?max=10&pgnInJson=true&clocks=false&evals=false",
                       "application/x-ndjson")
        games = [json.loads(line) for line in ndjson.splitlines() if line]
        player = lambda g, c: g["players"][c].get("user", {}).get("name") or f'Stockfish {g["players"][c].get("aiLevel", "")}'
        return jsonify([
            {"white": player(g, "white"), "black": player(g, "black"),
             "white_elo": g["players"]["white"].get("rating"), "black_elo": g["players"]["black"].get("rating"),
             "winner": g.get("winner"), "speed": g.get("speed"), "end": g["lastMoveAt"] // 1000, "pgn": g["pgn"]}
            for g in games if g.get("variant") == "standard" and g.get("pgn")
        ][:5])

    archives = json.loads(fetch(f"https://api.chess.com/pub/player/{user.lower()}/games/archives"))["archives"]
    games = []
    for url in reversed(archives[-3:]):
        try:
            games += reversed(json.loads(fetch(url))["games"])
        except urllib.error.HTTPError:  # chess.com sometimes 404s a listed (current) month
            continue
        if len(games) >= 10:
            break
    winner = lambda g: "white" if g["white"]["result"] == "win" else "black" if g["black"]["result"] == "win" else None
    return jsonify([
        {"white": g["white"]["username"], "black": g["black"]["username"],
         "white_elo": g["white"].get("rating"), "black_elo": g["black"].get("rating"),
         "winner": winner(g), "speed": g.get("time_class"), "end": g["end_time"], "pgn": g["pgn"]}
        for g in games if g.get("pgn") and g.get("rules") == "chess"
    ][:5])


if __name__ == "__main__":
    app.run(debug=True)
