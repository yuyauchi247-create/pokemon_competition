"""配布カードプールの分析。デッキ構築の材料を把握するための要約を出力。"""
from collections import Counter
from sim_env import all_card_data, CardType, EnergyType

cards = all_card_data()
print(f"総カード数: {len(cards)}")

by_type = Counter(c.cardType for c in cards)
print("\n=== 種類別枚数 ===")
for t in CardType:
    print(f"  {t.name:14} {by_type.get(t, 0)}")

ET = {e.value: e.name for e in EnergyType}


def line(c):
    flags = []
    if c.basic: flags.append("たね")
    if c.stage1: flags.append("1進化")
    if c.stage2: flags.append("2進化")
    if c.ex: flags.append("ex")
    if c.megaEx: flags.append("MEGA")
    if c.tera: flags.append("tera")
    if c.aceSpec: flags.append("ACE")
    ef = c.evolvesFrom or ""
    return (f"  id={c.cardId:<5} {c.name:<32} "
            f"{'/'.join(flags):<14} type={ET.get(c.energyType,'?'):<9} "
            f"HP={c.hp:<4} from={ef}")


print("\n=== たねポケモンの ex / MEGA（主役候補・進化不要で使いやすい）===")
for c in cards:
    if c.cardType == CardType.POKEMON and c.basic and (c.ex or c.megaEx):
        print(line(c))

print("\n=== その他のたねポケモン（非ex・アタッカー/サポート役候補, HP120以上）===")
for c in cards:
    if (c.cardType == CardType.POKEMON and c.basic and not c.ex
            and not c.megaEx and c.hp >= 120):
        print(line(c))

print("\n=== 進化ポケモンの ex / MEGA（進化ラインの主役候補）===")
for c in cards:
    if c.cardType == CardType.POKEMON and (c.stage1 or c.stage2) and (c.ex or c.megaEx):
        print(line(c))
