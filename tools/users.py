"""List registered users, newest first. Run on the server:  .venv/bin/python tools/users.py"""
import sqlite3
from pathlib import Path

db = sqlite3.connect(Path(__file__).resolve().parent.parent / "var" / "app.db")
rows = db.execute("SELECT id, username, created_at, puzzle_rating, puzzle_games, rush_best, site, nick"
                  " FROM users ORDER BY id DESC").fetchall()
print(f"{len(rows)} registered users\n")
print(f"{'#':>4}  {'username':<20} {'registered (UTC)':<19}  {'rating':>6} {'puzzles':>7} {'rush':>4}  linked")
for uid, name, created, rating, games, rush, site, nick in rows:
    linked = f"{nick} ({'chess.com' if site == 'chesscom' else site})" if nick else ""
    print(f"{uid:>4}  {name:<20} {created or '—':<19}  {rating:>6} {games:>7} {rush:>4}  {linked}")
