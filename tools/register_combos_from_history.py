#!/usr/bin/env python3
"""過去の対戦履歴(battle_history.json)から「エージェント × デッキ」の実使用ペアを
user_combos（対戦設定のペア プルダウン）へ登録する。

- デッキ同梱エージェントの「自分の同梱デッキ」ペアはスキップ
  （agent:<id> の自動ペアとしてプルダウンに常時出るため登録不要）。
- 既存 combo と同じ (agent_id, deck_mode, deck_id) はスキップ（冪等）。
- エージェント/デッキが現存しないペアはスキップ（警告表示）。

使い方:
    python3 tools/register_combos_from_history.py                   # ローカル履歴 → ローカル登録
    python3 tools/register_combos_from_history.py --history vps.json --out-dir /tmp/combos
    python3 tools/register_combos_from_history.py --dry-run         # 登録せず一覧表示のみ
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "webapp"))

from selection import (  # noqa: E402
    SAMPLE_DECKS, list_user_agents, list_user_combos, list_user_decks,
    read_user_agent_deck, save_user_combo,
)


def collect_pairs(history_path: Path):
    """battle_history.json の mode=ai エントリから (agent_id, deck_mode, deck_id) を集める。"""
    data = json.loads(history_path.read_text(encoding="utf-8"))
    pairs = {}
    for item in data.get("items", []):
        if item.get("mode") != "ai":
            continue
        f = item.get("form", {})
        for side in ("player", "opponent"):
            aid = (f.get(f"{side}_agent_id") or "").strip()
            dmode = (f.get(f"{side}_deck_mode") or "").strip()
            did = (f.get(f"{side}_deck_id") or f.get(f"{side}_user_deck_id")
                   or f.get(f"{side}_deck_agent_id") or "").strip()
            if aid and dmode and did:
                pairs.setdefault((aid, dmode, did), 0)
                pairs[(aid, dmode, did)] += 1
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", default=str(ROOT / "data" / "logs" / "battle_history.json"))
    ap.add_argument("--out-dir", default="", help="combo の出力先（既定: data/user_combos）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else None
    agents = {a["id"]: a["name"] for a in list_user_agents()}
    decks = {d["id"]: d["name"] for d in list_user_decks()}
    samples = {s["id"]: s["name"] for s in SAMPLE_DECKS}
    existing = {(c.get("agent_id"), c.get("deck_mode"), c.get("deck_id"))
                for c in list_user_combos(out_dir)}

    pairs = collect_pairs(Path(args.history))
    made = skipped = 0
    used_names = {c.get("name") for c in list_user_combos(out_dir)}
    for (aid, dmode, did), count in sorted(pairs.items(), key=lambda x: -x[1]):
        if aid not in agents:
            print(f"  skip(agent無し): {aid}")
            continue
        if dmode == "agent" and did == aid:
            continue  # 同梱デッキは自動ペア(agent:)で選べる
        if dmode == "user":
            deck_label = decks.get(did)
        elif dmode == "agent":
            deck_label = agents.get(did)
            if deck_label and not read_user_agent_deck(did):
                deck_label = None
        else:
            deck_label = samples.get(did)
        if not deck_label:
            print(f"  skip(デッキ無し): {agents[aid]} × {dmode}:{did}")
            continue
        if (aid, dmode, did) in existing:
            skipped += 1
            continue
        name = f"{agents[aid]} × {deck_label}"
        # 同名エージェント（別ID）由来のペア名衝突は連番で区別する
        if name in used_names:
            n = 2
            while f"{name} ({n})" in used_names:
                n += 1
            name = f"{name} ({n})"
        used_names.add(name)
        if args.dry_run:
            print(f"  [dry-run] {name}  ({count}戦で使用)")
            made += 1
            continue
        save_user_combo(name, did, aid, deck_label, agents[aid], dmode, combos_dir=out_dir)
        print(f"  登録: {name}  ({count}戦で使用)")
        made += 1
        time.sleep(0.005)  # combo_id が時刻ms由来のため衝突回避
    print(f"done: 登録 {made} 件 / 既存スキップ {skipped} 件")


if __name__ == "__main__":
    main()
