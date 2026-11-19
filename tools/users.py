"""List registered users, newest first. Run on the server:  .venv/bin/python tools/users.py"""
import sqlite3
from pathlib import Path

db = sqlite3.connect(Path(__file__).resolve().parent.parent / "var" / "app.db")
# created_at is added by app.py on start; don't depend on the site having been restarted yet
created = "created_at" if "created_at" in {r[1] for r in db.execute("PRAGMA table_info(users)")} else "NULL"
rows = db.execute(f"SELECT id, username, {created}, puzzle_rating, puzzle_games, rush_best, site, nick"
                  " FROM users ORDER BY id DESC").fetchall()
print(f"{len(rows)} registered users\n")
print(f"{'#':>4}  {'username':<20} {'registered (UTC)':<19}  {'rating':>6} {'puzzles':>7} {'rush':>4}  linked")
for uid, name, created, rating, games, rush, site, nick in rows:
    linked = f"{nick} ({'chess.com' if site == 'chesscom' else site})" if nick else ""
    print(f"{uid:>4}  {name:<20} {created or '—':<19}  {rating:>6} {games:>7} {rush:>4}  {linked}")
