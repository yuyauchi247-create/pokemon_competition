"""人 vs AI / AI vs AI ポケカ対戦の Web UI（Flask）。

起動:
    uv run tools/webapp/server.py
ブラウザで http://127.0.0.1:8000 を開く。

複数同時対戦:
    libcg は battle_ptr ごとに対戦状態を持つため、複数対戦を同時に保持できる。
    本サーバは対戦を gid（ゲームID）で管理し、各リクエストで対象対戦の ptr に
    切り替えてから cg を呼ぶ。Flask はシングルスレッドで動かすため、
    ptr の切り替えはリクエスト間で競合しない。
"""
import os
import random
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

# tools/ を import パスに追加して sim_env を使う
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import (Flask, jsonify, request, render_template,  # noqa: E402
                   send_from_directory, abort)
from selection import (  # noqa: E402
    DEFAULT_SAMPLE_DECK, SAMPLE_DECKS, SelectionError, deck_card_counts,
    extract_agent_code_from_ipynb, list_user_decks, load_custom_agent,
    parse_decklist_comments, parse_deck_csv_text,
    read_sample_deck, read_user_deck, sample_deck_options, save_user_deck,
    validate_ai_picks, validate_deck_for_builder,
)
from sim_env import (  # noqa: E402
    to_observation_class, battle_start, battle_select, battle_finish,
    set_active_battle, render_option, card_name, card_meta, card_detail,
    option_card, translate_logs, card_image_file, CARD_IMAGES_DIR,
    OptionType, AreaType, SelectContext, EnergyType,
)

HUMAN = 0
app = Flask(__name__, template_folder="templates", static_folder="static")

# ---- 複数同時対戦の管理 ----
# GAMES: gid -> ゲーム状態。mode は "human"（人 vs AI）/ "ai"（AI vs AI 観戦）。
# player_* はプレイヤー0側（human モードでは人間、ai モードではAI）。
GAMES = {}
MAX_GAMES = 16  # 同時に保持する対戦数の上限（超えたら古い・終了済みから破棄）


def _new_game_state(gid):
    return {"gid": gid, "ptr": None, "obs": None, "result": -1, "running": False,
            "rng": random.Random(), "events": [], "mode": "human",
            "player_type": "human", "player_agent": None, "player_label": "あなた",
            "opponent_type": "rule", "opponent_agent": None,
            "opponent_label": "ルールベースAI", "deck_label": "サンプルデッキ",
            # AI vs AI 観戦用: 各プレイヤーが手番のときに見えた手札を絶対index別に保持。
            "hand_cache": {}, "last_used": time.time()}


def _activate(g):
    """この対戦の battle_ptr をラッパに適用する（cg 呼び出し前に必須）。"""
    set_active_battle(g.get("ptr"))
    g["last_used"] = time.time()


def _get_game(gid):
    g = GAMES.get(gid)
    if g is not None:
        _activate(g)
    return g


def _destroy_game(gid):
    g = GAMES.pop(gid, None)
    if not g:
        return
    try:
        if g.get("ptr"):
            set_active_battle(g["ptr"])
            battle_finish()
    except Exception:
        pass
    for side in ("player", "opponent"):
        shutil.rmtree(_custom_agent_dir(gid, side), ignore_errors=True)


def _evict_if_needed():
    """上限を超えたら、終了済み→古い順に破棄してネイティブメモリを解放する。"""
    while len(GAMES) > MAX_GAMES:
        victim = min(GAMES.values(),
                     key=lambda x: (x["result"] == -1, x["last_used"]))
        _destroy_game(victim["gid"])


def _agent_pick(g, agent_type, agent, sel, obs):
    """指定エージェント（custom なら関数、それ以外はルールベース）で選択肢を決める。"""
    if agent_type == "custom" and agent:
        try:
            picks = agent(obs)
        except Exception as exc:
            raise SelectionError(f"カスタムAIの実行中にエラーが発生しました: {exc}") from exc
        return validate_ai_picks(picks, sel.minCount, sel.maxCount, len(sel.option))
    n = len(sel.option)
    k = g["rng"].randint(sel.minCount, min(sel.maxCount, n))
    return g["rng"].sample(range(n), k)


