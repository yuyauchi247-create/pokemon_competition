"""MAINの選択肢でATTACH等がどんなフィールドを持つか生ダンプ（D&D実装の確認用）。"""
import random
from sim_env import (to_observation_class, battle_start, battle_select,
                     battle_finish, read_deck, card_name, SelectContext, OptionType, AreaType)

rng = random.Random(1)
deck = read_deck()
obs, _ = battle_start(deck, list(deck))
steps = 0
while steps < 400:
    cur = obs.get("current")
    if cur is not None and cur.get("result", -1) != -1:
        break
    if obs.get("select") is None:
        break
    ob = to_observation_class(obs)
    sel = ob.select
    has_attach = any(OptionType(o.type) == OptionType.ATTACH for o in sel.option)
    if has_attach:
        st = ob.current
        print(f"=== MAIN select (turn{st.turn}) context={SelectContext(sel.context).name} ===")
        you = st.players[st.yourIndex]
        act = you.active[0] if you.active and you.active[0] else None
        print(f"自分 active={card_name(act.id) if act else 'なし'} "
              f"bench={[card_name(b.id) for b in you.bench]}")
        for i, o in enumerate(sel.option):
            t = OptionType(o.type).name
            fields = {k: getattr(o, k) for k in
                      ("area", "index", "inPlayArea", "inPlayIndex", "cardId", "attackId")
                      if getattr(o, k, None) is not None}
            cn = card_name(o.cardId) if getattr(o, "cardId", None) else ""
            print(f"  [{i}] {t:8} {cn:18} {fields}")
        break
    n = len(sel.option)
    k = rng.randint(sel.minCount, min(sel.maxCount, n))
    obs = battle_select(rng.sample(range(n), k))
    steps += 1
battle_finish()
