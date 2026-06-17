"""相手(AI)のバトル場がダメージを受けた状態をサーバに作る（相手HP反映の確認用）。"""
import random
import requests

B = "http://127.0.0.1:5000"
new = lambda seed: requests.post(B + "/api/new", json={"seed": seed}).json()
sel = lambda p: requests.post(B + "/api/select", json={"picks": p}).json()

for seed in range(80):
    s = new(seed)
    for step in range(600):
        if s.get("result", -1) != -1:
            break
        sl = s.get("select")
        if not sl:
            break
        n = len(sl["options"])
        k = random.Random(seed * 1000 + step).randint(sl["minCount"], min(sl["maxCount"], n))
        picks = random.Random(seed * 7 + step).sample(range(n), k)
        s = sel(picks)
        b = s.get("board")
        if b:
            a = b["opp"].get("active")
            if a and a["hp"] < a["maxHp"]:
                print(f"相手HPにダメージ反映: seed{seed} step{step} -> "
                      f"相手の {a['name']} が HP {a['hp']}/{a['maxHp']}")
                raise SystemExit
print("相手ダメージ状態に到達せず")
