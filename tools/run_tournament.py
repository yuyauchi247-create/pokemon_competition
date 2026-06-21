#!/usr/bin/env python3
"""総当たり戦を multiprocessing で並列実行する単体ランナー。

webapp の api_tournament と同じ対戦・集計ロジックを使い、N*(N-1) 試合を
複数CPUに手分けして実行する。親プロセスで全AIをロードし、fork した
ワーカーがそれを共有（コピーオンライト）する設計。

使い方:
    python3 tools/run_tournament.py                 # 全AI・全コアで実行
    python3 tools/run_tournament.py --limit 6       # 先頭6体だけ（検証用）
    python3 tools/run_tournament.py --sequential     # 1コア順次（結果照合用）
"""
import os

# numpy/BLAS の内部スレッドを1本に固定する（numpy を import する前に必須）。
# 1ワーカー=1コアにして、プロセス並列とBLASスレッドの二重取り合いを防ぐ。
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "webapp"))

import server as S  # noqa: E402  webapp のヘルパー（ロード/対戦/集計）を再利用
from sim_env import (  # noqa: E402
    battle_start, battle_select, battle_finish, set_active_battle,
    to_observation_class,
)
from cg.api import SelectContext  # noqa: E402

# 親でロードしたAI。fork 後は各ワーカーが自分のコピーを持つ（読み取りのみ）。
AGENTS = []          # [{agent, deck, name, id}, ...]
MATCH_TIMEOUT = 120  # 1試合の上限秒（暴走AIが全体を止めるのを防ぐ）
LOG_DIR = None       # 設定時、各試合の全行動ログ(対戦と同じ形式)をここに保存
LOG_FRAMES = False   # 盤面スナップショット(frames)も保存するか（重いので既定OFF）


def _save_eval_log(g, tag):
    """1試合の行動ログを、対戦(_save_match_log)と同じ形式で LOG_DIR に保存する。"""
    if not LOG_DIR:
        return
    try:
        d = Path(LOG_DIR)
        d.mkdir(parents=True, exist_ok=True)
        result = g.get("result", -1)
        p0 = g.get("player_label", "P1")
        p1 = g.get("opponent_label", "P2")
        winner = {0: p0, 1: p1}.get(result, "引き分け/未確定")
        data = {
            "mode": "eval", "tag": tag,
            "player0": p0, "player1": p1,
            "result": result, "winner": winner,
            "log": g.get("log", []),
            "frames": g.get("frames", []),
        }
        (d / f"{tag}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _run_match_with_deadline(p0, p1, seed, deadline_s, tag=None):
    """_run_headless_match と同じだが、1試合に時間上限を付ける。

    上限超過は引き分け(-1)扱い。勝者index 0/1、引き分け -1 を返す。
    LOG_DIR が設定され tag が与えられた場合は、各手の全行動ログ（対戦と同じ
    translate_logs 形式）を蓄積し、試合終了時に _save_eval_log で保存する。
    """
    rng = random.Random(seed)
    obs, start = battle_start(p0["deck"], p1["deck"])
    if obs is None:
        return -1
    set_active_battle(start.battlePtr)
    logging_on = bool(LOG_DIR) and tag is not None
    g = None
    if logging_on:
        g = {"obs": obs, "events": [], "log": [], "frames": [], "hand_cache": {},
             "player_label": p0.get("name", "P1"),
             "opponent_label": p1.get("name", "P2"), "result": -1}
    result = -1
    t_end = time.time() + deadline_s
    try:
        for _ in range(20000):
            cur = obs.get("current")
            if cur is not None and cur.get("result", -1) != -1:
                result = cur["result"]
                break
            if obs.get("select") is None:
                break
            if time.time() > t_end:
                result = -1  # タイムアウト → 引き分け扱い
                break
            ob = to_observation_class(obs)
            sel = ob.select
            you = ob.current.yourIndex if ob.current else 0
            if int(sel.context) == int(SelectContext.IS_FIRST):
                picks = [rng.randint(0, len(sel.option) - 1)]
            else:
                picks = S._headless_pick(p0 if you == 0 else p1, sel, obs, rng)
            obs = battle_select(picks)
            if logging_on:
                g["obs"] = obs
                S._collect_events(g, obs)       # 対戦と同じ全行動イベントを蓄積
                if LOG_FRAMES:
                    S._capture_frame(g)
    finally:
        try:
            battle_finish()
        except Exception:
            pass
    if logging_on:
        g["result"] = result
        _save_eval_log(g, tag)
    return result


def _silence_fds():
    """このプロセスの stdout/stderr を /dev/null へ恒久リダイレクトする。

    登録AIが print する大量のデバッグ出力を握りつぶす（I/O律速＆ログ汚染対策）。
    FDレベルで潰すので C 拡張(libcg等)の出力も消える。
    """
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)


def _write_progress(path, done, total):
    """進捗をJSONファイルに書き出す（UIのプログレスバー用）。"""
    if not path:
        return
    try:
        Path(path).write_text(json.dumps({"done": done, "total": total}),
                              encoding="utf-8")
    except Exception:
        pass


