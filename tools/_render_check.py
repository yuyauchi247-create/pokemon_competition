"""人間向け表示（盤面・選択肢）が読みやすいかの確認用。最初の数回ぶんを表示。"""
import random
from sim_env import (to_observation_class, battle_start, battle_select,
                     battle_finish, read_deck, render_option, render_board,
                     SelectContext)

rng = random.Random(7)
deck = read_deck()
obs, _ = battle_start(deck, list(deck))
shown = 0
while shown < 4:
    cur = obs.get("current")
    if cur is not None and cur.get("result", -1) != -1:
        break
    if obs.get("select") is None:
        break
    ob = to_observation_class(obs)
    sel = ob.select
    ctx = SelectContext(sel.context).name
    print("=" * 56)
    if ob.current is not None:
        print(render_board(ob.current))
    print(f"\n▼ 質問: {ctx} （{sel.minCount}〜{sel.maxCount}個）")
    for i, o in enumerate(sel.option):
        print(f"  [{i}] {render_option(o, ob.current)}")
    shown += 1
    n = len(sel.option)
    k = rng.randint(sel.minCount, min(sel.maxCount, n))
    obs = battle_select(rng.sample(range(n), k))
battle_finish()
print("=" * 56)
print("render check done")
