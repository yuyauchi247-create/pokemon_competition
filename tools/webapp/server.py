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
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# tools/ を import パスに追加して sim_env を使う
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import (Flask, jsonify, request, render_template,  # noqa: E402
                   send_from_directory, abort)
from selection import (  # noqa: E402
    DEFAULT_SAMPLE_DECK, SAMPLE_DECKS, SelectionError, deck_card_counts,
    delete_user_agent, extract_agent_code_from_ipynb, list_user_agents,
    list_user_decks, load_custom_agent, parse_decklist_comments, parse_deck_csv_text,
    preview_agent_upload, read_sample_deck, read_user_agent_code,
    read_user_agent_deck, read_user_deck, user_agent_dir,
    sample_deck_options, save_user_agent, save_user_deck,
    update_user_agent_deck, update_user_deck, delete_user_deck,
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

# 公開時の任意コード実行対策: カスタムAI(.py/.ipynb)アップロードの可否。
# 公開サーバーでは POKECA_ALLOW_AGENT_UPLOAD=0 にして無効化すること（exec によるRCE回避）。
ALLOW_AGENT_UPLOAD = os.environ.get("POKECA_ALLOW_AGENT_UPLOAD", "1") != "0"

# 対戦ログの保存先（対戦ごとに1ファイル）。
LOGS_DIR = Path(__file__).resolve().parents[2] / "data" / "logs"

# 個別AI評価（challenger）のジョブ置き場と実行スクリプト。
APP_ROOT = Path(__file__).resolve().parents[2]
EVAL_JOBS_DIR = Path(tempfile.gettempdir()) / "pokeca_eval_jobs"
EVAL_GAMES_PER_PAIR = 10  # 個別評価: 各相手と先攻/後攻×この試合数

# ---- 複数同時対戦の管理 ----
# GAMES: gid -> ゲーム状態。mode は "human"（人 vs AI）/ "ai"（AI vs AI 観戦）。
# player_* はプレイヤー0側（human モードでは人間、ai モードではAI）。
GAMES = {}
MAX_GAMES = 16  # 同時に保持する対戦数の上限（超えたら古い・終了済みから破棄）


def _new_game_state(gid):
    return {"gid": gid, "ptr": None, "obs": None, "result": -1, "running": False,
            "rng": random.Random(), "events": [], "mode": "human", "ver": 0,
            "controlled": {HUMAN},  # 外部操作する手番（_advance_until_human 参照）
            "player_type": "human", "player_agent": None, "player_label": "あなた",
            "opponent_type": "rule", "opponent_agent": None,
            "opponent_label": "ルールベースAI", "deck_label": "サンプルデッキ",
            # AI vs AI / PvP 用: 各プレイヤーが手番のときに見えた手札を絶対index別に保持。
            "hand_cache": {}, "last_used": time.time(),
            # --- オンライン人 vs 人(pvp) 用 ---
            "status": "playing",   # "waiting"（相手待ち）/ "playing"
            "slots": {},           # token -> playerIndex(0/1)
            "decks": [None, None], # 各プレイヤーのデッキ
            "labels": {},          # playerIndex -> 表示名
            # --- ログ保存用 ---
            "log": [],             # 対戦全体のイベント蓄積 [{turn,who,text}]
            "frames": [],          # リプレイ用の盤面スナップショット列
            "started_at": datetime.now(timezone.utc).isoformat(),
            "log_saved": False}


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


def _agent_deck_list(agent_id):
    """登録AIのデッキ（60枚のID列）を返す。取得不可なら SelectionError。

    保存済みデッキ（meta.json / deck.csv。Kaggle出力からの回収を含む）を優先し、
    無ければコード内のデッキ注記を解析する。
    """
    deck = read_user_agent_deck(agent_id) or parse_decklist_comments(read_user_agent_code(agent_id))
    if not deck:
        raise SelectionError("この登録AIのデッキは使えません（ファイルから読み取れず、対戦時にAIが決定するタイプ）。別のデッキを選んでください。")
    return deck


def _selected_deck(mode_field, sample_id_field, user_id_field, file_field, agent_id_field=None):
    """フォームのフィールド名群からデッキを選ぶ（自分・相手で共通利用）。

    mode: sample / user / custom / agent（登録AIのデッキを流用）。未指定や不明なら sample 扱い。
    戻り値は (デッキ(list[int]), 表示ラベル)。
    """
    mode = request.form.get(mode_field, "sample")
    if mode == "agent" and agent_id_field:
        aid = request.form.get(agent_id_field) or ""
        deck = _agent_deck_list(aid)
        name = next((a["name"] for a in list_user_agents() if a["id"] == aid), aid)
        return deck, f"登録AIのデッキ（{name}）"
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
    return _selected_deck("deck_mode", "deck_id", "user_deck_id", "deck_file",
                          agent_id_field="deck_agent_id")


def _agent_initial_observation():
    return {"select": None, "logs": [], "current": None, "search_begin_input": None}


def _custom_agent_dir(gid, side):
    """カスタムAIの一時作業ディレクトリ。対戦(gid)・side ごとに分けて衝突を防ぐ。"""
    return (Path(tempfile.gettempdir()) / "pokemon_competition_webapp_agent"
            / gid / side)


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


def _copy_sibling_modules(src_dir, dst_dir):
    """登録AIに同梱された別モジュール(*.py)を実行ディレクトリへ複製する。

    MCTS の heuristic.py や FrostWall の value_net.py のように、main.py が
    同階層の自作モジュールを import するタイプ（複数ファイル提出）に対応する。
    """
    if not src_dir:
        return
    try:
        for f in Path(src_dir).glob("*.py"):
            if f.name != "main.py":
                shutil.copyfile(f, Path(dst_dir) / f.name)
    except OSError:
        pass


def _materialize_agent(gid, side, code, name, default_deck, forced_deck=None, extra_src=None):
    """コード文字列1つから (agent, deck, label) を作る（アップロード/登録 共通の核）。

    デッキ解決: ⓪保存済みデッキ(forced_deck。Kaggle回収等)があればそれ →
    ①コード内のデッキリスト注記が60枚そろえばそれ → ②AIに初期デッキを尋ねる。
    解決デッキを deck.csv として配置してからエージェントを読み込む（deck.csv を読むAI対応）。
    extra_src があれば同梱の別モジュール(*.py)も複製する（複数ファイル提出対応）。
    """
    agent_dir = _custom_agent_dir(gid, side)
    if agent_dir.exists():
        shutil.rmtree(agent_dir, ignore_errors=True)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "main.py").write_text(code, encoding="utf-8")
    _copy_sibling_modules(extra_src, agent_dir)
    if forced_deck and len(forced_deck) == 60:
        deck = list(forced_deck)
        deck_source = "登録デッキ"
    else:
        deck = parse_decklist_comments(code)
        if deck is not None:
            deck_source = "ファイル内のデッキ"
        else:
            deck = _ask_agent_for_deck(agent_dir, default_deck)
            deck_source = "AIが決定"
    (agent_dir / "deck.csv").write_text(
        "\n".join(str(c) for c in deck) + "\n", encoding="utf-8")
    agent = _import_agent_in_dir(agent_dir)
    return agent, deck, f"{name}・{deck_source}"


