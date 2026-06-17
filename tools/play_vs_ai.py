"""人 vs AI でポケカ対戦できる最小CLIアプリ。

使い方（uv 仮想環境で）:
    uv run tools/play_vs_ai.py            # あなた(player0) vs ランダムAI(player1)
    uv run tools/play_vs_ai.py --demo     # AI vs AI（動作確認用・入力不要）
    uv run tools/play_vs_ai.py --seed 5   # 乱数シード指定

あなたの手番になると盤面と「できること（選択肢）」が表示されるので、
番号を入力して選びます（複数選ぶ場合はスペース区切り）。
ルール進行・合法手の判定はすべてシミュレータが行うので、反則手は選べません。
"""
import argparse
import random
import sys

from sim_env import (
    to_observation_class, battle_start, battle_select, battle_finish,
    read_deck, render_option, render_board,
)

HUMAN = 0  # あなたは player0


def ai_choose(sel, rng):
    """ランダムAI: 合法な範囲でランダムに選ぶ。"""
    n = len(sel.option)
    k = rng.randint(sel.minCount, min(sel.maxCount, n))
    return rng.sample(range(n), k)


def human_choose(ob):
    """人間に選ばせる。"""
    sel = ob.select
    if ob.current is not None:
        print(render_board(ob.current))
    print(f"\n▼ あなたの番です（{sel.minCount}〜{sel.maxCount}個 選んでください）")
    for i, o in enumerate(sel.option):
        print(f"  [{i}] {render_option(o, ob.current)}")
    while True:
        try:
            raw = input("番号を入力 (スペース区切り / q=投了) > ").strip()
        except EOFError:
            print("\n入力終了。投了します。")
            return None
        if raw.lower() in ("q", "quit"):
            return None
        try:
            picks = [int(x) for x in raw.split()]
        except ValueError:
            print("  数字で入力してください。")
            continue
        if len(picks) < sel.minCount or len(picks) > min(sel.maxCount, len(sel.option)):
            print(f"  個数は {sel.minCount}〜{min(sel.maxCount, len(sel.option))} 個です。")
            continue
        if any(p < 0 or p >= len(sel.option) for p in picks):
            print("  範囲外の番号です。")
            continue
        if len(set(picks)) != len(picks):
            print("  重複しています。")
            continue
        return picks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="AI vs AI（入力不要）")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-steps", type=int, default=100000)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    deck = read_deck()
    obs, start = battle_start(deck, list(deck))
    if obs is None:
        print(f"[NG] 対戦開始に失敗 (errorType={start.errorType})")
        return 1

    print("=" * 56)
    print("ポケカ 人 vs AI 対戦" + ("（DEMO: AI vs AI）" if args.demo else ""))
    print("=" * 56)

    steps = 0
    result = -1
    while steps < args.max_steps:
        cur = obs.get("current")
        if cur is not None and cur.get("result", -1) != -1:
            result = cur["result"]
            break
        if obs.get("select") is None:
            break
        ob = to_observation_class(obs)
        you = ob.current.yourIndex if ob.current else HUMAN
        if (not args.demo) and you == HUMAN:
            picks = human_choose(ob)
            if picks is None:  # 投了 / 入力終了
                battle_finish()
                print("\n投了しました。AIの勝ちです。")
                return 0
        else:
            picks = ai_choose(ob.select, rng)
        obs = battle_select(picks)
        steps += 1

    battle_finish()
    print("\n" + "=" * 56)
    if result == HUMAN:
        print(f"🎉 あなた(player{HUMAN})の勝ち！  (手数={steps})")
    elif result == 1 - HUMAN:
        print(f"AI(player{1-HUMAN})の勝ち。  (手数={steps})")
    elif result == 2:
        print(f"引き分け。  (手数={steps})")
    else:
        print(f"終了 (result={result}, 手数={steps})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
