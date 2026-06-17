"""弱点(×2)・抵抗が正しく適用されるかを、型違いのデッキで検証する。

deck0 = サンダー(雷)×4 + 基本雷エネルギー×56
deck1 = サンプルデッキ（カイオーガ＝雷弱点 を含む水デッキ）
雷タイプのワザが「雷弱点」の相手に当たると ダメージ×2 になるはず。
"""
import random
from collections import Counter

from sim_env import (to_observation_class, battle_start, battle_select,
                     battle_finish, read_deck, card_name, all_card_data,
                     all_attack, EnergyType, LogType)

CARDS = {c.cardId: c for c in all_card_data()}
ATKS = {a.attackId: a for a in all_attack()}
ET = {e.value: e.name for e in EnergyType}

records = []


def scan(ob):
    logs = ob.logs or []
    for i, lg in enumerate(logs):
        if LogType(lg.type) != LogType.ATTACK:
            continue
        atk_cid, atk_id = getattr(lg, "cardId", None), getattr(lg, "attackId", None)
        for l2 in logs[i + 1:]:
            if LogType(l2.type) == LogType.ATTACK:
                break
            if (LogType(l2.type) == LogType.HP_CHANGE and l2.value is not None
                    and l2.value < 0 and not getattr(l2, "putDamageCounter", False)):
                records.append((atk_cid, atk_id, getattr(l2, "cardId", None), -l2.value))


def main():
    deck0 = [514] * 4 + [4] * 56          # サンダー(514) + 基本雷エネルギー(4)
    deck1 = read_deck()                    # サンプル水デッキ
    for g in range(60):
        rng = random.Random(1000 + g)
        obs, start = battle_start(list(deck0), list(deck1))
        if obs is None:
            print(f"デッキ不正 errorType={start.errorType}"); return
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

    print(f"攻撃イベント {len(records)} 件\n")
    print(f"{'攻撃':<12}{'ワザ':<16}{'型':>5}{'記載':>5} | {'対象':<12}{'弱点':>9} |"
          f"{'実ダメ':>6} {'×2?':>5}  判定")
    print("-" * 84)
    seen, hits = set(), Counter()
    for atk_cid, atk_id, tgt_cid, dealt in records:
        atk, a, tgt = CARDS.get(atk_cid), ATKS.get(atk_id), CARDS.get(tgt_cid)
        if not (atk and a and tgt):
            continue
        weak = tgt.weakness is not None and tgt.weakness == atk.energyType
        if not weak:
            continue  # 弱点が絡む攻撃だけ表示
        base = a.damage
        ratio = (dealt / base) if base else 0
        verdict = ("×2一致" if base and dealt == base * 2 else
                   ("効果込み" if a.text else "要確認"))
        hits[verdict] += 1
        key = (atk_id, dealt)
        if key in seen:
            continue
        seen.add(key)
        print(f"{card_name(atk_cid):<12}{a.name:<16}{ET.get(atk.energyType):>5}{base:>5} | "
              f"{card_name(tgt_cid):<12}{ET.get(tgt.weakness):>9} |"
              f"{dealt:>6} {('x'+str(round(ratio,1))) if base else '-':>5}  {verdict}")
    print("\n=== 弱点が絡む攻撃のサマリ ===")
    for k, v in hits.most_common():
        print(f"  {k}: {v}")
    if not hits:
        print("  （弱点が絡む攻撃が発生しませんでした）")


if __name__ == "__main__":
    main()
