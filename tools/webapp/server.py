"""人 vs AI ポケカ対戦の Web UI（Flask）。

起動:
    uv run tools/webapp/server.py
ブラウザで http://127.0.0.1:5000 を開く。

注意: 配布シミュレータ(cg)は ctypes のグローバル状態で1対戦のみ保持する。
そのため本サーバは「同時に1ゲーム」を前提（ローカルで自分が遊ぶ用途）。
"""
import os
import random
import sys
from pathlib import Path

# tools/ を import パスに追加して sim_env を使う
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import (Flask, jsonify, request, render_template,  # noqa: E402
                   send_from_directory, abort)
from sim_env import (  # noqa: E402
    to_observation_class, battle_start, battle_select, battle_finish,
    read_deck, render_option, card_name, card_meta, card_detail,
    option_card, translate_logs, card_image_file, CARD_IMAGES_DIR,
    OptionType, AreaType,
)

HUMAN = 0
app = Flask(__name__, template_folder="templates", static_folder="static")

# ---- ゲーム状態（プロセスにつき1つ）----
G = {"obs": None, "result": -1, "running": False, "rng": random.Random(),
     "events": []}


def _ai_pick(sel):
    n = len(sel.option)
    k = G["rng"].randint(sel.minCount, min(sel.maxCount, n))
    return G["rng"].sample(range(n), k)


def _collect_events(obs):
    """obs.logs を日本語イベントに変換して蓄積。"""
    try:
        G["events"].extend(translate_logs(to_observation_class(obs), HUMAN))
    except Exception:
        pass


def _advance_until_human():
    """AIの手番を自動消化し、人間の手番 or 決着まで進める。
    ログは最後に戻ってきた自分視点の obs から1回だけ集めるため、ここでは集めない
    （各プレイヤーの obs に同じ出来事が重複して載るのを避ける）。"""
    obs = G["obs"]
    steps = 0
    while steps < 100000:
        cur = obs.get("current")
        if cur is not None and cur.get("result", -1) != -1:
            G["result"] = cur["result"]
            G["running"] = False
            break
        if obs.get("select") is None:
            G["running"] = False
            break
        ob = to_observation_class(obs)
        you = ob.current.yourIndex if ob.current else HUMAN
        if you == HUMAN:
            break  # 人間の入力待ち
        obs = battle_select(_ai_pick(ob.select))
        G["obs"] = obs
        steps += 1
    G["obs"] = obs
    # 自分視点に戻った最終 obs のログ（＝自分の前回選択以降の全出来事）だけを記録
    _collect_events(G["obs"])


def _poke_json(pk):
    if pk is None:
        return None
    meta = card_meta(pk.id)
    return {
        "id": pk.id, "name": meta["name"], "type": meta["type"],
        "flags": meta["flags"], "img": meta["img"],
        "hp": pk.hp, "maxHp": pk.maxHp,
        "energy": len(pk.energies),
        "tools": [card_name(t.id) for t in (pk.tools or [])],
    }


def _state_json():
    obs = G["obs"]
    ob = to_observation_class(obs)
    st = ob.current
    out = {"result": G["result"], "running": G["running"]}
    if st is None:
        out["board"] = None
        out["select"] = None
        return out

    you, opp = st.yourIndex, 1 - st.yourIndex

    def side(idx, reveal_hand):
        p = st.players[idx]
        act = p.active[0] if p.active and p.active[0] else None
        return {
            "active": _poke_json(act),
            "bench": [_poke_json(b) for b in p.bench],
            "handCount": p.handCount,
            "deck": p.deckCount,
            "prize": len(p.prize),
            "discard": len(p.discard),
            "hand": ([card_meta(c.id) for c in p.hand]
                     if (reveal_hand and p.hand) else None),
            "discardList": [card_meta(c.id) for c in p.discard],  # トラッシュは公開情報
            "conditions": [c for c, v in (("どく", p.poisoned), ("やけど", p.burned),
                           ("ねむり", p.asleep), ("まひ", p.paralyzed),
                           ("こんらん", p.confused)) if v],
        }

    out["board"] = {
        "turn": st.turn,
        "you": side(you, reveal_hand=True),
        "opp": side(opp, reveal_hand=False),
    }
    out["events"] = G["events"]

    # 人間の手番なら選択肢を出す
    sel = ob.select
    if sel is not None and st.yourIndex == HUMAN and G["result"] == -1:
        opts = []
        for i, o in enumerate(sel.option):
            try:
                otype = OptionType(o.type).name
            except ValueError:
                otype = str(o.type)
            opts.append({
                "index": i, "label": render_option(o, st), "card": option_card(o, st),
                "optionType": otype,
                "area": getattr(o, "area", None), "srcIndex": getattr(o, "index", None),
                "inPlayArea": getattr(o, "inPlayArea", None),
                "inPlayIndex": getattr(o, "inPlayIndex", None),
                "attackId": getattr(o, "attackId", None),
            })
        out["select"] = {
            "context": int(sel.context),
            "minCount": sel.minCount,
            "maxCount": min(sel.maxCount, len(sel.option)),
            "options": opts,
        }
    else:
        out["select"] = None
    return out


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/card/<int:cid>", methods=["GET"])
def api_card(cid):
    return jsonify(card_detail(cid))


@app.route("/card_img/<int:cid>", methods=["GET"])
def card_img(cid):
    fn = card_image_file(cid)
    if not fn:
        abort(404)
    return send_from_directory(CARD_IMAGES_DIR, fn, max_age=86400)


@app.route("/api/new", methods=["POST"])
def api_new():
    if G["obs"] is not None:
        try:
            battle_finish()
        except Exception:
            pass
    seed = (request.json or {}).get("seed")
    G["rng"] = random.Random(seed)
    deck = read_deck()
    obs, start = battle_start(deck, list(deck))
    if obs is None:
        return jsonify({"error": f"battle start failed (errorType={start.errorType})"}), 500
    G["obs"], G["result"], G["running"], G["events"] = obs, -1, True, []
    _advance_until_human()
    return jsonify(_state_json())


@app.route("/api/state", methods=["GET"])
def api_state():
    if G["obs"] is None:
        return jsonify({"board": None, "select": None, "result": -1, "running": False})
    return jsonify(_state_json())


@app.route("/api/select", methods=["POST"])
def api_select():
    if G["obs"] is None or G["result"] != -1:
        return jsonify(_state_json())
    picks = (request.json or {}).get("picks", [])
    ob = to_observation_class(G["obs"])
    sel = ob.select
    # 妥当性チェック（不正入力ではシミュレータが例外を投げるため事前に弾く）
    n = len(sel.option)
    ok = (isinstance(picks, list) and all(isinstance(p, int) for p in picks)
          and sel.minCount <= len(picks) <= min(sel.maxCount, n)
          and all(0 <= p < n for p in picks) and len(set(picks)) == len(picks))
    if not ok:
        return jsonify({"error": "invalid picks", **_state_json()}), 400
    G["events"] = []
    G["obs"] = battle_select(picks)
    _advance_until_human()
    return jsonify(_state_json())


if __name__ == "__main__":
    print("=" * 50)
    print(" ポケカ 人 vs AI  Web UI")
    print(" ブラウザで http://127.0.0.1:5000 を開いてください")
    print("=" * 50)
    # ctypes グローバル状態のため reloader/マルチスレッドは無効化
    app.run(host="127.0.0.1", port=5000, debug=False,
            use_reloader=False, threaded=False)