def _upload_agent_code(file_field):
    """アップロードファイルから (code, filename) を得る。"""
    if not ALLOW_AGENT_UPLOAD:
        raise SelectionError("このサーバーではカスタムAIのアップロードは無効化されています。")
    src = request.files.get(file_field)
    if not src or not src.filename:
        raise SelectionError("カスタムAIファイルを選択してください。")
    raw = _decode_upload(src, "カスタムAI")
    name = src.filename
    code = extract_agent_code_from_ipynb(raw) if name.lower().endswith(".ipynb") else raw
    return code, name


def _load_agent_side(gid, side, atype, file_field, id_field, default_deck):
    """atype = "registered"（登録済みID）/ "custom"（アップロード）。"""
    if atype == "registered":
        aid = request.form.get(id_field) or ""
        code = read_user_agent_code(aid)  # 不正IDは SelectionError
        name = next((a["name"] for a in list_user_agents() if a["id"] == aid), aid)
        forced = read_user_agent_deck(aid)  # 保存済みデッキ(Kaggle回収等)を優先
        return _materialize_agent(gid, side, code, f"登録AI（{name}）",
                                  default_deck, forced_deck=forced,
                                  extra_src=user_agent_dir(aid))
    code, fname = _upload_agent_code(file_field)
    return _materialize_agent(gid, side, code, f"カスタムAI（{fname}）", default_deck)


def _selected_opponent(gid, default_deck):
    mode = request.form.get("opponent_type", "rule")
    if mode == "rule":
        # ルールベースAI: 相手デッキも一覧から選べる。
        # opponent_deck_mode 未指定なら従来通りプレイヤーと同じデッキを使う。
        if request.form.get("opponent_deck_mode"):
            deck, deck_name = _selected_deck(
                "opponent_deck_mode", "opponent_deck_id",
                "opponent_user_deck_id", "opponent_deck_file",
                agent_id_field="opponent_deck_agent_id",
            )
            return "rule", None, deck, f"ルールベースAI（{deck_name}）"
        return "rule", None, list(default_deck), "ルールベースAI"

    agent, deck, label = _load_agent_side(
        gid, "opponent", mode, "agent_file", "opponent_agent_id", default_deck)
    return "custom", agent, deck, label


