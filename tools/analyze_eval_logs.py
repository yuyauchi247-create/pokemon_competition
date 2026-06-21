#!/usr/bin/env python3
"""個別AI評価の行動ログ(data/logs/eval/<job>/)を集計し、挑戦者の挙動を分析する。

run_tournament.py --log-dir で出力した「対戦と同じ形式」のログ(1試合=1JSON)を読み、
挑戦者(全試合に登場するAI)視点で、使用カード・進化/攻撃の速さ・決着理由などを集計する。

使い方:
    python3 tools/analyze_eval_logs.py data/logs/eval/va_analysis
    python3 tools/analyze_eval_logs.py va_dir base_dir   # 2つ並べて比較
"""
import glob
import json
import re
import sys
from collections import Counter
from pathlib import Path

BRACKET = re.compile(r"「([^」]*)」")
CRUSTLE = "イワパレス"
DWEBBLE = "イシズマイ"
# 注目カード（挑戦者の使用回数を見る）
WATCH_PLAY = ["むしとりセット", "エネルギー転送", "なかよしポフィン",
              "チェレン", "コック", "ジャンボメーカー", "ジャンボアイス"]


def _challenger_name(files):
    """全試合に共通して登場する名前＝挑戦者、を推定する。"""
    cnt = Counter()
    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        for k in ("player0", "player1"):
            cnt[d.get(k)] += 1
    # 最頻＝全試合に出る挑戦者
    return cnt.most_common(1)[0][0]


def analyze(log_dir):
    files = sorted(glob.glob(str(Path(log_dir) / "*.json")))
    files = [f for f in files if not f.endswith("_agents.json")]
    if not files:
        return None
    me = _challenger_name(files)

    R = {
        "name": me, "games": 0, "wins": 0, "losses": 0, "draws": 0,
        "turns": [], "first_evo_turn": [], "first_atk_turn": [],
        "crustle_attacks": [], "energy_on_crustle": [],
        "play": Counter(), "powerglass_attached": 0,
        "end_reason": Counter(),
        "atk_in_win": [], "atk_in_draw": [],
    }
    for f in files:
        d = json.load(open(f, encoding="utf-8"))
        side = "you" if d.get("player0") == me else "opp"
        res = d.get("result", -1)
        won = (res == 0 and side == "you") or (res == 1 and side == "opp")
        lost = (res == 1 and side == "you") or (res == 0 and side == "opp")
        R["games"] += 1
        R["wins"] += won
        R["losses"] += lost
        draw = not won and not lost
        R["draws"] += draw

        max_turn = 0
        first_evo = None
        first_atk = None
        atks = 0
        ene_on_crustle = 0
        for e in d.get("log", []):
            text = e.get("text", "")
            turn = e.get("turn") or 0
            max_turn = max(max_turn, turn)
            if "決着" in text:
                R["end_reason"][text] += 1
            if e.get("who") != side:
                continue
            br = BRACKET.findall(text)
            if "を出した／使った" in text and br:
                card = br[0]
                if card in WATCH_PLAY:
                    R["play"][card] += 1
                if card == "力の砂時計":
                    R["play"]["力の砂時計"] += 1
            if "につけた" in text and len(br) >= 2:
                attached, target = br[0], br[1]
                if attached == "力の砂時計":
                    R["powerglass_attached"] += 1
                if target == CRUSTLE and "エネルギー" in attached:
                    ene_on_crustle += 1
            if "に進化させた" in text and len(br) >= 2 and br[1] == CRUSTLE:
                if first_evo is None:
                    first_evo = turn
            if "のワザ" in text and br and br[0] == CRUSTLE:
                atks += 1
                if first_atk is None:
                    first_atk = turn

        R["turns"].append(max_turn)
        R["crustle_attacks"].append(atks)
        R["energy_on_crustle"].append(ene_on_crustle)
        if first_evo is not None:
            R["first_evo_turn"].append(first_evo)
        if first_atk is not None:
            R["first_atk_turn"].append(first_atk)
        if won:
            R["atk_in_win"].append(atks)
        elif draw:
            R["atk_in_draw"].append(atks)
    return R


def avg(xs):
    return sum(xs) / len(xs) if xs else 0.0


def report_one(R):
    g = R["games"]
    print(f"■ {R['name']}")
    print(f"  試合数: {g}  ({R['wins']}W {R['losses']}L {R['draws']}D, 勝率 {round(100*R['wins']/g)}%)")
    print(f"  平均ターン長: {avg(R['turns']):.1f}")
    print(f"  Crustle初進化ターン(平均): {avg(R['first_evo_turn']):.2f}  (進化できた試合 {len(R['first_evo_turn'])}/{g})")
    print(f"  Crustle初攻撃ターン(平均): {avg(R['first_atk_turn']):.2f}  (攻撃できた試合 {len(R['first_atk_turn'])}/{g})")
    print(f"  Crustle攻撃回数/試合(平均): {avg(R['crustle_attacks']):.2f}")
    print(f"    └ 勝ち試合: {avg(R['atk_in_win']):.2f}  /  引き分け試合: {avg(R['atk_in_draw']):.2f}")
    print(f"  Crustleへのエネ付与/試合(平均): {avg(R['energy_on_crustle']):.2f}")
    print(f"  力の砂時計をCrustleに装着した回数(合計/平均): {R['powerglass_attached']} / {R['powerglass_attached']/g:.2f}")
    print("  使用カード回数/試合(平均):")
    for c in ["むしとりセット", "エネルギー転送", "力の砂時計", "なかよしポフィン", "チェレン", "コック"]:
        print(f"    {c}: {R['play'].get(c,0)/g:.2f}  (合計 {R['play'].get(c,0)})")
    print("  決着理由(上位):")
    for k, v in R["end_reason"].most_common(5):
        print(f"    {v:5d}  {k}")


def main():
    dirs = sys.argv[1:]
    if not dirs:
        print("usage: analyze_eval_logs.py <log_dir> [<log_dir2>]")
        return
    results = [analyze(d) for d in dirs]
    for R in results:
        if R is None:
            print("(ログが見つかりません)")
            continue
        report_one(R)
        print()
    if len(results) == 2 and all(results):
        a, b = results  # a=1つ目, b=2つ目
        print("================ 比較（1つ目 − 2つ目）================")
        print(f"  対象: {a['name']}  vs  {b['name']}")
        print(f"  勝率: {round(100*a['wins']/a['games'])}% vs {round(100*b['wins']/b['games'])}%  "
              f"({round(100*a['wins']/a['games'])-round(100*b['wins']/b['games']):+d}pt)")
        print(f"  初攻撃ターン: {avg(a['first_atk_turn']):.2f} vs {avg(b['first_atk_turn']):.2f}  "
              f"({avg(a['first_atk_turn'])-avg(b['first_atk_turn']):+.2f})")
        print(f"  攻撃回数/試合: {avg(a['crustle_attacks']):.2f} vs {avg(b['crustle_attacks']):.2f}  "
              f"({avg(a['crustle_attacks'])-avg(b['crustle_attacks']):+.2f})")
        print(f"  平均ターン長: {avg(a['turns']):.1f} vs {avg(b['turns']):.1f}")


if __name__ == "__main__":
    main()