def _run_task(task):
    """ワーカー: 1試合を実行して (i, j, result) を返す。

    task = (i, j, g, seed)。seed は呼び出し側が決める（挑戦者比較では挑戦者の
    indexに依存させず、相手＋手番＋試合番号だけで決める＝ペア比較でノイズ低減）。
    """
    i, j, g, seed = task
    tag = f"{i:03d}_{j:03d}_g{g}" if LOG_DIR else None
    res = _run_match_with_deadline(
        AGENTS[i], AGENTS[j], seed=seed,
        deadline_s=MATCH_TIMEOUT, tag=tag)
    return (i, j, res)


def _build_standings(results):
    """(i, j, result) の集計を api_tournament と同じ並びで作る。"""
    stand = {a["id"]: {"name": a["name"], "wins": 0, "losses": 0,
                       "draws": 0, "games": 0} for a in AGENTS}
    for i, j, res in results:
        id0, id1 = AGENTS[i]["id"], AGENTS[j]["id"]
        stand[id0]["games"] += 1
        stand[id1]["games"] += 1
        if res == 0:
            stand[id0]["wins"] += 1
            stand[id1]["losses"] += 1
        elif res == 1:
            stand[id1]["wins"] += 1
            stand[id0]["losses"] += 1
        else:
            stand[id0]["draws"] += 1
            stand[id1]["draws"] += 1
    standings = sorted(stand.values(), key=lambda s: (-s["wins"], s["losses"]))
    for s in standings:
        s["winRate"] = round(100 * s["wins"] / s["games"]) if s["games"] else 0
    return standings


def _resolve_challenger(spec):
    """--challenger 指定(id か index)を AGENTS のインデックスに解決する。"""
    spec = spec.strip()
    if spec == "":
        return None
    if spec.isdigit():
        i = int(spec)
        if 0 <= i < len(AGENTS):
            return i
    for i, a in enumerate(AGENTS):           # 完全一致(id)
        if a["id"] == spec:
            return i
    for i, a in enumerate(AGENTS):           # 部分一致(id/name)フォールバック
        if spec in a["id"] or spec in a["name"]:
            return i
    return None