def _selected_player_agent_side(gid):
    """AI vs AI モードのプレイヤー0側エージェントを決める。"""
    ptype = request.form.get("player_type", "rule")
    if ptype == "rule":
        deck, deck_name = _selected_player_deck()
        return "rule", None, deck, f"ルールベースAI（{deck_name}）"
    default_deck = read_sample_deck(SAMPLE_DECKS[0]["id"])
    agent, deck, label = _load_agent_side(
        gid, "player", ptype, "player_agent_file", "player_agent_id", default_deck)
    return "custom", agent, deck, label


def _resolve_registered_agent_deck(aid):
    """登録時、静的に読めなかったAIに初期デッキを問い合わせて meta に保存する。

    サンプルデッキを置いてエージェントに初期デッキを尋ね、サンプルと異なる60枚を
    返したらそれを保存する（deck.csv をそのまま読むタイプはサンプルに一致するので保存しない）。
    """
    try:
        d = Path(tempfile.gettempdir()) / "pokemon_competition_register_probe" / aid
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        (d / "main.py").write_text(read_user_agent_code(aid), encoding="utf-8")
        sample = read_sample_deck(SAMPLE_DECKS[0]["id"])
        deck = _ask_agent_for_deck(d, sample)
        if sorted(deck) == sorted(sample):
            return None  # サンプルそのまま＝deck.csv読み込み型。確定できない。
        return update_user_agent_deck(aid, deck, "AIが決定（登録時取得）")
    except Exception:
        return None


def _load_registered_agent(aid):
    """登録AIを読み込み {agent,deck} を返す（総当たり用）。"""
    code = read_user_agent_code(aid)
    d = Path(tempfile.gettempdir()) / "pokemon_competition_tournament" / aid
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    (d / "main.py").write_text(code, encoding="utf-8")
    _copy_sibling_modules(user_agent_dir(aid), d)  # 同梱の別モジュール(*.py)も複製
    deck = read_user_agent_deck(aid)  # 保存済みデッキ(Kaggle回収等)を優先
    if deck is None:
        deck = parse_decklist_comments(code)
    if deck is None:
        deck = _ask_agent_for_deck(d, read_sample_deck(SAMPLE_DECKS[0]["id"]))
    (d / "deck.csv").write_text("\n".join(str(c) for c in deck) + "\n", encoding="utf-8")
    agent = _import_agent_in_dir(d)
    return {"agent": agent, "deck": deck}


def _headless_pick(side, sel, obs, rng):
    """総当たり用: そのサイドのAIで選択（失敗時はランダムにフォールバック）。"""
    n = len(sel.option)
    if side.get("agent"):
        try:
            return validate_ai_picks(side["agent"](obs), sel.minCount, sel.maxCount, n)
        except Exception:
            pass
    k = rng.randint(sel.minCount, min(sel.maxCount, n))
    return rng.sample(range(n), k)


def _run_headless_match(p0, p1, rng, max_steps=20000):
    """2エージェントを最後まで対戦させ、勝者index(0/1)/引き分け(-1)を返す。"""
    obs, start = battle_start(p0["deck"], p1["deck"])
    if obs is None:
        return None
    set_active_battle(start.battlePtr)
    result = -1
    try:
        for _ in range(max_steps):
            cur = obs.get("current")
            if cur is not None and cur.get("result", -1) != -1:
                result = cur["result"]
                break
            if obs.get("select") is None:
                break
            ob = to_observation_class(obs)
            sel = ob.select
            you = ob.current.yourIndex if ob.current else 0
            if int(sel.context) == int(SelectContext.IS_FIRST):
                picks = [rng.randint(0, len(sel.option) - 1)]
            else:
                picks = _headless_pick(p0 if you == 0 else p1, sel, obs, rng)
            obs = battle_select(picks)
    finally:
        try:
            battle_finish()
        except Exception:
            pass
    return result


def _collect_events(g, obs):
    """obs.logs を日本語イベントに変換して蓄積（画面表示用 events と保存用 log の両方）。"""
    try:
        ob = to_observation_class(obs)
        evs = translate_logs(ob, HUMAN)
        g["events"].extend(evs)
        turn = ob.current.turn if ob.current else None
        for e in evs:
            g["log"].append({"turn": turn, **e})
    except Exception:
        pass


