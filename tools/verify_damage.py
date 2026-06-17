"""攻撃のダメージ判定を検証する。

自己対戦を回し、ATTACK ログ直後の HP_CHANGE（ダメージ）を集計。
各攻撃について「記載ダメージ」「弱点/抵抗の有無」「実際に減ったHP」を突き合わせ、
ワザの効果でダメージが変動しないもの（×や効果文なし）は 実ダメージ==記載ダメージ を検証する。
"""
import random
from collections import Counter

from sim_env import (to_observation_class, battle_start, battle_select,
                     battle_finish, read_deck, card_name, all_card_data,
                     all_attack, EnergyType, LogType)

CARDS = {c.cardId: c for c in all_card_data()}
ATKS = {a.attackId: a for a in all_attack()}
ET = {e.value: e.name for e in EnergyType}

records = []  # (attackerCid, attackId, targetCid, dealt)


def scan(ob):
    logs = ob.logs or []
    i = 0
    while i < len(logs):
        lg = logs[i]
        if LogType(lg.type) == LogType.ATTACK:
            atk_cid = getattr(lg, "cardId", None)
            atk_id = getattr(lg, "attackId", None)
            j = i + 1
            while j < len(logs) and LogType(logs[j].type) != LogType.ATTACK:
                l2 = logs[j]
                if (LogType(l2.type) == LogType.HP_CHANGE
                        and l2.value is not None and l2.value < 0
                        and not getattr(l2, "putDamageCounter", False)):
                    records.append((atk_cid, atk_id, getattr(l2, "cardId", None), -l2.value))
                j += 1
        i += 1


def main():
    for g in range(30):
        rng = random.Random(g)
        deck = read_deck()
        obs, _ = battle_start(deck, list(deck))
        steps = 0
        while steps < 6000:
            cur = obs.get("current")
            if cur is not None and cur.get("result", -1) != -1:
                break
            if obs.get("select") is None:
                break
            ob = to_observation_class(obs)
            scan(ob)
            sel = ob.select
            n = len(sel.option)
            k = rng.randint(sel.minCount, min(sel.maxCount, n))
            obs = battle_select(rng.sample(range(n), k))
            steps += 1
        battle_finish()

    print(f"記録した攻撃ダメージイベント: {len(records)} 件\n")
    print(f"{'攻撃ポケモン':<14}{'ワザ':<14}{'記載':>5}{'型':>5} | "
          f"{'対象':<14}{'弱点':>5}{'抵抗':>5} | {'実ダメ':>6} 判定")
    print("-" * 92)
    verdicts = Counter()
    shown = set()
    for atk_cid, atk_id, tgt_cid, dealt in records:
        atk = CARDS.get(atk_cid)
        a = ATKS.get(atk_id)
        tgt = CARDS.get(tgt_cid)
        if not a or not atk or not tgt:
            continue
        base = a.damage
        atype = atk.energyType
        weak = (tgt.weakness == atype) if tgt.weakness is not None else False
        resist = (tgt.resistance == atype) if tgt.resistance is not None else False
        variable = ("×" in (a.damage_text if hasattr(a, "damage_text") else "")) or bool(a.text)
        # 記載どおり（効果なし・弱点抵抗なし）の素直なワザだけ厳密判定
        if base > 0 and not a.text and not weak and not resist:
            verdict = "OK" if dealt == base else "★不一致"
        elif weak:
            verdict = f"弱点(実{dealt}/素{base})"
        elif resist:
            verdict = f"抵抗(実{dealt}/素{base})"
        elif a.text:
            verdict = "効果あり(変動可)"
        else:
            verdict = "-"
        verdicts[verdict.split("(")[0]] += 1
        key = (atk_cid, atk_id, dealt, verdict)
        if key in shown:
            continue
        shown.add(key)
        print(f"{card_name(atk_cid):<14}{a.name:<14}{base:>5}{ET.get(atype,'?'):>5} | "
              f"{card_name(tgt_cid):<14}"
              f"{ET.get(tgt.weakness,'-') if tgt.weakness is not None else '-':>5}"
              f"{ET.get(tgt.resistance,'-') if tgt.resistance is not None else '-':>5} | "
              f"{dealt:>6} {verdict}")
    print("\n=== 判定サマリ ===")
    for k, v in verdicts.most_common():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
