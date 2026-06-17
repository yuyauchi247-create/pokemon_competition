"""英語カード名 → ポケモン種 → PokéAPI公式アートワークURL の対応表を作る。

PokéAPI のポケモン一覧を1回だけ取得し、各ポケモンカードに画像URLを割り当てて
data/card_images.json （{cardId: url}）に保存する。トレーナーズ/エネは対象外。
"""
import json
import re
import urllib.request

from sim_env import all_card_data, CardType, ROOT

ART = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/{}.png"


def fetch_index():
    url = "https://pokeapi.co/api/v2/pokemon?limit=100000"
    with urllib.request.urlopen(url, timeout=120) as r:
        data = json.load(r)
    idx = {}
    for it in data["results"]:
        pid = int(it["url"].rstrip("/").split("/")[-1])
        idx[it["name"]] = pid
    return idx


def norm(s):
    s = s.strip().lower()
    for ch in ["'", "’", ".", "(", ")", ":", "é"]:
        s = s.replace(ch, "e" if ch == "é" else "")
    return s.replace(" ", "-")


def species_keys(name):
    n = name
    n = re.sub(r"^.*?[’']s ", "", n)      # 所有格プレフィックス (Hop's / Team Rocket's など)
    n = re.sub(r"^Mega ", "", n)           # メガ
    n = re.sub(r"\s*(ex|EX)$", "", n).strip()
    full = norm(n)
    parts = n.split()
    cands = [full]
    if parts:
        last = norm(parts[-1])
        if last != full:
            cands.append(last)
    return cands


def main():
    idx = fetch_index()
    print(f"PokéAPI index: {len(idx)} 種")
    out, matched, total, miss = {}, 0, 0, []
    for c in all_card_data():
        if c.cardType != CardType.POKEMON:
            continue
        total += 1
        for k in species_keys(c.name):
            if k in idx:
                out[c.cardId] = ART.format(idx[k])
                matched += 1
                break
        else:
            miss.append(c.name)
    path = ROOT / "data" / "card_images.json"
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"matched {matched}/{total} ({round(100*matched/total)}%) -> {path}")
    print("未マッチ例:", miss[:25])


if __name__ == "__main__":
    main()