def _frame_board(g):
    """リプレイ用に、現在の盤面スナップショット（両プレイヤー絶対index 0/1）を作る。"""
    ob = to_observation_class(g["obs"])
    st = ob.current
    if st is None:
        return None

    def side(idx):
        p = st.players[idx]
        act = p.active[0] if p.active and p.active[0] else None
        hand = ([card_meta(c.id) for c in p.hand]
                if p.hand is not None else g["hand_cache"].get(idx))
        return {
            "active": _poke_json(act),
            "bench": [_poke_json(b) for b in p.bench],
            "handCount": p.handCount, "deck": p.deckCount,
            "prize": len(p.prize), "discard": len(p.discard),
            "hand": hand,
            "discardList": [card_meta(c.id) for c in p.discard],
            "conditions": [c for c, v in (("どく", p.poisoned), ("やけど", p.burned),
                           ("ねむり", p.asleep), ("まひ", p.paralyzed),
                           ("こんらん", p.confused)) if v],
        }
    return {"turn": st.turn, "you": side(0), "opp": side(1)}


def _capture_frame(g):
    """1手ぶんのスナップショット（盤面＋その間のイベント）をリプレイ用に記録。"""
    try:
        b = _frame_board(g)
        if b is not None:
            g["frames"].append({"board": b, "events": list(g.get("events", [])),
                                "result": g.get("result", -1)})
    except Exception:
        pass


