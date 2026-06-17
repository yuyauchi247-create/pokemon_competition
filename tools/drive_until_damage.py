"""サーバAPIをランダムに進め、場のポケモンがダメージを受けた状態を作る（UI反映の確認用）。
さらに「攻撃対象の選択(CARD/ダメージ対象)」が出たかも記録する。"""
import random
import requests

B = "http://127.0.0.1:5000"


def new(seed):
    return requests.post(B + "/api/new", json={"seed": seed}).json()


def sel(p):
    return requests.post(B + "/api/select", json={"picks": p}).json()


target_select_seen = []
for seed in range(40):
    s = new(seed)
    for step in range(500):
        if s.get("result", -1) != -1:
            break
        sl = s.get("select")
        if not sl:
            break
        # 攻撃対象などのカード選択コンテキストを記録（context 13/14/15=ダメージ系）
        if sl.get("context") in (13, 14, 15):
            target_select_seen.append((seed, step, sl.get("context"), len(sl["options"])))
        n = len(sl["options"])
        k = random.Random(seed * 1000 + step).randint(sl["minCount"], min(sl["maxCount"], n))
        picks = random.Random(seed * 7 + step).sample(range(n), k)
        s = sel(picks)
        b = s.get("board")
        if b:
            for who in ("you", "opp"):
                a = b[who].get("active")
                if a and a["hp"] < a["maxHp"]:
                    print(f"ダメージ反映あり: seed{seed} step{step} -> "
                          f"{who} の {a['name']} が HP {a['hp']}/{a['maxHp']}")
                    print(f"  （このゲーム状態はブラウザにも反映されています）")
                    print(f"ダメージ対象の選択が出た回数: {len(target_select_seen)} {target_select_seen[:5]}")
                    raise SystemExit
print("ダメージ状態に到達せず")
print(f"ダメージ対象の選択が出た回数: {len(target_select_seen)}")
