#!/usr/bin/env python3
"""ブラウザ(Playwright)で取得した各チームの生データを kaggle_top10_observations/ に整理する。

入力: 各チームを processTeam() でファイル保存した JSON 群（team01_*.json ...）。
  形式: {"rank":1,"team":"...","submissionId":...,"games":[{episodeId,endTime,result,
         opponentSubmissionId,numSteps,targetStep,observation:{...}}, ...]}
  observation は replay.json の steps[n-2] の能動側 observation から search_begin_input を除いたもの
  （= Kaggle ビジュアライザ右側の Observation パネルと同一）。

使い方:
  python3 organize.py <input_glob> <out_dir>
  例: python3 organize.py 'team*.json' kaggle_top10_observations
"""
import json
import os
import re
import sys
import glob


def sanitize(s):
    return re.sub(r"[^0-9A-Za-z_-]+", "_", s or "").strip("_") or "team"


def main():
    in_glob = sys.argv[1] if len(sys.argv) > 1 else "team*.json"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "kaggle_top10_observations"
    os.makedirs(out_dir, exist_ok=True)
    manifest = []
    total = 0
    for tf in sorted(glob.glob(in_glob)):
        d = json.load(open(tf, encoding="utf-8"))
        games = [g for g in d.get("games", []) if not g.get("error") and g.get("observation")]
        games.sort(key=lambda g: g.get("endTime") or "", reverse=True)  # 新しい順
        rank = d.get("rank", 0)
        team = d.get("team", "team")
        folder = os.path.join(out_dir, f"rank{int(rank):02d}_{sanitize(team)}")
        os.makedirs(folder, exist_ok=True)
        meta_games = []
        for i, g in enumerate(games, 1):
            fn = f"{i:02d}_ep{g['episodeId']}_{g.get('result', '?')}.json"
            with open(os.path.join(folder, fn), "w", encoding="utf-8") as fp:
                fp.write(json.dumps(g["observation"], ensure_ascii=False, indent=2))
            meta_games.append({"seq": i, "file": fn, "episodeId": g["episodeId"],
                               "result": g.get("result"), "endTime": g.get("endTime"),
                               "numSteps": g.get("numSteps"), "targetStep": g.get("targetStep"),
                               "opponentSubmissionId": g.get("opponentSubmissionId")})
            total += 1
        with open(os.path.join(folder, "_index.json"), "w", encoding="utf-8") as fp:
            json.dump({"rank": rank, "team": team, "submissionId": d.get("submissionId"),
                       "games": meta_games}, fp, ensure_ascii=False, indent=2)
        wl = {"W": 0, "L": 0, "D": 0}
        for g in games:
            wl[g.get("result", "?")] = wl.get(g.get("result", "?"), 0) + 1
        manifest.append({"rank": rank, "team": team, "folder": os.path.basename(folder),
                         "games": len(games), "record": wl})
        print(f"rank{rank:>2} {team[:24]:24} saved={len(games)}")
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fp:
        json.dump({"competition": "pokemon-tcg-ai-battle",
                   "description": "各チーム最高スコアエージェントの直近 N 試合。各JSONは "
                                  "replay.json の steps[n-2] 能動側 Observation（ビジュアライザ右パネル相当）。",
                   "teams": manifest}, fp, ensure_ascii=False, indent=2)
    print(f"TOTAL saved: {total}  -> {out_dir}")


if __name__ == "__main__":
    main()