def _ai_pick(g, sel, obs):
    """相手（プレイヤー1）の手番を進めるための選択。"""
    return _agent_pick(g, g.get("opponent_type"), g.get("opponent_agent"), sel, obs)


def _selection_error(message, status=400):
    return jsonify({"error": message}), status


def _decode_upload(file_storage, label):
    if not file_storage or not file_storage.filename:
        raise SelectionError(f"{label}ファイルを選択してください。")
    data = file_storage.read()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SelectionError(f"{label}ファイルはUTF-8のCSV/Pythonファイルにしてください。") from exc


def _selected_deck(mode_field, sample_id_field, user_id_field, file_field):
    """フォームのフィールド名群からデッキを選ぶ（自分・相手で共通利用）。

    mode: sample / user / custom。未指定や不明なら sample 扱い。
    戻り値は (デッキ(list[int]), 表示ラベル)。
    """
    mode = request.form.get(mode_field, "sample")
    if mode == "custom":
        deck_file = request.files.get(file_field)
        deck = parse_deck_csv_text(_decode_upload(deck_file, "デッキCSV"))
        validate_deck_for_builder(deck)
        deck_name = deck_file.filename if deck_file is not None else "deck.csv"
        return deck, f"アップロードデッキ（{deck_name}）"
    if mode == "user":
        deck_id = request.form.get(user_id_field) or ""
        label = next((d["name"] for d in list_user_decks() if d["id"] == deck_id), "保存済みデッキ")
        return read_user_deck(deck_id), label
    # サンプルデッキ: ID で5種から選ぶ（未指定なら先頭）
    deck_id = request.form.get(sample_id_field) or SAMPLE_DECKS[0]["id"]
    label = next((d["name"] for d in SAMPLE_DECKS if d["id"] == deck_id), "サンプルデッキ")
    return read_sample_deck(deck_id), label


def _selected_player_deck():
    return _selected_deck("deck_mode", "deck_id", "user_deck_id", "deck_file")


def _agent_initial_observation():
    return {"select": None, "logs": [], "current": None, "search_begin_input": None}


def _custom_agent_dir(gid, side):
    """カスタムAIの一時作業ディレクトリ。対戦(gid)・side ごとに分けて衝突を防ぐ。"""
    return (Path(tempfile.gettempdir()) / "pokemon_competition_webapp_agent"
            / gid / side)


def _prepare_uploaded_agent(gid, field, side):
    """アップロードされた .py / .ipynb を main.py として配置する。

    戻り値は (agent_dir, code, filename)。.ipynb の場合はコードを抽出して main.py 化する。
    """
    src = request.files.get(field)
    if not src or not src.filename:
        raise SelectionError("カスタムAIファイルを選択してください。")
    raw = _decode_upload(src, "カスタムAI")
    name = src.filename
    code = extract_agent_code_from_ipynb(raw) if name.lower().endswith(".ipynb") else raw
    agent_dir = _custom_agent_dir(gid, side)
    if agent_dir.exists():
        shutil.rmtree(agent_dir, ignore_errors=True)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "main.py").write_text(code, encoding="utf-8")
    return agent_dir, code, name


def _import_agent_in_dir(agent_dir):
    """agent_dir を cwd にして main.py を読み込む。

    トップレベルで deck.csv を読むタイプ（Kaggle定番）に対応するため、
    import 時の cwd を agent_dir に固定する。呼ぶ前に deck.csv を置いておくこと。
    """
    old_cwd = Path.cwd()
    try:
        os.chdir(agent_dir)
        return load_custom_agent(agent_dir / "main.py")
    finally:
        os.chdir(old_cwd)


