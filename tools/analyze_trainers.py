"""トレーナーズ・エネルギーの在庫を一覧化（デッキのエンジン把握用）。"""
from sim_env import all_card_data, CardType, EnergyType

cards = all_card_data()
ET = {e.value: e.name for e in EnergyType}

for label, ct in [("SUPPORTER（サポート）", CardType.SUPPORTER),
                  ("ITEM（グッズ）", CardType.ITEM),
                  ("TOOL（どうぐ）", CardType.TOOL),
                  ("STADIUM（スタジアム）", CardType.STADIUM)]:
    rows = [c for c in cards if c.cardType == ct]
    print(f"\n=== {label}  ({len(rows)}) ===")
    for c in rows:
        ace = " [ACE]" if c.aceSpec else ""
        print(f"  id={c.cardId:<5} {c.name}{ace}")

print("\n=== BASIC_ENERGY ===")
for c in cards:
    if c.cardType == CardType.BASIC_ENERGY:
        print(f"  id={c.cardId:<5} {c.name}  type={ET.get(c.energyType)}")

print("\n=== SPECIAL_ENERGY ===")
for c in cards:
    if c.cardType == CardType.SPECIAL_ENERGY:
        print(f"  id={c.cardId:<5} {c.name}")