def _save_match_log(g):
    """対戦終了時に、対戦ログを data/logs/ に1ファイルとして保存する。"""
    if g.get("log_saved"):
        return
    g["log_saved"] = True
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        if g.get("mode") == "pvp":
            p0 = g["labels"].get(0, "プレイヤー1")
            p1 = g["labels"].get(1, "プレイヤー2")
        else:
            p0 = g.get("player_label", "あなた")
            p1 = g.get("opponent_label", "相手")
        result = g.get("result", -1)
        winner = {0: p0, 1: p1}.get(result, "引き分け/未確定")
        finished = datetime.now(timezone.utc).isoformat()
        ts = finished.replace(":", "").replace("-", "").split(".")[0]
        data = {
            "gid": g.get("gid"), "mode": g.get("mode"),
            "player0": p0, "player1": p1,
            "result": result, "winner": winner,
            "started_at": g.get("started_at"), "finished_at": finished,
            "log": g.get("log", []),
            "frames": g.get("frames", []),
        }
        path = LOGS_DIR / f"{ts}_{g.get('mode')}_{g.get('gid', '')[:8]}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # 人が読みやすいテキスト版も併せて出力
        lines = [f"対戦ID: {g.get('gid')}", f"モード: {g.get('mode')}",
                 f"プレイヤー1(あなた視点): {p0}", f"プレイヤー2(相手視点): {p1}",
                 f"結果: 勝者={winner}", f"開始: {g.get('started_at')}  終了: {finished}", "-" * 30]
        for e in g.get("log", []):
            who = {"you": p0, "opp": p1}.get(e.get("who"), "")
            t = e.get("turn")
            lines.append(f"[T{t}]" + (f"({who}) " if who else " ") + e.get("text", ""))
        path.with_suffix(".log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def _advance_until_human(g):
    """外部操作する手番(controlled)まで自動進行する。
    呼び出し前に対象対戦を _activate(g) しておくこと。

    controlled = 外部から操作する手番の集合。
      - human / ai モード: {0}（index1 の相手AIは自動消化、index0 で停止）
      - pvp モード: {0, 1}（両方とも人間なので、どちらの手番でも停止）
    ログは最後に戻ってきた obs から1回だけ集める（重複防止）。"""
    controlled = g.get("controlled", {HUMAN})
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
        # 観戦/相手視点用: 手番プレイヤーの手札をキャッシュ（後で各自の視点に流用）。
        if g.get("mode") in ("ai", "pvp") and ob.current:
            try:
                g["hand_cache"][you] = [card_meta(c.id)
                                        for c in ob.current.players[you].hand]
            except Exception:
                pass
        if you in controlled:
            # 先攻/後攻の選択（IS_FIRST）はランダムで自動決定
            if ob.select and int(ob.select.context) == int(SelectContext.IS_FIRST):
                pick = g["rng"].randint(0, len(ob.select.option) - 1)
                obs = battle_select([pick])
                g["obs"] = obs
                steps += 1
                continue
            break  # 外部操作（人間 or ステップ）の入力待ち
        obs = battle_select(_ai_pick(g, ob.select, obs))
        g["obs"] = obs
        steps += 1
    g["obs"] = obs
    # 最終 obs のログ（＝前回選択以降の全出来事）だけを記録
    _collect_events(g, g["obs"])
    _capture_frame(g)  # リプレイ用スナップショット
    if g.get("result", -1) != -1:
        _save_match_log(g)


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


def _state_json(g, viewer=HUMAN):
    """viewer（このレスポンスを見るプレイヤーの index）視点で状態を返す。

    - 自分(viewer)の手札のみ可視。相手手札は非公開（AI観戦モードのみ公開）。
    - 選択肢は viewer の手番のときだけ返す。
    - ポーリング重複防止のため ver（手の通し番号）を含める。
    """
    obs = g["obs"]
    ob = to_observation_class(obs)
    st = ob.current
    mode = g.get("mode", "human")
    out = {"gid": g.get("gid"), "result": g["result"], "running": g["running"],
           "mode": mode, "status": g.get("status", "playing"),
           "ver": g.get("ver", 0), "deckLabel": g.get("deck_label")}
    if mode == "pvp":
        out["youLabel"] = g["labels"].get(viewer, f"プレイヤー{viewer + 1}")
        out["oppLabel"] = g["labels"].get(1 - viewer, f"プレイヤー{2 - viewer}")
    else:
        out["youLabel"] = g.get("player_label", "あなた")
        out["oppLabel"] = g.get("opponent_label")
    out["opponentLabel"] = out["oppLabel"]
    if st is None:
        out["board"] = None
        out["select"] = None
        return out

    cur_idx = st.yourIndex
    you, opp = viewer, 1 - viewer
    # 現在の手番プレイヤーの手札（obs に載っている＝可視）をキャッシュ。
    try:
        if st.players[cur_idx].hand is not None:
            g["hand_cache"][cur_idx] = [card_meta(c.id) for c in st.players[cur_idx].hand]
    except Exception:
        pass

    def side(idx, hand):
        p = st.players[idx]
        act = p.active[0] if p.active and p.active[0] else None
        return {
            "active": _poke_json(act),
            "bench": [_poke_json(b) for b in p.bench],
            "handCount": p.handCount,
            "deck": p.deckCount,
            "prize": len(p.prize),
            "discard": len(p.discard),
            "hand": hand,
            "discardList": [card_meta(c.id) for c in p.discard],  # トラッシュは公開情報
            "conditions": [c for c, v in (("どく", p.poisoned), ("やけど", p.burned),
                           ("ねむり", p.asleep), ("まひ", p.paralyzed),
                           ("こんらん", p.confused)) if v],
        }

    # 自分の手札: 自分の手番なら obs から、そうでなければキャッシュ（最後に見えた手札）。
    you_hand = ([card_meta(c.id) for c in st.players[you].hand]
                if st.players[you].hand is not None else g["hand_cache"].get(you))
    # 相手の手札: AI観戦のみ公開。人 vs AI / pvp は非公開。
    opp_hand = g["hand_cache"].get(opp) if mode == "ai" else None
    out["board"] = {"turn": st.turn, "you": side(you, you_hand), "opp": side(opp, opp_hand)}
    out["yourTurn"] = (cur_idx == viewer)
    # イベント: pvp は viewer 視点で都度翻訳、それ以外は収集済みを使う。
    out["events"] = translate_logs(ob, viewer) if mode == "pvp" else g["events"]

    # viewer の手番のときだけ選択肢を出す
    sel = ob.select
    if sel is not None and cur_idx == viewer and g["result"] == -1:
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
    return render_template("home.html")


@app.route("/decks")
def decks():
    return render_template("decks.html")


@app.route("/agents")
def agents():
    return render_template("agents.html", allow_upload=ALLOW_AGENT_UPLOAD)


@app.route("/play")
def play():
    return render_template("play.html")


@app.route("/setup")
def setup():
    mode = request.args.get("mode")
    mode = mode if mode in ("human", "ai", "pvp") else "human"
    return render_template("setup.html", mode=mode, allow_upload=ALLOW_AGENT_UPLOAD)


@app.route("/builder")
def builder():
    return render_template("builder.html")


@app.route("/battle")
def battle():
    return render_template("battle.html")


@app.route("/join")
def join():
    return render_template("join.html")


@app.route("/replay")
def replay():
    return render_template("replay.html")


@app.route("/tournament")
def tournament():
    return render_template("tournament.html")


@app.route("/evaluate")
def evaluate_page():
    return render_template("evaluate.html")


def _pid_alive(pid):
    """プロセスが生存しているか（POSIX）。"""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


@app.route("/api/evaluate", methods=["POST"])
def api_evaluate_start():
    """個別AI評価を開始: run_tournament.py --challenger を別プロセスで起動し job_id を返す。

    Webワーカー(1本)を塞がないよう、重い対戦は子プロセスに逃がしてポーリングさせる。
    """
    data = request.get_json(silent=True) or {}
    aid = data.get("agent_id")
    metas = list_user_agents()
    if len(metas) < 2:
        return _selection_error("評価には登録AIが2体以上必要です。")
    m = next((x for x in metas if x["id"] == aid), None)
    if m is None:
        return _selection_error("指定のAIが見つかりません。")
    EVAL_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    jobfile = EVAL_JOBS_DIR / f"{job_id}.json"
    workers = max(1, os.cpu_count() or 2)
    cmd = [sys.executable, "tools/run_tournament.py",
           "--challenger", aid,
           "--games-per-pair", str(EVAL_GAMES_PER_PAIR),
           "--workers", str(workers),
           "--out", str(jobfile)]
    proc = subprocess.Popen(
        cmd, cwd=str(APP_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    (EVAL_JOBS_DIR / f"{job_id}.meta").write_text(json.dumps({
        "agent_id": aid, "name": m["name"], "pid": proc.pid,
        "opponents": len(metas) - 1,
        "games_per_pair": EVAL_GAMES_PER_PAIR,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    return jsonify({"job_id": job_id, "name": m["name"],
                    "opponents": len(metas) - 1})


@app.route("/api/evaluate/<job_id>", methods=["GET"])
def api_evaluate_status(job_id):
    """個別AI評価の進捗/結果を返す（done / running / failed）。"""
    if not job_id.isalnum():
        abort(404)
    metafile = EVAL_JOBS_DIR / f"{job_id}.meta"
    jobfile = EVAL_JOBS_DIR / f"{job_id}.json"
    if not metafile.exists():
        abort(404)
    meta = json.loads(metafile.read_text(encoding="utf-8"))
    if jobfile.exists():
        try:
            result = json.loads(jobfile.read_text(encoding="utf-8"))
            return jsonify({"status": "done", "meta": meta, "result": result})
        except Exception:
            return jsonify({"status": "running", "meta": meta})  # 書き込み中
    alive = _pid_alive(meta.get("pid"))
    return jsonify({"status": "running" if alive else "failed", "meta": meta})


@app.route("/api/tournament", methods=["POST"])
def api_tournament():
    """登録AIの総当たり戦（各ペアを home/away の2試合）を実行して結果を返す。"""
    metas = list_user_agents()
    if len(metas) < 2:
        return _selection_error("総当たりには登録AIが2体以上必要です。")
    rng = random.Random()
    loaded = []
    for m in metas:
        try:
            a = _load_registered_agent(m["id"])
            a["name"], a["id"] = m["name"], m["id"]
            loaded.append(a)
        except SelectionError:
            pass
    n = len(loaded)
    stand = {a["id"]: {"name": a["name"], "wins": 0, "losses": 0, "draws": 0, "games": 0}
             for a in loaded}
    matches = []
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            p0, p1 = loaded[i], loaded[j]
            res = _run_headless_match(p0, p1, rng)
            winner = p0["name"] if res == 0 else (p1["name"] if res == 1 else "引き分け")
            matches.append({"p0": p0["name"], "p1": p1["name"], "result": res, "winner": winner})
            for p in (p0, p1):
                stand[p["id"]]["games"] += 1
            if res == 0:
                stand[p0["id"]]["wins"] += 1
                stand[p1["id"]]["losses"] += 1
            elif res == 1:
                stand[p1["id"]]["wins"] += 1
                stand[p0["id"]]["losses"] += 1
            else:
                stand[p0["id"]]["draws"] += 1
                stand[p1["id"]]["draws"] += 1
    standings = sorted(stand.values(), key=lambda s: (-s["wins"], s["losses"]))
    for s in standings:
        s["winRate"] = round(100 * s["wins"] / s["games"]) if s["games"] else 0
    return jsonify({"n": n, "standings": standings, "matches": matches})


@app.route("/api/logs", methods=["GET"])
def api_logs():
    """保存済み対戦ログ（.json）の一覧を返す。"""
    items = []
    if LOGS_DIR.exists():
        for p in sorted(LOGS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                items.append({"file": p.stem, "mode": d.get("mode"),
                              "player0": d.get("player0"), "player1": d.get("player1"),
                              "winner": d.get("winner"), "result": d.get("result"),
                              "finished_at": d.get("finished_at"),
                              "frames": len(d.get("frames") or [])})
            except Exception:
                pass
    return jsonify({"logs": items})


@app.route("/api/logs/<name>", methods=["GET"])
def api_log(name):
    """1対戦ぶんのログ（frames込み）を返す。"""
    if not name.replace("_", "").isalnum():
        abort(404)
    p = LOGS_DIR / (name + ".json")
    if not p.exists():
        abort(404)
    try:
        return jsonify(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        abort(404)


def _agents_with_cards():
    """登録AI一覧に、デッキのカード画像プレビューを付けて返す。"""
    out = []
    for a in list_user_agents():
        cards = _deck_preview(a["deck"]) if a.get("deck") else None
        out.append({**a, "cards": cards})
    return out


@app.route("/api/agents", methods=["GET"])
def api_agents():
    return jsonify({"agents": _agents_with_cards()})


@app.route("/api/agents/preview", methods=["POST"])
def api_preview_agent():
    """登録前に、アップロードAIのデッキ内容を確認するためのプレビュー。"""
    if not ALLOW_AGENT_UPLOAD:
        return _selection_error("このサーバーではAIエージェントの登録は無効化されています。")
    src = request.files.get("agent_file")
    if not src or not src.filename:
        return _selection_error("ファイル(.py / .ipynb)を選択してください。")
    try:
        raw = _decode_upload(src, "AIエージェント")
        info = preview_agent_upload(src.filename, raw)
        info["cards"] = _deck_preview(info["deck"]) if info.get("deck") else None
        return jsonify(info)
    except SelectionError as exc:
        return _selection_error(str(exc))


@app.route("/api/agents", methods=["POST"])
def api_save_agent():
    if not ALLOW_AGENT_UPLOAD:
        return _selection_error("このサーバーではAIエージェントの登録は無効化されています。")
    src = request.files.get("agent_file")
    if not src or not src.filename:
        return _selection_error("登録するファイル(.py / .ipynb)を選択してください。")
    try:
        raw = _decode_upload(src, "AIエージェント")
        name = request.form.get("name") or src.filename
        meta = save_user_agent(name, src.filename, raw)
        # ②静的に読めなかった場合、AIに初期デッキを問い合わせて保存を試みる
        if not meta.get("deck"):
            meta = _resolve_registered_agent_deck(meta["id"]) or meta
        return jsonify({**meta, "cards": _deck_preview(meta["deck"]) if meta.get("deck") else None})
    except SelectionError as exc:
        return _selection_error(str(exc))


@app.route("/api/agents/<agent_id>", methods=["DELETE"])
def api_delete_agent(agent_id):
    try:
        delete_user_agent(agent_id)
        return jsonify({"ok": True})
    except SelectionError as exc:
        return _selection_error(str(exc), status=404)


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
    return jsonify({"sampleDecks": sample_decks, "userDecks": user_decks,
                    "agents": _agents_with_cards(), "allowUpload": ALLOW_AGENT_UPLOAD})


def _deck_preview(counts):
    return [{**card_meta(c["cardId"]), "count": c["count"]} for c in counts]


def _max_attack_damage(detail):
    """ワザの「ダメージ」表記(例 '130', '90+', '120×')の先頭数値の最大値。技無しは0。"""
    best = 0
    for m in detail.get("moves", []):
        s = str(m.get("damage") or "").strip()
        num = ""
        for ch in s:
            if ch.isdigit():
                num += ch
            else:
                break
        if num:
            best = max(best, int(num))
    return best


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
            "maxDamage": _max_attack_damage(detail),  # 攻撃力ソート用
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


@app.route("/api/user_decks/<deck_id>", methods=["PUT"])
def api_update_user_deck(deck_id):
    """既存デッキを上書き更新（編集保存）。"""
    payload = request.json or {}
    try:
        name = str(payload.get("name", "保存デッキ"))
        deck = [int(v) for v in payload.get("cards", [])]
        saved = update_user_deck(deck_id, name, deck)
        counts = deck_card_counts(read_user_deck(saved["id"]))
        return jsonify({**saved, "cards": _deck_preview(counts)})
    except (SelectionError, ValueError, TypeError) as exc:
        return _selection_error(str(exc))


@app.route("/api/user_decks/<deck_id>", methods=["DELETE"])
def api_delete_user_deck(deck_id):
    """保存デッキを削除。"""
    try:
        delete_user_deck(deck_id)
        return jsonify({"ok": True, "id": deck_id})
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


def _request_token():
    return request.args.get("token") or (request.get_json(silent=True) or {}).get("token")


def _viewer_of(g):
    """pvp は token から自分の playerIndex を引く。それ以外は 0（=操作する側）。"""
    if g.get("mode") == "pvp":
        return g["slots"].get(_request_token())
    return HUMAN


@app.route("/api/new", methods=["POST"])
def api_new():
    gid = uuid.uuid4().hex
    g = _new_game_state(gid)
    seed = request.form.get("seed") or None
    g["rng"] = random.Random(seed)
    mm = request.form.get("match_mode")
    match_mode = mm if mm in ("ai", "pvp") else "human"

    # --- オンライン人 vs 人: ホストが部屋を作って相手を待つ（対戦はまだ開始しない） ---
    if match_mode == "pvp":
        try:
            host_deck, host_label = _selected_player_deck()
        except SelectionError as exc:
            return _selection_error(str(exc))
        token = uuid.uuid4().hex
        g["mode"] = "pvp"
        g["controlled"] = {0, 1}
        g["status"] = "waiting"
        g["slots"] = {token: 0}
        g["decks"] = [host_deck, None]
        g["labels"] = {0: f"プレイヤー1（{host_label}）"}
        g["deck_label"] = "オンライン対戦"
        GAMES[gid] = g
        _evict_if_needed()
        return jsonify({"gid": gid, "token": token, "role": "host", "status": "waiting"})

    # --- 人 vs AI / AI vs AI ---
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
    g["controlled"] = {HUMAN}
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


@app.route("/api/join", methods=["POST"])
def api_join():
    """オンライン人 vs 人: ゲストが部屋に参加して対戦開始。"""
    g = GAMES.get(_request_gid())
    if g is None or g.get("mode") != "pvp":
        return jsonify({"error": "対戦が見つかりません（期限切れの可能性）。"}), 404
    if g["status"] != "waiting" or g["decks"][1] is not None:
        return jsonify({"error": "この対戦はすでに開始済み、または満員です。"}), 409
    try:
        guest_deck, guest_label = _selected_player_deck()
    except SelectionError as exc:
        return _selection_error(str(exc))
    token = uuid.uuid4().hex
    g["slots"][token] = 1
    g["decks"][1] = guest_deck
    g["labels"][1] = f"プレイヤー2（{guest_label}）"
    _activate(g)
    obs, start = battle_start(g["decks"][0], g["decks"][1])
    if obs is None:
        return jsonify({"error": f"battle start failed (errorType={start.errorType})"}), 500
    g["ptr"] = start.battlePtr
    g["obs"], g["result"], g["running"], g["events"], g["status"] = obs, -1, True, [], "playing"
    try:
        _advance_until_human(g)
    except SelectionError as exc:
        return _selection_error(str(exc))
    return jsonify({"gid": g["gid"], "token": token, "role": "guest", "status": "playing"})


@app.route("/api/state", methods=["GET"])
def api_state():
    g = _get_game(_request_gid())
    if g is None:
        return jsonify(NO_GAME)
    viewer = _viewer_of(g)
    if g.get("mode") == "pvp" and viewer is None:
        return jsonify({**NO_GAME, "error": "この対戦の参加者ではありません。"})
    if g.get("status") == "waiting":
        return jsonify({"gid": g["gid"], "mode": "pvp", "status": "waiting",
                        "board": None, "select": None, "result": -1, "running": False,
                        "youLabel": g["labels"].get(0)})
    if g["obs"] is None:
        return jsonify(NO_GAME)
    return jsonify(_state_json(g, viewer or HUMAN))


@app.route("/api/select", methods=["POST"])
def api_select():
    g = _get_game(_request_gid())
    if g is None or g["obs"] is None:
        return jsonify(NO_GAME)
    viewer = _viewer_of(g)
    if g.get("mode") == "pvp" and viewer is None:
        return jsonify({"error": "この対戦の参加者ではありません。", **NO_GAME}), 403
    viewer = viewer if viewer is not None else HUMAN
    if g["result"] != -1:
        return jsonify(_state_json(g, viewer))
    ob = to_observation_class(g["obs"])
    # pvp: 自分の手番でなければ拒否
    if g.get("mode") == "pvp" and ob.current and ob.current.yourIndex != viewer:
        return jsonify({"error": "相手の手番です。", **_state_json(g, viewer)}), 409
    picks = (request.get_json(silent=True) or {}).get("picks", [])
    sel = ob.select
    # 妥当性チェック（不正入力ではシミュレータが例外を投げるため事前に弾く）
    n = len(sel.option)
    ok = (isinstance(picks, list) and all(isinstance(p, int) for p in picks)
          and sel.minCount <= len(picks) <= min(sel.maxCount, n)
          and all(0 <= p < n for p in picks) and len(set(picks)) == len(picks))
    if not ok:
        return jsonify({"error": "invalid picks", **_state_json(g, viewer)}), 400
    g["events"] = []
    g["obs"] = battle_select(picks)
    g["ver"] = g.get("ver", 0) + 1
    try:
        _advance_until_human(g)
    except SelectionError as exc:
        return jsonify({"error": str(exc), **_state_json(g, viewer)}), 400
    return jsonify(_state_json(g, viewer))


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
        g["ver"] = g.get("ver", 0) + 1
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