def _ask_agent_for_deck(agent_dir, default_deck):
    """deck.csv を読むタイプのAIに、サンプルデッキを置いた状態で初期デッキを尋ねる。"""
    shutil.copyfile(DEFAULT_SAMPLE_DECK, agent_dir / "deck.csv")
    agent = _import_agent_in_dir(agent_dir)
    old_cwd = Path.cwd()
    try:
        os.chdir(agent_dir)
        deck = agent(_agent_initial_observation())
    except Exception:
        deck = list(default_deck)
    finally:
        os.chdir(old_cwd)
    try:
        return validate_ai_picks(deck, 60, 60, 1000000)
    except SelectionError:
        return list(default_deck)


def _build_custom_side(gid, field, side, default_deck):
    """カスタムAIファイル1つから (agent, deck, label) を作る。

    デッキは次の順で解決する:
      1. ファイル内のデッキリスト注記（`= <id> # ×<n>`）が60枚そろえばそれを使う。
      2. 揃わなければ、サンプルデッキを置いてエージェントに初期デッキを尋ねる。
    解決したデッキは deck.csv として確定配置し、deck.csv を読むAIにも正しく渡す。
    """
    agent_dir, code, name = _prepare_uploaded_agent(gid, field, side)
    deck = parse_decklist_comments(code)
    if deck is not None:
        deck_source = "ファイル内のデッキ"
    else:
        deck = _ask_agent_for_deck(agent_dir, default_deck)
        deck_source = "AIが決定"
    # 確定デッキを deck.csv に書き、import 時に読まれるようにしてからエージェントを読み込む。
    (agent_dir / "deck.csv").write_text(
        "\n".join(str(c) for c in deck) + "\n", encoding="utf-8")
    agent = _import_agent_in_dir(agent_dir)
    return agent, deck, f"カスタムAI（{name}・{deck_source}）"


def _selected_opponent(gid, default_deck):
    mode = request.form.get("opponent_type", "rule")
    if mode != "custom":
        # ルールベースAI: 相手デッキも一覧から選べる。
        # opponent_deck_mode 未指定なら従来通りプレイヤーと同じデッキを使う。
        if request.form.get("opponent_deck_mode"):
            deck, deck_name = _selected_deck(
                "opponent_deck_mode", "opponent_deck_id",
                "opponent_user_deck_id", "opponent_deck_file",
            )
            return "rule", None, deck, f"ルールベースAI（{deck_name}）"
        return "rule", None, list(default_deck), "ルールベースAI"

    agent, deck, label = _build_custom_side(gid, "agent_file", "opponent", default_deck)
    return "custom", agent, deck, label


def _selected_player_agent_side(gid):
    """AI vs AI モードのプレイヤー0側エージェントを決める。"""
    ptype = request.form.get("player_type", "rule")
    if ptype != "custom":
        deck, deck_name = _selected_player_deck()
        return "rule", None, deck, f"ルールベースAI（{deck_name}）"
    default_deck = read_sample_deck(SAMPLE_DECKS[0]["id"])
    agent, deck, label = _build_custom_side(gid, "player_agent_file", "player", default_deck)
    return "custom", agent, deck, label


def _collect_events(g, obs):
    """obs.logs を日本語イベントに変換して蓄積。"""
    try:
        g["events"].extend(translate_logs(to_observation_class(obs), HUMAN))
    except Exception:
        pass


