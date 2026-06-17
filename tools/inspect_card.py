"""指定カードID（複数可）のワザ・コストを表示。デッキのエネルギー設計用。
使い方: uv run tools/inspect_card.py 957 210 37 ...
"""
import sys
from sim_env import all_card_data, all_attack, EnergyType, CardType

ids = [int(x) for x in sys.argv[1:]]
cards = {c.cardId: c for c in all_card_data()}
attacks = {a.attackId: a for a in all_attack()}
ET = {e.value: e.name[:4] for e in EnergyType}

for cid in ids:
    c = cards.get(cid)
    if not c:
        print(f"id={cid} 見つかりません"); continue
    flags = [f for f, v in (("たね", c.basic), ("1進", c.stage1), ("2進", c.stage2),
             ("ex", c.ex), ("MEGA", c.megaEx), ("tera", c.tera), ("ACE", c.aceSpec)) if v]
    print(f"\nid={cid} {c.name}  [{'/'.join(flags)}] type={ET.get(c.energyType)} "
          f"HP={c.hp} retreat={c.retreatCost} from={c.evolvesFrom or '-'}")
    for aid in c.attacks:
        a = attacks.get(aid)
        if not a:
            continue
        cost = "+".join(ET.get(e, str(e)) for e in a.energies) or "なし"
        print(f"    ワザ: {a.name}  コスト[{cost}] ダメージ{a.damage}")
        if a.text:
            print(f"         {a.text[:80]}")
