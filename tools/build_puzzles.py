"""Build data/puzzles.csv.gz: a small, well-rated subset of the lichess puzzle database (CC0).

    pip install zstandard
    curl -O https://database.lichess.org/lichess_db_puzzle.csv.zst
    python tools/build_puzzles.py lichess_db_puzzle.csv.zst

Keeps popular, often-played puzzles with a settled rating, at most PER_BUCKET per 25-point rating bucket.
"""
import csv
import gzip
import io
import random
import sys
from pathlib import Path

import zstandard

PER_BUCKET, BUCKET, MIN_RATING, MAX_RATING = 250, 25, 400, 2800


def main(src):
    random.seed(1)
    buckets, seen = {}, {}
    with open(src, "rb") as raw:
        reader = csv.DictReader(io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(raw), encoding="utf-8"))
        for row in reader:
            rating = int(row["Rating"])
            if not (MIN_RATING <= rating < MAX_RATING and int(row["Popularity"]) >= 90
                    and int(row["NbPlays"]) >= 500 and int(row["RatingDeviation"]) <= 90):
                continue
            b = rating // BUCKET
            keep = buckets.setdefault(b, [])
            n = seen[b] = seen.get(b, 0) + 1
            item = (row["PuzzleId"], row["FEN"], row["Moves"], rating, row["Themes"])
            if len(keep) < PER_BUCKET:  # reservoir sampling: uniform pick from the whole file
                keep.append(item)
            elif (j := random.randrange(n)) < PER_BUCKET:
                keep[j] = item

    rows = sorted((p for keep in buckets.values() for p in keep), key=lambda p: p[3])
    out = Path(__file__).resolve().parent.parent / "data" / "puzzles.csv.gz"
    with gzip.open(out, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "fen", "moves", "rating", "themes"])
        w.writerows(rows)
    print(f"{len(rows)} puzzles -> {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1])