def _advance_until_human(g):
    """AIの手番を自動消化し、人間の手番 or 決着まで進める。
    呼び出し前に対象対戦を _activate(g) しておくこと。
    ログは最後に戻ってきた自分視点の obs から1回だけ集めるため、ここでは集めない
    （各プレイヤーの obs に同じ出来事が重複して載るのを避ける）。"""
    obs = g["obs"]
    steps = 0
    while steps < 100000:
        cur = obs.get("current")
        if cur is not None and cur.get("result", -1) != -1:
            g["result"] = cur["result"]
            g["running"] = False
            break
        if obs.get("select") is None:
            g["running"] = False
            break
        ob = to_observation_class(obs)
        you = ob.current.yourIndex if ob.current else HUMAN
        # AI vs AI 観戦用: 手番プレイヤーの手札をキャッシュ（相手手札を見せるため）。
        if g.get("mode") == "ai" and ob.current:
            try:
                g["hand_cache"][you] = [card_meta(c.id)
                                        for c in ob.current.players[you].hand]
            except Exception:
                pass
        if you == HUMAN:
            # 先攻/後攻の選択（IS_FIRST）はランダムで自動決定
            if ob.select and int(ob.select.context) == int(SelectContext.IS_FIRST):
                pick = g["rng"].randint(0, len(ob.select.option) - 1)
                obs = battle_select([pick])
                g["obs"] = obs
                steps += 1
                continue
            break  # それ以外の人間の入力待ち
        obs = battle_select(_ai_pick(g, ob.select, obs))
        g["obs"] = obs
        steps += 1
    g["obs"] = obs
    # 自分視点に戻った最終 obs のログ（＝自分の前回選択以降の全出来事）だけを記録
    _collect_events(g, g["obs"])


def _energy_summary(pk):
    """ポケモンについているエネルギーをタイプ別に集計する。"""
    counts = {}
    order = []
    for e in (pk.energies or []):
        try:
            name = EnergyType(int(e)).name
        except (ValueError, TypeError):
            name = "COLORLESS"
        if name not in counts:
            counts[name] = 0
            order.append(name)
        counts[name] += 1
    return [{"type": t, "count": counts[t]} for t in order]


def _poke_json(pk):
    if pk is None:
        return None
    meta = card_meta(pk.id)
    return {
        "id": pk.id, "name": meta["name"], "type": meta["type"],
        "flags": meta["flags"], "img": meta["img"],
        "hp": pk.hp, "maxHp": pk.maxHp,
        "energy": len(pk.energies),
        "energyTypes": _energy_summary(pk),
        "tools": [card_name(t.id) for t in (pk.tools or [])],
    }