def _build_challenger_summary(results, c):
    """挑戦者 c の対フィールド成績と、相手別の内訳を作る。"""
    me = AGENTS[c]
    total = {"wins": 0, "losses": 0, "draws": 0, "games": 0}
    per = {k: {"name": AGENTS[k]["name"], "id": AGENTS[k]["id"],
               "wins": 0, "losses": 0, "draws": 0, "games": 0}
           for k in range(len(AGENTS)) if k != c}
    for i, j, res in results:
        if c not in (i, j):
            continue
        opp = j if i == c else i
        won = (res == 0 and i == c) or (res == 1 and j == c)
        lost = (res == 1 and i == c) or (res == 0 and j == c)
        for bucket in (total, per[opp]):
            bucket["games"] += 1
            if won:
                bucket["wins"] += 1
            elif lost:
                bucket["losses"] += 1
            else:
                bucket["draws"] += 1
    total["winRate"] = round(100 * total["wins"] / total["games"]) if total["games"] else 0
    vs = sorted(per.values(), key=lambda s: -s["wins"])
    for s in vs:
        s["winRate"] = round(100 * s["wins"] / s["games"]) if s["games"] else 0
    return {"name": me["name"], "id": me["id"], **total}, vs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--limit", type=int, default=0,
                    help="先頭N体だけ使う（検証用、0=全部）")
    ap.add_argument("--indices", default="",
                    help="使うAIのindexをカンマ区切りで指定（検証用）")
    ap.add_argument("--timeout", type=int, default=120, help="1試合の上限秒")
    ap.add_argument("--games-per-pair", type=int, default=1,
                    help="各順序ペアを何試合やるか（勝率の安定用）")
    ap.add_argument("--challenger", default="",
                    help="このAI(id か index)を含む対戦だけ実行＝個別評価。"
                         "対フィールド勝率と相手別内訳を出す（O(N)で安い）")
    ap.add_argument("--sequential", action="store_true",
                    help="1コアで順次実行（並列結果との照合用）")
    ap.add_argument("--progress-file", default="",
                    help="進捗(JSON: {done,total})を書き出すファイル（UIのバー用）")
    ap.add_argument("--log-dir", default="",
                    help="設定すると各試合の全行動ログ(対戦と同じ形式)をこのフォルダに保存")
    ap.add_argument("--log-frames", action="store_true",
                    help="盤面スナップショット(frames)も保存（重い。既定は行動ログのみ）")
    ap.add_argument("--out", default=str(ROOT / "data" / "tournament_result.json"))
    args = ap.parse_args()

    global MATCH_TIMEOUT, LOG_DIR, LOG_FRAMES
    MATCH_TIMEOUT = args.timeout
    LOG_DIR = args.log_dir or None
    LOG_FRAMES = bool(args.log_frames)

    metas = S.list_user_agents()
    metas = sorted(metas, key=lambda m: m["id"])  # index/シードを実行間で固定（再現性）
    if args.indices:
        idx = [int(x) for x in args.indices.split(",") if x.strip() != ""]
        metas = [metas[i] for i in idx]
    elif args.limit:
        metas = metas[:args.limit]
    print(f"loading {len(metas)} agents...", flush=True)
    t0 = time.time()
    for m in metas:
        try:
            a = S._load_registered_agent(m["id"])
            a["name"], a["id"] = m["name"], m["id"]
            AGENTS.append(a)
        except Exception as e:
            print(f"  skip {m['id']}: {e}", flush=True)
    n = len(AGENTS)
    print(f"loaded {n} agents in {time.time() - t0:.1f}s", flush=True)

    if LOG_DIR:
        # ログのファイル名(index)を名前/IDに対応付けるための索引を書き出す。
        try:
            d = Path(LOG_DIR)
            d.mkdir(parents=True, exist_ok=True)
            (d / "_agents.json").write_text(json.dumps(
                {i: {"id": a["id"], "name": a["name"]} for i, a in enumerate(AGENTS)},
                ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    g_per = max(1, args.games_per_pair)
    challenger = _resolve_challenger(args.challenger)
    if args.challenger and challenger is None:
        print(f"error: challenger '{args.challenger}' が見つかりません", flush=True)
        return
    if challenger is not None:
        # 個別評価: 挑戦者を含む対戦だけ（home/away × g_per）。O(N)。
        # seed は相手(j)＋手番＋試合番号だけで決め、挑戦者のindexに依存させない。
        # → 別の挑戦者でも各相手に対し同一シャッフルで戦う＝ペア比較でノイズ低減。
        c = challenger
        tasks = []
        for j in range(n):
            if j == c:
                continue
            for g in range(g_per):
                tasks.append((c, j, g, j * 100003 + g))           # 挑戦者が先攻(player0)
                tasks.append((j, c, g, j * 100003 + 50000 + g))   # 挑戦者が後攻(player1)
        print(f"challenger 評価: {AGENTS[c]['name']} vs {n - 1}体", flush=True)
    else:
        tasks = [(i, j, g, i * 100003 + j * 97 + g)
                 for i in range(n) for j in range(n) if i != j
                 for g in range(g_per)]
    workers = 1 if args.sequential else args.workers
    print(f"{len(tasks)} matches ({g_per}/pair), workers={workers}, "
          f"timeout={MATCH_TIMEOUT}s", flush=True)

    total = len(tasks)
    _write_progress(args.progress_file, 0, total)
    results = []
    t0 = time.time()
    if args.sequential:
        # 順次でも AI 出力は潰す（標準FDを退避→devnull→復帰）。
        saved = os.dup(1), os.dup(2)
        _silence_fds()
        try:
            for k, tk in enumerate(tasks, 1):
                results.append(_run_task(tk))
                if k % 10 == 0:
                    _write_progress(args.progress_file, k, total)
        finally:
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            os.close(saved[0])
            os.close(saved[1])
    else:
        import multiprocessing as mp
        ctx = mp.get_context("fork")
        with ctx.Pool(workers, initializer=_silence_fds) as pool:
            done = 0
            for r in pool.imap_unordered(_run_task, tasks, chunksize=1):
                results.append(r)
                done += 1
                if done % 20 == 0:
                    _write_progress(args.progress_file, done, total)
                if done % 50 == 0:
                    print(f"  {done}/{total} ({time.time() - t0:.0f}s)",
                          flush=True)
    _write_progress(args.progress_file, total, total)
    dt = time.time() - t0
    print(f"done {len(results)} matches in {dt:.1f}s "
          f"({dt / max(1, len(results)) * 1000:.0f} ms/match wall)", flush=True)

    if challenger is not None:
        summary, vs = _build_challenger_summary(results, challenger)
        out = {"mode": "challenger", "matches": len(results),
               "elapsed_s": round(dt, 1), "games_per_pair": g_per,
               "challenger": summary, "vs": vs}
        Path(args.out).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.out}", flush=True)
        print(f"\n=== {summary['name']} 対フィールド ===", flush=True)
        print(f"  {summary['wins']}W {summary['losses']}L {summary['draws']}D "
              f"／勝率 {summary['winRate']}%（{summary['games']}試合）", flush=True)
        print("  得意な相手 / 苦手な相手:", flush=True)
        for s in vs[:3]:
            print(f"    ◎ {s['name']}: {s['wins']}-{s['losses']}-{s['draws']}", flush=True)
        for s in vs[-3:]:
            print(f"    ▲ {s['name']}: {s['wins']}-{s['losses']}-{s['draws']}", flush=True)
        return

    standings = _build_standings(results)
    out = {"n": n, "matches": len(results), "elapsed_s": round(dt, 1),
           "workers": workers, "standings": standings}
    Path(args.out).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {args.out}", flush=True)
    for rank, s in enumerate(standings[:10], 1):
        print(f"  {rank}. {s['name']}: {s['wins']}W {s['losses']}L "
              f"{s['draws']}D ({s['winRate']}%)", flush=True)


if __name__ == "__main__":
    main()
