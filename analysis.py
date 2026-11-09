"""Game parsing and chess.com-style move classification.

Engine evals come from the browser (Stockfish WASM); this module turns them into a report.
"""
import csv
import io
import math
from pathlib import Path

import chess
import chess.pgn

VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}


def _load_book():
    book = {}
    with open(Path(__file__).parent / "data" / "openings.tsv", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            game = chess.pgn.read_game(io.StringIO(row["pgn"]))
            board = game.end().board()
            book[board.epd()] = f'{row["eco"]} {row["name"]}'
    return book


BOOK = _load_book()


def read_game(pgn):
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None or game.errors or game.next() is None:
        raise ValueError("Не удалось прочитать PGN")
    return game


def parse_game(pgn):
    """Positions the browser must evaluate."""
    game = read_game(pgn)
    board = game.board()
    fens = [board.fen()]
    for move in game.mainline_moves():
        board.push(move)
        fens.append(board.fen())
    return {"pgn": pgn, "fens": fens, "headers": dict(game.headers)}


def win_prob(score):
    """White win probability from a white-POV score {'cp': n} or {'mate': n}."""
    if "mate" in score:
        return 1.0 if score["mate"] > 0 else 0.0
    return 1 / (1 + math.exp(-0.00368208 * score["cp"]))


# Accuracy constants fitted to chess.com on 40 reviewed games. Mean abs error vs chess.com:
# 3.4 with Stockfish 17.1 lite depth 14, 3.2 with full Stockfish 17.1 at 1 s/position (refitting gained nothing).
ACC_SIGMOID, ACC_DECAY, ACC_POWER = 0.0022, 0.135, 0.5


def accuracy_wp(score, sigmoid=ACC_SIGMOID):
    """White win % on the flatter curve used only for accuracy; mate counts as fully won/lost."""
    cp = score["cp"] if "cp" in score else (10000 if score["mate"] > 0 else -10000)
    return 100 / (1 + math.exp(-sigmoid * cp))


def move_accuracy(before, after, decay=ACC_DECAY):
    """Mover's win % before and after the move."""
    return min(100.0, max(0.0, 103.1668 * math.exp(-decay * max(0.0, before - after)) - 3.1669))


def game_accuracy(accs, p=ACC_POWER):
    # power mean: p < 1 makes blunders pull the score down harder than a plain mean
    return (sum(max(a, 0.01) ** p for a in accs) / len(accs)) ** (1 / p)


def _capture_gains(board, color):
    """Square -> material the side to move wins by capturing a non-pawn piece of `color` there."""
    gains = {}
    for m in board.legal_moves:
        target = board.piece_at(m.to_square)
        if target and target.color == color and target.piece_type != chess.PAWN:
            g = VALUE[target.piece_type]
            if board.is_attacked_by(color, m.to_square):
                g -= VALUE[board.piece_type_at(m.from_square)]
            gains[m.to_square] = max(gains.get(m.to_square, 0), g)
    return gains


def _offered(before, move):
    """Net material the mover leaves capturable (non-pawn pieces) after `move`."""
    taken = 1 if before.is_en_passant(move) else VALUE.get(before.piece_type_at(move.to_square), 0)
    after = before.copy(stack=False)
    after.push(move)
    return max(_capture_gains(after, before.turn).values(), default=0) - taken


def is_sacrifice(before, move):
    """Move leaves material en prise although another move would have given up less.
    A piece that is lost whatever you do (trapped, forced check reply) is no sacrifice."""
    offered = _offered(before, move)
    return offered > 0 and any(_offered(before, m) < offered for m in before.legal_moves if m != move)


def classify(loss, is_best, sac, wp_best, wp_after, wp_second, prev_cls, recapture=False):
    # Thresholds = chess.com expected-points model.
    # ponytail: heuristic brilliant/great/miss rules, chess.com's exact rules are private
    if loss <= 0.02 and sac and wp_after >= 0.45 and wp_second is not None and wp_second < 0.97:
        return "brilliant"
    if is_best and not recapture and wp_second is not None and wp_best - wp_second >= 0.15:
        return "great"
    if is_best:
        return "best"
    if prev_cls in ("mistake", "blunder") and loss >= 0.10 and wp_after >= 0.3:
        return "miss"
    for limit, cls in ((0.02, "excellent"), (0.05, "good"), (0.10, "inaccuracy"), (0.20, "mistake")):
        if loss < limit:
            return cls
    return "blunder"


def _score(board, ev):
    """White-POV score of a position; terminal positions come without engine eval."""
    if ev:
        return {k: ev[k] for k in ("cp", "mate") if k in ev}
    if board.is_checkmate():
        return {"mate": -1 if board.turn == chess.WHITE else 1}
    return {"cp": 0}


def judge_move(before, move, ev, score_after, prev_cls=None, recapture=False):
    """Classify one move. ev: engine eval of `before` (white POV, best/second); score_after: white-POV score after it.
    Returns (cls, best_san, best_uci); best_* only when a better move existed."""
    ev = ev or {}
    pov = (lambda p: p) if before.turn == chess.WHITE else (lambda p: 1 - p)
    wp_best, wp_after = pov(win_prob(_score(before, ev))), pov(win_prob(score_after))
    is_best = move.uci() == ev.get("best")
    if is_best:
        wp_after = wp_best  # same line, avoid depth noise between two searches
    loss = max(0.0, wp_best - wp_after)
    is_best = is_best or loss < 0.005
    wp_second = pov(win_prob(ev["second"])) if ev.get("second") else None
    sac = loss <= 0.02 and is_sacrifice(before, move)  # only matters for brilliant, skip the slow check otherwise
    cls = classify(loss, is_best, sac, wp_best, wp_after, wp_second, prev_cls, recapture)
    best = chess.Move.from_uci(ev["best"]) if ev.get("best") and not is_best else None
    if best and best not in before.legal_moves:
        best = None
    return cls, best and before.san(best), best and best.uci()


def build_report(pgn, evals):
    """evals[i] = {'best': uci, 'cp'|'mate': white POV, 'second': {'cp'|'mate'}|None} or None."""
    game = read_game(pgn)
    board = game.board()
    moves = list(game.mainline_moves())
    if len(evals) != len(moves) + 1:
        raise ValueError("Число оценок не совпадает с партией")

    boards = [board.copy()]
    for m in moves:
        board.push(m)
        boards.append(board.copy(stack=False))
    scores = [_score(b, e) for b, e in zip(boards, evals)]
    wps = [win_prob(s) for s in scores]
    acc_wps = [accuracy_wp(s) for s in scores]

    out, stats, in_book, opening, prev_cls = [], {}, True, game.headers.get("Opening", ""), None
    for i, move in enumerate(moves):
        before, after = boards[i], boards[i + 1]
        in_book = in_book and after.epd() in BOOK
        if in_book:
            cls, opening, best_san, best_uci = "book", BOOK[after.epd()], None, None
        else:
            recapture = i > 0 and before.is_capture(move) and move.to_square == moves[i - 1].to_square
            cls, best_san, best_uci = judge_move(before, move, evals[i], scores[i + 1], prev_cls, recapture)
        prev_cls = cls

        white = before.turn == chess.WHITE
        side = stats.setdefault("w" if white else "b", {"acc": [], "counts": {}})
        a, b = acc_wps[i], acc_wps[i + 1]
        side["acc"].append(move_accuracy(a, b) if white else move_accuracy(100 - a, 100 - b))
        side["counts"][cls] = side["counts"].get(cls, 0) + 1
        out.append({
            "san": before.san(move), "uci": move.uci(), "fen": after.fen(), "cls": cls,
            "best_san": best_san, "best_uci": best_uci,
        })

    for side in stats.values():
        side["accuracy"] = round(game_accuracy(side.pop("acc")), 1)
    return {
        "headers": dict(game.headers), "opening": opening, "start_fen": boards[0].fen(),
        "scores": [dict(s, wp=round(w, 4)) for s, w in zip(scores, wps)], "moves": out, "stats": stats,
    }


if __name__ == "__main__":
    # sacrifice: Bxf7+ hangs bishop for pawn; quiet Nf3 does not
    b = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 2 3")
    assert is_sacrifice(b, chess.Move.from_uci("c4f7"))
    assert not is_sacrifice(chess.Board(), chess.Move.from_uci("g1f3"))
    # 29...Nc4 leaves the d5 knight to Qg5 although it could have moved away (chess.com: brilliant)
    b = chess.Board("1k6/1pp2p2/pq4b1/n2n2Q1/5P2/2P4B/P1PB4/2K5 b - - 2 29")
    assert is_sacrifice(b, chess.Move.from_uci("a5c4"))
    # 25...Kg8 answering Qe5+ cannot save the d5 bishop: no sacrifice
    b = chess.Board("7r/pp3pk1/8/3bQ3/3P2pq/7P/PPPB1PP1/6K1 b - - 1 25")
    assert not is_sacrifice(b, chess.Move.from_uci("g7g8"))
    # even trade is not a sacrifice
    b = chess.Board("rnbqkb1r/pppp1ppp/5n2/4p3/4P3/2N5/PPPP1PPP/R1BQKBNR w KQkq - 2 3")
    assert not is_sacrifice(b, chess.Move.from_uci("c3d5"))
    assert move_accuracy(60, 60) > 99.99 and move_accuracy(60, 70) > 99.99 and move_accuracy(90, 10) == 0
    assert round(game_accuracy([100, 100, 0]), 1) == 44.9  # one blunder costs more than a plain mean (66.7)
    assert classify(0.0, True, False, 0.5, 0.5, 0.49, None) == "best"
    assert classify(0.0, True, False, 0.5, 0.5, 0.2, None) == "great"
    assert classify(0.0, True, False, 0.5, 0.5, 0.2, None, recapture=True) == "best"
    assert classify(0.0, True, True, 0.6, 0.6, 0.5, None) == "brilliant"
    assert classify(0.3, False, False, 0.9, 0.6, 0.8, "blunder") == "miss"
    assert classify(0.3, False, False, 0.6, 0.3, 0.5, None) == "blunder"
    assert classify(0.07, False, False, 0.6, 0.53, 0.5, None) == "inaccuracy"
    pgn = "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. h4 *"
    r = build_report(pgn, [{"best": "e2e4", "cp": 30, "second": {"cp": 25}}] * 7 + [{"best": "g8f6", "cp": -200}])
    assert [m["cls"] for m in r["moves"]] == ["book"] * 6 + ["blunder"], r["moves"]
    assert "Ruy Lopez" in r["opening"]
    print("ok")