def _state_json(g):
    obs = g["obs"]
    ob = to_observation_class(obs)
    st = ob.current
    ai_mode = g.get("mode") == "ai"
    out = {"gid": g.get("gid"), "result": g["result"], "running": g["running"],
           "mode": g.get("mode", "human"),
           "youLabel": g.get("player_label", "あなた"),
           "oppLabel": g.get("opponent_label"),
           "opponentLabel": g.get("opponent_label"),
           "deckLabel": g.get("deck_label")}
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

    you_side = side(you, reveal_hand=True)
    opp_side = side(opp, reveal_hand=False)
    # 現在の手番プレイヤーの手札はキャッシュしておく（観戦時に相手側へ流用するため）。
    g["hand_cache"][you] = you_side["hand"]
    if ai_mode:
        # AI vs AI: 相手の「最後に見えた手札」を公開する（同時公開は観測上できないため近似）。
        opp_side["hand"] = g["hand_cache"].get(opp)
    out["board"] = {
        "turn": st.turn,
        "you": you_side,
        "opp": opp_side,
    }
    out["events"] = g["events"]

    # 人間の手番なら選択肢を出す
    sel = ob.select
    if sel is not None and st.yourIndex == HUMAN and g["result"] == -1:
        opts = []
        for i, o in enumerate(sel.option):
            try:
                otype = OptionType(o.type).name
            except ValueError:
                otype = str(o.type)
            opts.append({
                "index": i, "label": render_option(o, st, sel),
                "card": option_card(o, st, sel),
                "optionType": otype,
                "area": getattr(o, "area", None), "srcIndex": getattr(o, "index", None),
                "inPlayArea": getattr(o, "inPlayArea", None),
                "inPlayIndex": getattr(o, "inPlayIndex", None),
                "attackId": getattr(o, "attackId", None),
                "number": getattr(o, "number", None),
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
    return render_template("selection.html")


@app.route("/builder")
def builder():
    return render_template("builder.html")


@app.route("/battle")
def battle():
    return render_template("battle.html")


@app.route("/api/config", methods=["GET"])
def api_config():
    """サンプル・保存済みデッキ一覧（中身プレビュー込み）を返す。"""
    sample_decks = []
    for opt in sample_deck_options():
        try:
            cards = deck_card_counts(read_sample_deck(opt["id"]))
        except SelectionError:
            cards = []
        sample_decks.append({**opt, "cards": _deck_preview(cards)})

    user_decks = []
    for opt in list_user_decks():
        try:
            cards = deck_card_counts(read_user_deck(opt["id"]))
        except SelectionError:
            cards = []
        user_decks.append({"id": opt["id"], "name": opt["name"], "cards": _deck_preview(cards)})
    return jsonify({"sampleDecks": sample_decks, "userDecks": user_decks})


def _deck_preview(counts):
    return [{**card_meta(c["cardId"]), "count": c["count"]} for c in counts]


@app.route("/api/cards", methods=["GET"])
def api_cards():
    """デッキ作成画面用: PDF 39ページまでの使用可能カード(ID 1..1267)。"""
    cards = []
    for cid in range(1, 1268):
        meta = card_meta(cid)
        detail = card_detail(cid)
        cards.append({
            **meta,
            "stage": detail.get("stage", ""),
            "typeJP": detail.get("typeJP", ""),
            "category": detail.get("category", ""),
            "rule": detail.get("rule", ""),
            "evolvesFrom": detail.get("evolvesFrom", ""),
        })
    return jsonify({"cards": cards})


@app.route("/api/user_decks", methods=["POST"])
def api_save_user_deck():
    payload = request.json or {}
    try:
        name = str(payload.get("name", "保存デッキ"))
        deck = [int(v) for v in payload.get("cards", [])]
        saved = save_user_deck(name, deck)
        counts = deck_card_counts(read_user_deck(saved["id"]))
        return jsonify({**saved, "cards": _deck_preview(counts)})
    except (SelectionError, ValueError, TypeError) as exc:
        return _selection_error(str(exc))


@app.route("/api/user_decks/<deck_id>", methods=["GET"])
def api_user_deck(deck_id):
    try:
        deck = read_user_deck(deck_id)
        opt = next((d for d in list_user_decks() if d["id"] == deck_id), {"id": deck_id, "name": deck_id})
        return jsonify({"id": deck_id, "name": opt["name"], "cards": _deck_preview(deck_card_counts(deck))})
    except SelectionError as exc:
        return _selection_error(str(exc), status=404)


@app.route("/api/card/<int:cid>", methods=["GET"])
def api_card(cid):
    return jsonify(card_detail(cid))


@app.route("/card_img/<int:cid>", methods=["GET"])
def card_img(cid):
    fn = card_image_file(cid)
    if not fn:
        abort(404)
    return send_from_directory(CARD_IMAGES_DIR, fn, max_age=86400)


NO_GAME = {"board": None, "select": None, "result": -1, "running": False,
           "gid": None, "mode": None}


def _request_gid():
    """リクエストから gid を取り出す（query または JSON body）。"""
    return request.args.get("gid") or (request.get_json(silent=True) or {}).get("gid")


@app.route("/api/new", methods=["POST"])
def api_new():
    gid = uuid.uuid4().hex
    g = _new_game_state(gid)
    seed = request.form.get("seed") or None
    g["rng"] = random.Random(seed)
    match_mode = "ai" if request.form.get("match_mode") == "ai" else "human"
    try:
        if match_mode == "ai":
            player_type, player_agent, player_deck, player_label = _selected_player_agent_side(gid)
            deck_label = player_label
        else:
            player_deck, deck_label = _selected_player_deck()
            player_type, player_agent, player_label = "human", None, "あなた"
        opponent_type, opponent_agent, opponent_deck, opponent_label = _selected_opponent(gid, player_deck)
    except SelectionError as exc:
        _destroy_game(gid)
        return _selection_error(str(exc))

    obs, start = battle_start(player_deck, opponent_deck)
    if obs is None:
        _destroy_game(gid)
        return jsonify({"error": f"battle start failed (errorType={start.errorType})"}), 500
    g["ptr"] = start.battlePtr
    g["obs"], g["result"], g["running"], g["events"] = obs, -1, True, []
    g["mode"] = match_mode
    g["player_type"], g["player_agent"], g["player_label"] = player_type, player_agent, player_label
    g["opponent_type"], g["opponent_agent"] = opponent_type, opponent_agent
    g["opponent_label"], g["deck_label"] = opponent_label, deck_label
    GAMES[gid] = g
    _evict_if_needed()  # 他対戦を破棄する際 ptr が切り替わるので、この後で再アクティブ化する
    _activate(g)
    try:
        _advance_until_human(g)
    except SelectionError as exc:
        return _selection_error(str(exc))
    return jsonify(_state_json(g))


@app.route("/api/state", methods=["GET"])
def api_state():
    g = _get_game(_request_gid())
    if g is None or g["obs"] is None:
        return jsonify(NO_GAME)
    return jsonify(_state_json(g))


@app.route("/api/select", methods=["POST"])
def api_select():
    g = _get_game(_request_gid())
    if g is None or g["obs"] is None:
        return jsonify(NO_GAME)
    if g["result"] != -1:
        return jsonify(_state_json(g))
    picks = (request.get_json(silent=True) or {}).get("picks", [])
    ob = to_observation_class(g["obs"])
    sel = ob.select
    # 妥当性チェック（不正入力ではシミュレータが例外を投げるため事前に弾く）
    n = len(sel.option)
    ok = (isinstance(picks, list) and all(isinstance(p, int) for p in picks)
          and sel.minCount <= len(picks) <= min(sel.maxCount, n)
          and all(0 <= p < n for p in picks) and len(set(picks)) == len(picks))
    if not ok:
        return jsonify({"error": "invalid picks", **_state_json(g)}), 400
    g["events"] = []
    g["obs"] = battle_select(picks)
    try:
        _advance_until_human(g)
    except SelectionError as exc:
        return jsonify({"error": str(exc), **_state_json(g)}), 400
    return jsonify(_state_json(g))


@app.route("/api/step", methods=["POST"])
def api_step():
    """AI vs AI モードで、プレイヤー0側のAIに1手ぶん進めさせる。"""
    g = _get_game(_request_gid())
    if g is None or g["obs"] is None:
        return jsonify(NO_GAME)
    if g["result"] != -1 or g.get("mode") != "ai":
        return jsonify(_state_json(g))
    ob = to_observation_class(g["obs"])
    sel = ob.select
    you = ob.current.yourIndex if ob.current else HUMAN
    try:
        if sel is None or you != HUMAN:
            # 念のため：相手手番が残っていれば消化する
            _advance_until_human(g)
            return jsonify(_state_json(g))
        picks = _agent_pick(g, g.get("player_type"), g.get("player_agent"), sel, g["obs"])
        g["events"] = []
        g["obs"] = battle_select(picks)
        _advance_until_human(g)
    except SelectionError as exc:
        return jsonify({"error": str(exc), **_state_json(g)}), 400
    return jsonify(_state_json(g))


if __name__ == "__main__":
    host = os.environ.get("POKECA_WEBAPP_HOST", "127.0.0.1")
    port = int(os.environ.get("POKECA_WEBAPP_PORT", "8000"))
    print("=" * 50)
    print(" ポケカ 人 vs AI  Web UI")
    print(f" ブラウザで http://127.0.0.1:{port} を開いてください")
    print("=" * 50)
    # ctypes グローバル状態のため reloader/マルチスレッドは無効化
    app.run(host=host, port=port, debug=False,
            use_reloader=False, threaded=False)
