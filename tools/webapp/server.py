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
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# tools/ を import パスに追加して sim_env を使う
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import (Flask, jsonify, request, render_template,  # noqa: E402
                   send_from_directory, abort, Response)
from urllib.parse import quote  # noqa: E402
from selection import (  # noqa: E402
    DEFAULT_SAMPLE_DECK, SAMPLE_DECKS, SelectionError, deck_card_counts,
    delete_user_agent, extract_agent_code_from_ipynb, list_user_agents,
    list_user_decks, load_custom_agent, parse_decklist_comments, parse_deck_csv_text,
    preview_agent_upload, read_sample_deck, read_user_agent_code,
    read_user_agent_deck, read_user_deck, user_agent_dir,
    sample_deck_options, save_user_agent, save_user_deck,
    update_user_agent_deck, update_user_deck, delete_user_deck,
    validate_ai_picks, validate_deck_for_builder, deck_has_basic,
    deck_is_private, check_deck_password, touch_user_deck_opened,
    list_user_combos, save_user_combo, read_user_combo, delete_user_combo,
    read_favorites, save_favorites,
)
from sim_env import (  # noqa: E402
    to_observation_class, battle_start, battle_select, battle_finish,
    set_active_battle, render_option, card_name, card_meta, card_detail,
    option_card, option_energy_card, translate_logs, card_image_file, CARD_IMAGES_DIR,
    search_begin, search_step, search_end, build_determinization,
    attack_source_card,
    OptionType, AreaType, SelectContext, EnergyType,
)
from replay_recorder import ReplayRecorder  # noqa: E402

HUMAN = 0
app = Flask(__name__, template_folder="templates", static_folder="static")

# 公開時の任意コード実行対策: カスタムAI(.py/.ipynb)アップロードの可否。
# 公開サーバーでは POKECA_ALLOW_AGENT_UPLOAD=0 にして無効化すること（exec によるRCE回避）。
ALLOW_AGENT_UPLOAD = os.environ.get("POKECA_ALLOW_AGENT_UPLOAD", "1") != "0"

# 対戦ログの保存先（対戦ごとに1ファイル）。
LOGS_DIR = Path(__file__).resolve().parents[2] / "data" / "logs"

# バトルロワイヤル: ランキング/殿堂入りの保存先（logs はマウント済みで再ビルド後も残る）。
ROYALE_DIR = LOGS_DIR / "royale"
ROYALE_RANKING_FILE = ROYALE_DIR / "ranking.json"
ROYALE_HOF_FILE = ROYALE_DIR / "hof.json"

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
            # 各イベントの通し番号。クライアントは seq で描画済みを判定し、
            # ポーリング再取得でもログが二重に積まれないようにする（PvP重複対策）。
            "seq": 0,
            # PvP用: 視点(0/1)別のイベント累積と「最新差分」。
            # テキストに「あなた/相手」が埋まるため視点ごとに別々に持つ。
            "pvp_log": [[], []], "pvp_last": ([], []),
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


def _selected_deck(mode_field, sample_id_field, user_id_field, file_field, agent_id_field=None,
                   pw_field=None):
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
        # 非公開デッキは対戦に使う際もパスワードが必要。
        if deck_is_private(deck_id):
            pw = request.form.get(pw_field) if pw_field else ""
            if not check_deck_password(deck_id, pw or ""):
                raise SelectionError("非公開デッキです。正しいパスワードを入力してください。")
        label = next((d["name"] for d in list_user_decks() if d["id"] == deck_id), "保存済みデッキ")
        return read_user_deck(deck_id), label
    # サンプルデッキ: ID で5種から選ぶ（未指定なら先頭）
    deck_id = request.form.get(sample_id_field) or SAMPLE_DECKS[0]["id"]
    label = next((d["name"] for d in SAMPLE_DECKS if d["id"] == deck_id), "サンプルデッキ")
    return read_sample_deck(deck_id), label


def _require_basic_deck(deck):
    """たねポケモンの無いデッキは対戦開始前に弾く（無限マリガン＝進行不能の防止）。"""
    if deck and not deck_has_basic(deck):
        raise SelectionError("選択したデッキにたねポケモンがありません。"
                             "たねポケモンを含むデッキを選んでください。")
    return deck


def _selected_player_deck():
    deck, label = _selected_deck("deck_mode", "deck_id", "user_deck_id", "deck_file",
                                 agent_id_field="deck_agent_id", pw_field="deck_password")
    _require_basic_deck(deck)
    return deck, label


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
    # AI vs AI 観戦: 相手側も「デッキ＋エージェント」の組合せで選ぶ（プレイヤー側と対称）。
    if request.form.get("opponent_select") == "combo":
        return _opponent_combo_side(gid)
    mode = request.form.get("opponent_type", "rule")
    if mode == "rule":
        # ルールベースAI: 相手デッキも一覧から選べる。
        # opponent_deck_mode 未指定なら従来通りプレイヤーと同じデッキを使う。
        if request.form.get("opponent_deck_mode"):
            deck, deck_name = _selected_deck(
                "opponent_deck_mode", "opponent_deck_id",
                "opponent_user_deck_id", "opponent_deck_file",
                agent_id_field="opponent_deck_agent_id", pw_field="opponent_deck_password",
            )
            _require_basic_deck(deck)
            return "rule", None, deck, f"ルールベースAI（{deck_name}）"
        return "rule", None, list(default_deck), "ルールベースAI"

    agent, deck, label = _load_agent_side(
        gid, "opponent", mode, "agent_file", "opponent_agent_id", default_deck)
    _require_basic_deck(deck)
    return "custom", agent, deck, label


def _random_player_deck(rng):
    """ランダム対戦用: 公開かつたね有りの保存済みデッキからランダムに選ぶ。
    無ければサンプルデッキにフォールバック。"""
    cands = []
    for d in list_user_decks():
        if d.get("visibility") == "private":
            continue  # 非公開はパスワードが要るのでランダム対象外
        try:
            deck = read_user_deck(d["id"])
        except SelectionError:
            continue
        if deck_has_basic(deck):
            cands.append((deck, d["name"]))
    if cands:
        deck, name = rng.choice(cands)
        return list(deck), name
    sid = rng.choice(SAMPLE_DECKS)["id"]
    return read_sample_deck(sid), "サンプルデッキ"


def _random_opponent(gid, rng, default_deck):
    """ランダム対戦用: 登録AIからランダムに相手を選ぶ。
    読み込み/デッキ検証に失敗する個体は飛ばし、全滅ならルールベースAIにフォールバック。"""
    agents = list(list_user_agents())
    rng.shuffle(agents)
    for a in agents:
        aid = a["id"]
        try:
            code = read_user_agent_code(aid)
            forced = read_user_agent_deck(aid)
            agent, deck, label = _materialize_agent(
                gid, "opponent", code, f"登録AI（{a['name']}）",
                default_deck, forced_deck=forced, extra_src=user_agent_dir(aid))
            _require_basic_deck(deck)
            return "custom", agent, deck, label
        except Exception:
            continue
    return "rule", None, list(default_deck), "ルールベースAI"


def _selected_player_agent_side(gid):
    """AI vs AI モードのプレイヤー0側エージェントを決める。

    player_select=combo: デッキとエージェントを別々に選び、エージェントのコードを
    選んだデッキ(forced_deck)で動かす（新方式）。
    それ以外: 従来の player_type（rule / registered＝AI同梱デッキ）。
    """
    if request.form.get("player_select") == "combo":
        return _player_combo_side(gid)
    ptype = request.form.get("player_type", "rule")
    if ptype == "rule":
        deck, deck_name = _selected_player_deck()
        return "rule", None, deck, f"ルールベースAI（{deck_name}）"
    default_deck = read_sample_deck(SAMPLE_DECKS[0]["id"])
    agent, deck, label = _load_agent_side(
        gid, "player", ptype, "player_agent_file", "player_agent_id", default_deck)
    return "custom", agent, deck, label


def _player_combo_side(gid):
    """自分側=「デッキ」＋「エージェント」の組合せ。エージェントを選んだデッキで動かす。"""
    aid = (request.form.get("player_agent_id") or "").strip()
    if not aid:
        raise SelectionError("対戦に使うエージェントを選んでください。")
    deck, deck_label = _selected_deck(
        "player_deck_mode", "player_deck_id", "player_user_deck_id", "player_deck_file",
        agent_id_field="player_deck_agent_id", pw_field="player_deck_password")
    _require_basic_deck(deck)
    code = read_user_agent_code(aid)
    name = next((a["name"] for a in list_user_agents() if a["id"] == aid), aid)
    agent, used_deck, _src = _materialize_agent(
        gid, "player", code, f"AI（{name}）", deck,
        forced_deck=deck, extra_src=user_agent_dir(aid))
    return "custom", agent, used_deck, f"{name} × {deck_label}"


def _opponent_combo_side(gid):
    """相手側=「デッキ」＋「エージェント」の組合せ。_player_combo_side と対称。"""
    aid = (request.form.get("opponent_agent_id") or "").strip()
    if not aid:
        raise SelectionError("相手に使うエージェントを選んでください。")
    deck, deck_label = _selected_deck(
        "opponent_deck_mode", "opponent_deck_id", "opponent_user_deck_id", "opponent_deck_file",
        agent_id_field="opponent_deck_agent_id", pw_field="opponent_deck_password")
    _require_basic_deck(deck)
    code = read_user_agent_code(aid)
    name = next((a["name"] for a in list_user_agents() if a["id"] == aid), aid)
    agent, used_deck, _src = _materialize_agent(
        gid, "opponent", code, f"AI（{name}）", deck,
        forced_deck=deck, extra_src=user_agent_dir(aid))
    return "custom", agent, used_deck, f"{name} × {deck_label}"


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


def _load_registered_agent(aid, override_deck=None):
    """登録AIを読み込み {agent,deck} を返す（総当たり用）。

    override_deck（60枚）を渡すと、AI同梱デッキの代わりにそのデッキで動かす
    （個別AI評価の combo: エージェント×任意デッキ）。deck.csv を読むAIにも効くよう
    import 前に deck.csv へ書き出す。
    """
    code = read_user_agent_code(aid)
    d = Path(tempfile.gettempdir()) / "pokemon_competition_tournament" / aid
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    (d / "main.py").write_text(code, encoding="utf-8")
    _copy_sibling_modules(user_agent_dir(aid), d)  # 同梱の別モジュール(*.py)も複製
    if override_deck and len(override_deck) == 60:
        deck = list(override_deck)
    else:
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
        evs = translate_logs(ob, HUMAN, st=g.setdefault("_drawst", {}))
        g["events"].extend(evs)
        turn = ob.current.turn if ob.current else None
        for e in evs:
            g["log"].append({"turn": turn, **e})
    except Exception:
        pass


def _board_from_obs(obs_dict, hand_cache=None):
    """observation(dict) から盤面スナップショット（両プレイヤー絶対index 0/1）を作る。

    リプレイ生成(replay.json→表示)と対戦中の both で使う共通関数。"""
    ob = to_observation_class(obs_dict)
    st = ob.current
    if st is None:
        return None
    hand_cache = hand_cache or {}

    def side(idx):
        p = st.players[idx]
        act = p.active[0] if p.active and p.active[0] else None
        hand = ([card_meta(c.id) for c in p.hand]
                if p.hand is not None else hand_cache.get(idx))
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


def _frame_board(g):
    """リプレイ用に、現在の盤面スナップショット（両プレイヤー絶対index 0/1）を作る。"""
    return _board_from_obs(g["obs"], g.get("hand_cache"))


def _record_select(g, picks):
    """battle_select を行い、その手を replay レコーダへ記録する（ゲーム進行の各 select で使う）。

    記録するのは select 直前の observation（能動側視点）＋ action（picks）。"""
    rec = g.get("recorder")
    if rec is not None:
        try:
            ob = to_observation_class(g["obs"])
            you = ob.current.yourIndex if ob.current else HUMAN
            rec.record(g["obs"], you, picks)
        except Exception:
            pass
    return battle_select(picks)


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
    """対戦終了時に、対戦ログを Kaggle の replay.json 互換形式で data/logs/ に保存する。

    observation/steps/action/reward/status の構造・中身を Kaggle に揃える。
    画面のリプレイ表示は保存した replay.json から導出する（_replay_to_view）。"""
    if g.get("log_saved"):
        return
    g["log_saved"] = True
    rec = g.get("recorder")
    if rec is None:
        return
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
        replay = rec.finalize(result)
        # ローカル固有メタは info に補足（info は自由 dict なので Kaggle 互換を壊さない）。
        replay["info"]["TeamNames"] = [p0, p1]
        replay["info"]["Mode"] = g.get("mode")
        replay["info"]["StartedAt"] = g.get("started_at")
        replay["info"]["FinishedAt"] = finished
        path = LOGS_DIR / f"{ts}_{g.get('mode')}_{g.get('gid', '')[:8]}.json"
        path.write_text(json.dumps(replay, ensure_ascii=False, indent=2), encoding="utf-8")
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
    g["ai_steps"] = []          # AIの1手ごとの盤面スナップショット（ライブ再生用）
    _human_mode = g.get("mode") != "ai"
    st = g.setdefault("_drawst", {})  # ドロー種別判定の状態（delta跨ぎ）
    g.setdefault("seq", 0)            # 古い state 救済（通常は _new_game_state で初期化済み）
    g.setdefault("pvp_log", [[], []])
    g.setdefault("pvp_last", ([], []))

    def _emit(record_step):
        """g["obs"] の差分ログを「1回だけ」翻訳し、保存/表示ログ(events/log)へ蓄積する。

        obs.logs は select 毎の差分なので、この関数で各 delta を1回ずつ処理すれば
        ライブ(ai_steps)も保存(log)も同じイベント列・同じドロー種別分類になる。
        record_step=True のときは盤面スナップショットも ai_steps に積む（AIの1手の再生用）。
        以前は AIの各手を _snap で ai_steps にしか積まず、保存用 log は最終 obs だけを
        別途 _collect_events で処理していたため、途中ログ欠落＋最終 delta の二重変換による
        分類ズレが起きていた。それを解消する。"""
        try:
            ob_ = to_observation_class(g["obs"])
        except Exception:
            return []
        mode = g.get("mode")
        evs = translate_logs(ob_, HUMAN, st=st)
        # PvP は相手(viewer=1)視点でも翻訳する（「あなた/相手」がテキストに埋まるため）。
        # ドロー種別判定の状態は視点ごとに別 dict で引き継ぐ。
        evs1 = (translate_logs(ob_, 1, st=g.setdefault("_drawst1", {}))
                if mode == "pvp" else None)
        # 各イベントに通し番号(seq)を付与。両視点で同じ番号を振る（イベント数は視点に依らず同じ）。
        for k, e in enumerate(evs):
            e["seq"] = g["seq"]
            if evs1 is not None and k < len(evs1):
                evs1[k]["seq"] = g["seq"]
            g["seq"] += 1
        if evs1 is not None and len(evs1) > len(evs):   # 念のための長さズレ救済
            for e in evs1[len(evs):]:
                e["seq"] = g["seq"]
                g["seq"] += 1
        if evs:
            g["events"].extend(evs)
            turn = ob_.current.turn if ob_.current else None
            for e in evs:
                g["log"].append({"turn": turn, **e})
        if mode == "pvp":
            g["pvp_log"][0].extend(evs)
            g["pvp_log"][1].extend(evs1 if evs1 is not None else evs)
            g["pvp_last"] = (evs, evs1 if evs1 is not None else evs)
        # 手札キャッシュの維持: 行動後の obs で見えている手札をライブで上書きする。
        # （手番交代直前に出したカード/付けたエネが手札に残って見えるズレを抑える）
        try:
            if ob_.current:
                for _i in range(len(ob_.current.players)):
                    _ph = ob_.current.players[_i].hand
                    if _ph is not None:
                        g["hand_cache"][_i] = [card_meta(c.id) for c in _ph]
        except Exception:
            pass
        if record_step:
            try:
                board = _frame_board(g)
            except Exception:
                board = None
            if board is not None:
                if _human_mode and isinstance(board.get("opp"), dict):
                    board["opp"]["hand"] = None   # 相手手札は伏せる
                g["ai_steps"].append({"board": board, "events": evs})
        return evs

    # 入口（人間の操作結果 or 初期 obs）の差分を1回だけ反映する。
    _emit(record_step=False)

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
        # 先攻/後攻(IS_FIRST=コイン)は、人間・AIどちらが選ぶ側でも常にランダムで自動決定する。
        # rng は seed 無し（/api/new）なので毎ゲーム無作為＝先攻/後攻が毎回ランダムになる。
        # （以前は人間の手番のときだけ無作為化しており、相手が先攻を固定選択するエージェントだと
        #   人間が常に後攻になり得たため、どちらの選択側でも無作為化するよう変更。）
        if ob.select and int(ob.select.context) == int(SelectContext.IS_FIRST):
            pick = g["rng"].randint(0, len(ob.select.option) - 1)
            obs = _record_select(g, [pick])
            g["obs"] = obs
            _emit(record_step=False)  # コイン等の差分も保存ログへ反映
            steps += 1
            continue
        if you in controlled:
            break  # 外部操作（人間 or ステップ）の入力待ち
        obs = _record_select(g, _ai_pick(g, ob.select, obs))
        g["obs"] = obs
        _emit(record_step=True)  # AIの1手: ai_steps と 保存ログ の両方へ
        steps += 1
    g["obs"] = obs
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


def _state_json(g, viewer=HUMAN, reveal_all=False):
    """viewer（このレスポンスを見るプレイヤーの index）視点で状態を返す。

    - 自分(viewer)の手札のみ可視。相手手札は非公開（AI観戦モードのみ公開）。
    - 選択肢は viewer の手番のときだけ返す。
    - ポーリング重複防止のため ver（手の通し番号）を含める。
    - reveal_all=True（サンドボックス）: 両者の手札を公開し、現在の手番プレイヤーの
      選択肢を（viewer と一致しなくても）返す。out["mover"] に現手番の index を入れる。
      盤面の向きは viewer 固定（＝プレイヤー0を下段）にする。
    """
    obs = g["obs"]
    ob = to_observation_class(obs)
    st = ob.current
    mode = g.get("mode", "human")
    out = {"gid": g.get("gid"), "result": g["result"], "running": g["running"],
           "mode": mode, "status": g.get("status", "playing"),
           "ver": g.get("ver", 0), "deckLabel": g.get("deck_label"),
           "aiSteps": g.get("ai_steps", [])}
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

    def _hand_of(idx):
        """idx の手札: 手番中なら obs から、そうでなければキャッシュ（最後に見えた手札）。"""
        return ([card_meta(c.id) for c in st.players[idx].hand]
                if st.players[idx].hand is not None else g["hand_cache"].get(idx))

    # 自分の手札: 自分の手番なら obs から、そうでなければキャッシュ（最後に見えた手札）。
    you_hand = _hand_of(you)
    # 相手の手札: サンドボックスと AI観戦のみ公開。人 vs AI / pvp は非公開。
    if reveal_all:
        opp_hand = _hand_of(opp)
    elif mode == "ai":
        opp_hand = g["hand_cache"].get(opp)
    else:
        opp_hand = None
    # 場に出ているスタジアム（State.stadium は 0/1 枚。両プレイヤー共有の1枠）。
    stadium = None
    try:
        if getattr(st, "stadium", None):
            stadium = card_meta(st.stadium[0].id)
    except Exception:
        stadium = None
    out["board"] = {"turn": st.turn, "you": side(you, you_hand), "opp": side(opp, opp_hand),
                    "stadium": stadium}
    out["yourTurn"] = (cur_idx == viewer)
    out["mover"] = cur_idx
    # イベント: pvp は emit 時に蓄積した viewer 視点の最新差分を使う（都度翻訳しない）。
    # 各イベントに seq が付いており、クライアントは描画済み seq を除外して二重表示を防ぐ。
    if mode == "pvp":
        out["events"] = g.get("pvp_last", ([], []))[viewer]   # 最新差分（アニメ用）
        out["log"] = list(g.get("pvp_log", [[], []])[viewer])  # 累積ログ（ログ欄を冪等に再構築）
        out["seq"] = len(g.get("pvp_log", [[], []])[viewer])
    else:
        out["events"] = g["events"]
        out["seq"] = g.get("seq", 0)
    if reveal_all:  # サンドボックス: テスト用に累積ログ全体を返す
        out["log"] = [{"turn": e.get("turn"), "who": e.get("who"), "text": e.get("text")}
                      for e in g.get("log", [])]

    # viewer の手番のときだけ選択肢を出す（reveal_all では現手番のぶんを常に出す）
    sel = ob.select
    if sel is not None and (cur_idx == viewer or reveal_all) and g["result"] == -1:
        opts = []
        for i, o in enumerate(sel.option):
            try:
                otype = OptionType(o.type).name
            except ValueError:
                otype = str(o.type)
            pi = getattr(o, "playerIndex", None)
            # 対象がどちら側の盤面か（盤面の向きは viewer 基準なので viewer で you/opp を判定）。
            # これが無いとフロントが相手ベンチ対象を自分側に誤って割り当てる。
            side = "opp" if (pi is not None and pi != viewer) else "you"
            card = option_card(o, st, sel)
            # こうまんしれい等「他ポケモンのワザを使う」ATTACK選択肢はカード参照が無い。
            # attackId から元ポケモンを逆引きしてカードを載せ、フロントでカード表示できるようにする。
            aid = getattr(o, "attackId", None)
            if card is None and otype == "ATTACK" and aid is not None:
                scid = attack_source_card(aid)
                if scid:
                    card = card_meta(scid)
            # ENERGY / ENERGY_CARD の area/index は“エネが付いているポケモン”を指すため、
            # option_card だとポケモンのカードが返ってしまう（「トラッシュするエネルギーを
            # 選んでください」でポケモン画像が出るバグ）。実際のエネカードに差し替える。
            if otype in ("ENERGY", "ENERGY_CARD"):
                ecard = option_energy_card(o, st)
                if ecard is not None:
                    card = ecard
            opts.append({
                "index": i, "label": render_option(o, st, sel),
                "card": card,
                "optionType": otype,
                "area": getattr(o, "area", None), "srcIndex": getattr(o, "index", None),
                "playerIndex": pi, "side": side,
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


@app.route("/guide")
def guide():
    return render_template("guide.html")


@app.route("/rules")
def rules():
    return render_template("rules.html")


@app.route("/royale")
def royale():
    return render_template("royale.html")


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


@app.route("/sandbox")
def sandbox_page():
    return render_template("sandbox.html")


def _read_json(path):
    """JSONファイルを読み、欠落/壊れていれば空dictを返す。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


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

    # combo評価: 評価対象のデッキを差し替える（任意。未指定なら従来の同梱デッキ）。
    override_deck = None
    deck_label = ""
    deck_mode = (data.get("deck_mode") or "").strip()
    deck_id = (data.get("deck_id") or "").strip()
    if deck_mode and deck_id:
        try:
            if deck_mode == "user":
                override_deck = read_user_deck(deck_id)
                deck_label = next((d["name"] for d in list_user_decks()
                                   if d["id"] == deck_id), "保存済みデッキ")
            elif deck_mode == "agent":
                override_deck = _agent_deck_list(deck_id)
                deck_label = next((a["name"] for a in metas
                                   if a["id"] == deck_id), "登録AIのデッキ")
            else:  # sample
                override_deck = read_sample_deck(deck_id)
                deck_label = "サンプルデッキ"
        except SelectionError as exc:
            return _selection_error(str(exc))
        except Exception:
            return _selection_error("評価用デッキの読み込みに失敗しました。")
        if not override_deck or len(override_deck) != 60 or not deck_has_basic(override_deck):
            return _selection_error("選んだデッキが不正です（60枚・たねポケモン必須）。")

    # 各相手との対戦数（先攻/後攻で各 gpp 試合）。既定 EVAL_GAMES_PER_PAIR。1〜50に制限。
    try:
        gpp = int(data.get("games_per_pair"))
    except (TypeError, ValueError):
        gpp = EVAL_GAMES_PER_PAIR
    gpp = max(1, min(50, gpp))

    EVAL_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    jobfile = EVAL_JOBS_DIR / f"{job_id}.json"
    workers = max(1, os.cpu_count() or 2)
    # 各試合の全行動ログ（対戦と同じ形式）を data/logs/eval/<job_id>/ に保存する。
    eval_log_dir = LOGS_DIR / "eval" / job_id
    cmd = [sys.executable, "tools/run_tournament.py",
           "--challenger", aid,
           "--games-per-pair", str(gpp),
           "--workers", str(workers),
           "--progress-file", str(EVAL_JOBS_DIR / f"{job_id}.progress"),
           "--log-dir", str(eval_log_dir),
           "--out", str(jobfile)]
    if override_deck:
        deck_file = EVAL_JOBS_DIR / f"{job_id}.deck.csv"
        deck_file.write_text("\n".join(str(c) for c in override_deck) + "\n", encoding="utf-8")
        cmd += ["--challenger-deck-file", str(deck_file)]
    proc = subprocess.Popen(
        cmd, cwd=str(APP_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    (EVAL_JOBS_DIR / f"{job_id}.meta").write_text(json.dumps({
        "agent_id": aid, "name": m["name"], "pid": proc.pid,
        "opponents": len(metas) - 1,
        "games_per_pair": gpp,
        "log_dir": str(eval_log_dir),
        "deck_label": deck_label,  # combo評価で差し替えたデッキ名（同梱デッキなら空）
        "started_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    return jsonify({"job_id": job_id, "name": m["name"],
                    "opponents": len(metas) - 1, "deck_label": deck_label})


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
            pass  # 書き込み中 → running 扱い
    prog = None
    progfile = EVAL_JOBS_DIR / f"{job_id}.progress"
    if progfile.exists():
        try:
            prog = json.loads(progfile.read_text(encoding="utf-8"))
        except Exception:
            prog = None
    if not jobfile.exists() and not _pid_alive(meta.get("pid")):
        return jsonify({"status": "failed", "meta": meta, "progress": prog})
    return jsonify({"status": "running", "meta": meta, "progress": prog})


@app.route("/api/evaluate/<job_id>/download", methods=["GET"])
def api_evaluate_download(job_id):
    """個別AI評価の結果一式（サマリ・相手別CSV・各試合ログ）をZIPで返す。

    ZIP はメモリ上(BytesIO)で組み立てて送るだけで、サーバのディスクには残さない
    （= ダウンロード後にローカルへ保存されず、削除する手間も無い）。
    """
    if not job_id.isalnum():
        abort(404)
    metafile = EVAL_JOBS_DIR / f"{job_id}.meta"
    jobfile = EVAL_JOBS_DIR / f"{job_id}.json"
    if not metafile.exists():
        abort(404)
    if not jobfile.exists():
        return jsonify({"error": "評価がまだ完了していません。"}), 409
    try:
        meta = json.loads(metafile.read_text(encoding="utf-8"))
        result = json.loads(jobfile.read_text(encoding="utf-8"))
    except Exception:
        return jsonify({"error": "評価結果がまだ読み込めません。"}), 409

    summary = result.get("challenger", {})
    vs = result.get("vs", [])

    # 相手別成績CSV（Excelでの文字化け回避に BOM 付き UTF-8、改行は CRLF）
    def _cell(v):
        return '"' + ("" if v is None else str(v)).replace('"', '""') + '"'
    rows = ["相手AI,勝,敗,分,試合,勝率(%)"]
    for s in vs:
        rows.append(",".join(_cell(s.get(k)) for k in
                             ("name", "wins", "losses", "draws", "games", "winRate")))
    csv_text = "﻿" + "\r\n".join(rows) + "\r\n"

    txt = (
        "個別AI評価 結果\n================\n"
        f"評価AI: {meta.get('name')} (id={meta.get('agent_id')})\n"
        f"対フィールド勝率: {summary.get('winRate')}%\n"
        f"成績: {summary.get('wins')}勝 {summary.get('losses')}敗 "
        f"{summary.get('draws')}分 / {summary.get('games')}試合\n"
        f"対戦相手: {len(vs)}体・{result.get('games_per_pair')}試合/相手\n"
        f"所要時間: {result.get('elapsed_s')}秒\n"
        f"開始時刻: {meta.get('started_at')}\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("summary.txt", txt)
        zf.writestr("summary.json", json.dumps(
            {"meta": meta, "result": result}, ensure_ascii=False, indent=2))
        zf.writestr("per_opponent.csv", csv_text)
        # 各試合の行動ログ（あれば同梱）
        log_dir = Path(meta.get("log_dir") or (LOGS_DIR / "eval" / job_id))
        if log_dir.exists():
            for p in sorted(log_dir.glob("*.json")):
                zf.write(p, arcname=f"match_logs/{p.name}")

    data = buf.getvalue()
    # Content-Disposition は latin-1 のみ。ファイル名は ASCII 英数字に限定する
    # （日本語等は isalnum() が True になりヘッダのエンコードを壊すため除外）。
    safe = "".join(ch if (ch.isascii() and (ch.isalnum() or ch in "-_.")) else "_"
                   for ch in str(meta.get("name") or "agent"))[:40].strip("_") or "agent"
    fname = f"eval_{safe}_{job_id}.zip"
    resp = Response(data, mimetype="application/zip")
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    resp.headers["Content-Length"] = str(len(data))
    return resp


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
    stand = {a["id"]: {"id": a["id"], "name": a["name"], "wins": 0,
                       "losses": 0, "draws": 0, "games": 0}
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
    # バトルロワイヤルのラダー元データも更新（id付き standings を永続化）。
    try:
        ROYALE_DIR.mkdir(parents=True, exist_ok=True)
        ROYALE_RANKING_FILE.write_text(
            json.dumps({"n": n, "matches": len(matches), "standings": standings},
                       ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return jsonify({"n": n, "standings": standings, "matches": matches})


def _replay_result(replay):
    """rewards から 勝者index(0/1)/引き分け(-1) を求める。"""
    rewards = replay.get("rewards") or [0, 0]
    if rewards == [1, -1]:
        return 0
    if rewards == [-1, 1]:
        return 1
    return -1


def _replay_to_view(replay):
    """保存した replay.json を、リプレイ画面が期待する {frames, log, ...} 形に変換する。

    各ステップの能動側 observation.current から盤面を、logs からイベントを再構成する。"""
    info = replay.get("info") or {}
    names = info.get("TeamNames") or ["プレイヤー1", "プレイヤー2"]
    result = _replay_result(replay)
    winner = {0: names[0], 1: names[1]}.get(result, "引き分け/未確定")
    frames, log, st_draw = [], [], {}
    for step in replay.get("steps", []):
        active = next((a for a in step if a.get("status") == "ACTIVE"), None)
        if active is None:
            continue
        obs = active.get("observation") or {}
        try:
            board = _board_from_obs(obs)
        except Exception:
            board = None
        if board is None:
            continue
        try:
            evs = translate_logs(to_observation_class(obs), HUMAN, st=st_draw)
        except Exception:
            evs = []
        turn = board.get("turn")
        for e in evs:
            log.append({"turn": turn, **e})
        frames.append({"board": board, "events": evs, "result": result})
    return {"gid": replay.get("id"), "mode": info.get("Mode"),
            "player0": names[0], "player1": names[1],
            "result": result, "winner": winner,
            "started_at": info.get("StartedAt"), "finished_at": info.get("FinishedAt"),
            "log": log, "frames": frames}


@app.route("/api/logs", methods=["GET"])
def api_logs():
    """保存済み対戦ログ（replay.json 互換）の一覧を返す。"""
    items = []
    if LOGS_DIR.exists():
        for p in sorted(LOGS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if "steps" not in d and "frames" in d:
                    # 旧形式ログ（後方互換）
                    items.append({"file": p.stem, "mode": d.get("mode"),
                                  "player0": d.get("player0"), "player1": d.get("player1"),
                                  "winner": d.get("winner"), "result": d.get("result"),
                                  "finished_at": d.get("finished_at"),
                                  "frames": len(d.get("frames") or [])})
                    continue
                info = d.get("info") or {}
                names = info.get("TeamNames") or ["プレイヤー1", "プレイヤー2"]
                result = _replay_result(d)
                items.append({"file": p.stem, "mode": info.get("Mode"),
                              "player0": names[0], "player1": names[1],
                              "winner": {0: names[0], 1: names[1]}.get(result, "引き分け/未確定"),
                              "result": result, "finished_at": info.get("FinishedAt"),
                              "frames": len(d.get("steps") or [])})
            except Exception:
                pass
    return jsonify({"logs": items})


@app.route("/api/logs/<name>", methods=["GET"])
def api_log(name):
    """1対戦ぶんのログを、リプレイ画面用に変換して返す（保存は replay.json 互換）。"""
    if not name.replace("_", "").isalnum():
        abort(404)
    p = LOGS_DIR / (name + ".json")
    if not p.exists():
        abort(404)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        abort(404)
    if "steps" not in d and "frames" in d:
        return jsonify(d)  # 旧形式はそのまま
    return jsonify(_replay_to_view(d))


def _is_safe_log_name(name):
    return bool(name) and name.replace("_", "").replace("-", "").isalnum()


@app.route("/api/logs/<name>/download", methods=["GET"])
def api_log_download(name):
    """1対戦ぶんのログを生の replay.json ファイルとしてダウンロードさせる。"""
    if not _is_safe_log_name(name):
        abort(404)
    p = LOGS_DIR / (name + ".json")
    if not p.exists():
        abort(404)
    return send_from_directory(LOGS_DIR, name + ".json", as_attachment=True,
                               download_name=name + ".json",
                               mimetype="application/json")


@app.route("/api/logs/download", methods=["GET"])
def api_logs_download_zip():
    """複数の対戦ログをまとめて1つの ZIP でダウンロードさせる。
    クエリ files=名前1,名前2,... で対象を指定。進捗バー用に Content-Length を付与する。"""
    raw = (request.args.get("files") or "").strip()
    names = [n for n in (s.strip() for s in raw.split(",")) if n]
    names = [n for n in names if _is_safe_log_name(n)]
    if not names:
        return jsonify({"error": "ダウンロードするログを指定してください。"}), 400
    buf = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n in names:
            p = LOGS_DIR / (n + ".json")
            if p.exists():
                zf.write(p, arcname=n + ".json")
                added += 1
    if not added:
        return jsonify({"error": "対象のログが見つかりませんでした。"}), 404
    data = buf.getvalue()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname = f"battle_logs_{ts}.zip"
    resp = Response(data, mimetype="application/zip")
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    resp.headers["Content-Length"] = str(len(data))
    return resp


_AGENTS_CARDS_CACHE = {"sig": None, "data": None}


def _agents_with_cards():
    """登録AI一覧に、デッキのカード画像プレビューを付けて返す。

    プレビュー生成(カードごとの card_meta)は登録AIが数十体あると重く、毎回作ると
    /api/config が数秒かかる。登録AIの構成(id＋デッキ)が変わらない限りキャッシュを返し、
    変化したら自動で作り直す（保存/削除/外部同期いずれも検知できる）。"""
    metas = list_user_agents()
    sig = tuple((a["id"], tuple(a.get("deck") or [])) for a in metas)
    if _AGENTS_CARDS_CACHE["sig"] == sig and _AGENTS_CARDS_CACHE["data"] is not None:
        return _AGENTS_CARDS_CACHE["data"]
    out = [{**a, "cards": _deck_preview(a["deck"]) if a.get("deck") else None} for a in metas]
    _AGENTS_CARDS_CACHE["sig"] = sig
    _AGENTS_CARDS_CACHE["data"] = out
    return out


@app.route("/api/agents", methods=["GET"])
def api_agents():
    return jsonify({"agents": _agents_with_cards()})


@app.route("/api/agents/<agent_id>/download", methods=["GET"])
def api_download_agent(agent_id):
    """登録AIのコード(main.py)をダウンロードさせる。

    .ipynb で登録されたものも、保存時に抽出済みの Python コード(main.py)を返す。"""
    try:
        code = read_user_agent_code(agent_id)
    except (SelectionError, OSError):
        abort(404)
    meta = next((a for a in list_user_agents() if a["id"] == agent_id), None)
    base = (meta and (meta.get("name") or meta.get("filename"))) or agent_id
    base = base.rsplit(".", 1)[0]  # 拡張子を除く
    fname = quote(f"{base}.py")    # 日本語名でも壊れないよう RFC5987 でエンコード
    return Response(code, mimetype="text/x-python",
                    headers={"Content-Disposition":
                             f"attachment; filename=\"agent.py\"; filename*=UTF-8''{fname}"})


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
        # ファイル内にデッキが無い場合のデッキ決定:
        #   ①ユーザーが既存デッキ(user_deck_id)を指定 → それを結びつける
        #   ②指定なし → 従来どおりAIに初期デッキを問い合わせて保存を試みる
        if not meta.get("deck"):
            deck_id = (request.form.get("user_deck_id") or "").strip()
            if deck_id:
                try:
                    deck = read_user_deck(deck_id)
                except SelectionError as exc:
                    return _selection_error(str(exc))
                dname = next((d["name"] for d in list_user_decks()
                              if d["id"] == deck_id), "選択デッキ")
                meta = update_user_agent_deck(meta["id"], deck, f"選択デッキ: {dname}")
            else:
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


def _user_decks_payload():
    """保存済みデッキ一覧（カードプレビュー込み）。登録AIプレビューより軽い。"""
    user_decks = []
    for opt in list_user_decks():
        private = opt.get("visibility") == "private"
        try:
            deck = read_user_deck(opt["id"])
            has_basic = deck_has_basic(deck)
            cards = None if private else _deck_preview(deck_card_counts(deck))
        except SelectionError:
            cards = None
            has_basic = True  # 読めない場合は従来通り（弾かない）
        # 非公開デッキは名前だけ見せ、中身(cards)は隠す（解錠でのみ取得）。
        user_decks.append({"id": opt["id"], "name": opt["name"],
                           "visibility": opt.get("visibility", "public"),
                           "private": private, "cards": cards, "hasBasic": has_basic})
    return user_decks


@app.route("/api/decks", methods=["GET"])
def api_decks():
    """デッキ選択用の軽量エンドポイント。

    /api/config は登録AI(数十体)のデッキプレビュー生成が重く数秒かかることがあり、
    その間デッキ選択プルダウンが空になる。デッキ選択はAI一覧に依存しないので、
    保存済みデッキだけを返す本エンドポイントで先に埋める。"""
    # サンプルデッキはUIから非表示（内部フォールバックでのみ使用するため返さない）。
    return jsonify({"sampleDecks": [], "userDecks": _user_decks_payload()})


@app.route("/api/config", methods=["GET"])
def api_config():
    """保存済みデッキ・登録AIデッキ一覧（中身プレビュー込み）を返す。"""
    return jsonify({"sampleDecks": [], "userDecks": _user_decks_payload(),
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


_ALL_CARDS_CACHE = None


@app.route("/api/cards", methods=["GET"])
def api_cards():
    """デッキ作成画面用: PDF 39ページまでの使用可能カード(ID 1..1267)。

    カードデータは静的（起動中に変わらない）ので初回構築結果をプロセス内にキャッシュする。
    毎回 1267枚ぶん card_detail を生成すると ~60秒かかり画面が事実上使えないため必須。"""
    global _ALL_CARDS_CACHE
    if _ALL_CARDS_CACHE is not None:
        return jsonify({"cards": _ALL_CARDS_CACHE})
    cards = []
    for cid in range(1, 1268):
        meta = card_meta(cid)
        detail = card_detail(cid)
        # 効果テキスト検索用: 全ワザ/特性の名前・効果＋トレーナーズ説明をまとめる。
        parts = []
        for m in (detail.get("moves") or []):
            if m.get("name"):
                parts.append(m["name"])
            if m.get("effect"):
                parts.append(m["effect"])
        if detail.get("text"):
            parts.append(detail["text"])
        cards.append({
            **meta,
            "stage": detail.get("stage", ""),
            "typeJP": detail.get("typeJP", ""),
            "category": detail.get("category", ""),
            "rule": detail.get("rule", ""),
            "evolvesFrom": detail.get("evolvesFrom", ""),
            "maxDamage": _max_attack_damage(detail),  # 攻撃力ソート/レンジ検索用
            "hasAbility": any(str(m.get("name") or "").startswith("[特性]")
                              for m in (detail.get("moves") or [])),  # 特性持ちフィルタ用
            "effectText": " ".join(parts),            # 効果テキスト検索用
        })
    _ALL_CARDS_CACHE = cards
    return jsonify({"cards": cards})


@app.route("/api/decks/parse_csv", methods=["POST"])
def api_parse_deck_csv():
    """デッキCSVをアップロードして60枚のカードID列に変換する（デッキ作成画面の読み込み用）。

    ID1枚/行・カンマ区切りどちらも可（parse_deck_csv_text が吸収）。検証も同時に行う。"""
    try:
        text = _decode_upload(request.files.get("deck_file"), "デッキCSV")
        deck = parse_deck_csv_text(text)
        validate_deck_for_builder(deck)
        return jsonify({"cards": deck})
    except SelectionError as exc:
        return _selection_error(str(exc))


@app.route("/api/user_decks", methods=["POST"])
def api_save_user_deck():
    payload = request.json or {}
    try:
        name = str(payload.get("name", "保存デッキ"))
        deck = [int(v) for v in payload.get("cards", [])]
        visibility = payload.get("visibility", "public")
        password = str(payload.get("password", ""))
        if visibility == "private" and not password:
            raise SelectionError("非公開デッキにはパスワードを設定してください。")
        if not deck_has_basic(deck):
            raise SelectionError("たねポケモンを最低1枚入れてください。")
        saved = save_user_deck(name, deck, visibility, password)
        return jsonify(saved)
    except (SelectionError, ValueError, TypeError) as exc:
        return _selection_error(str(exc))


@app.route("/api/decks/<deck_id>/unlock", methods=["POST"])
def api_unlock_deck(deck_id):
    """非公開デッキをパスワードで解錠し、中身（カードプレビュー）を返す。"""
    pw = (request.get_json(silent=True) or {}).get("password", "")
    try:
        if not check_deck_password(deck_id, pw):
            return jsonify({"error": "パスワードが違います。"}), 403
        return jsonify({"cards": _deck_preview(deck_card_counts(read_user_deck(deck_id)))})
    except SelectionError as exc:
        return _selection_error(str(exc), status=404)


@app.route("/api/user_decks/<deck_id>", methods=["GET"])
def api_user_deck(deck_id):
    try:
        # 非公開デッキは中身の取得（編集含む）にパスワードが必要。
        if deck_is_private(deck_id):
            pw = request.args.get("password", "")
            if not check_deck_password(deck_id, pw):
                return jsonify({"error": "このデッキは非公開です。パスワードが必要です。",
                                "private": True}), 403
        deck = read_user_deck(deck_id)
        # ?touch=1 はビルダーでデッキを「開いた」印。一覧の並び順（直近で開いた順）に使う。
        if request.args.get("touch"):
            touch_user_deck_opened(deck_id)
        opt = next((d for d in list_user_decks() if d["id"] == deck_id), {"id": deck_id, "name": deck_id})
        return jsonify({"id": deck_id, "name": opt["name"],
                        "visibility": opt.get("visibility", "public"),
                        "cards": _deck_preview(deck_card_counts(deck))})
    except SelectionError as exc:
        return _selection_error(str(exc), status=404)


@app.route("/api/user_decks/<deck_id>", methods=["PUT"])
def api_update_user_deck(deck_id):
    """既存デッキを上書き更新（編集保存）。"""
    payload = request.json or {}
    try:
        name = str(payload.get("name", "保存デッキ"))
        deck = [int(v) for v in payload.get("cards", [])]
        visibility = payload.get("visibility")  # None なら既存維持
        password = str(payload.get("password", ""))
        if not deck_has_basic(deck):
            raise SelectionError("たねポケモンを最低1枚入れてください。")
        saved = update_user_deck(deck_id, name, deck, visibility, password)
        return jsonify(saved)
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


@app.route("/api/favorites", methods=["GET", "PUT"])
def api_favorites():
    """お気に入りカテゴリ（全利用者で共有・data/favorites に永続）。

    構造: {"categories": [{"id","name","cards":[...]}]}（1枚が複数カテゴリ可）。"""
    if request.method == "GET":
        return jsonify(read_favorites())
    payload = request.get_json(force=True, silent=True) or {}
    return jsonify(save_favorites(payload))


@app.route("/card_img/<int:cid>", methods=["GET"])
def card_img(cid):
    fn = card_image_file(cid)
    if not fn:
        abort(404)
    return send_from_directory(CARD_IMAGES_DIR, fn, max_age=86400)


@app.route("/card_back", methods=["GET"])
def card_back():
    """カード裏面画像（面伏せ＝サイド/相手手札/山札の選択表示用）。"""
    return send_from_directory(CARD_IMAGES_DIR, "_back.png", max_age=86400)


# ---------- バトルロワイヤル ----------
def _royale_ladder():
    """保存済み総当たり結果から、現存する登録AIのTOP5を強い順で返す。

    挑戦は弱い相手(5位)→強い相手(1位)の順なので、フロント側で並びを反転して使う。
    既に削除されたAIは除外し、現存分の上位5体を採用する。
    """
    try:
        data = json.loads(ROYALE_RANKING_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    valid = {a["id"] for a in list_user_agents()}
    standings = [s for s in data.get("standings", [])
                 if s.get("id") in valid]
    ladder = []
    for rank, s in enumerate(standings[:5], 1):  # 1 が最強
        ladder.append({
            "id": s["id"], "name": s["name"], "rank": rank,
            "winRate": s.get("winRate"), "wins": s.get("wins"),
            "losses": s.get("losses"), "draws": s.get("draws"),
        })
    return ladder


@app.route("/api/royale/ranking", methods=["GET"])
def api_royale_ranking():
    """バトルロワイヤルの相手ラダー（現存TOP5）を返す。"""
    return jsonify({"ladder": _royale_ladder()})


@app.route("/api/royale/next", methods=["POST"])
def api_royale_next():
    """バトルロワイヤル: 直前の対戦の挑戦者設定のまま、次のラダー相手と即対戦を始める。

    ロワイヤルの選択画面に戻らず、勝利画面から直接次戦へ進むためのもの。挑戦者側は
    元ゲームに保存した rematch スペック（人/登録AI・デッキ）を再利用し、相手だけを
    次の順位のAIに差し替える。stage = 次に戦う相手のラダー上 index（0始まり, 弱→強）。
    """
    data = request.get_json(silent=True) or {}
    src = GAMES.get(data.get("gid") or _request_gid())
    if src is None or not src.get("rematch"):
        return jsonify({"error": "対戦情報が見つかりません（期限切れの可能性）。"
                                 "ロワイヤル画面から続けてください。"}), 404
    try:
        stage = int(data.get("stage"))
    except (TypeError, ValueError):
        return jsonify({"error": "stage が不正です。"}), 400
    ladder = list(reversed(_royale_ladder()))  # 弱→強（第1戦=index0）
    if stage < 0 or stage >= len(ladder):
        return jsonify({"error": "次の相手がいません（全クリア済み）。", "cleared": True}), 400
    opp_id = ladder[stage]["id"]
    spec = src["rematch"]
    gid = uuid.uuid4().hex
    g = _new_game_state(gid)
    g["rng"] = random.Random(None)
    player_deck = list(spec["player_deck"])
    try:
        code = read_user_agent_code(opp_id)
        name = next((a["name"] for a in list_user_agents() if a["id"] == opp_id), opp_id)
        forced = read_user_agent_deck(opp_id)
        opp_agent, opp_deck, opp_label = _materialize_agent(
            gid, "opponent", code, f"登録AI（{name}）", player_deck,
            forced_deck=forced, extra_src=user_agent_dir(opp_id))
    except SelectionError as exc:
        _destroy_game(gid)
        return _selection_error(str(exc))
    obs, start = battle_start(player_deck, opp_deck)
    if obs is None:
        _destroy_game(gid)
        return jsonify({"error": f"battle start failed (errorType={start.errorType})"}), 500
    g["ptr"] = start.battlePtr
    g["obs"], g["result"], g["running"], g["events"] = obs, -1, True, []
    g["mode"] = spec["mode"]
    g["controlled"] = {HUMAN}
    g["player_type"], g["player_agent"], g["player_label"] = (
        spec["player_type"], spec["player_agent"], spec["player_label"])
    g["opponent_type"], g["opponent_agent"] = "custom", opp_agent
    g["opponent_label"], g["deck_label"] = opp_label, spec["deck_label"]
    g["recorder"] = ReplayRecorder([spec["player_label"], opp_label], episode_id=gid)
    g["rematch"] = {**spec, "opponent_type": "custom", "opponent_agent": opp_agent,
                    "opponent_deck": list(opp_deck), "opponent_label": opp_label}
    GAMES[gid] = g
    _evict_if_needed()
    _activate(g)
    try:
        _advance_until_human(g)
    except SelectionError as exc:
        return _selection_error(str(exc))
    out = _state_json(g)
    out["royaleStage"] = stage + 1
    return jsonify(out)


@app.route("/api/royale/refresh", methods=["POST"])
def api_royale_refresh():
    """登録AIの総当たりをバックグラウンドで実行し、ロワイヤルのラダー元データ
    （ranking.json）を更新する。重い処理なので子プロセスに逃がす（評価ジョブと同方式）。"""
    metas = list_user_agents()
    if len(metas) < 2:
        return _selection_error("ランキング更新には登録AIが2体以上必要です。")
    meta_p = ROYALE_DIR / "_refresh.meta"
    prog_p = ROYALE_DIR / "_refresh.progress"
    # 既存ジョブが「本当に稼働中」なら二重起動を拒否（同一ファイルへの並行書き込み防止）。
    # 完了済みPIDがゾンビとして生存判定されても次回更新を塞がないよう、progress が
    # done>=total（完了）なら稼働中とみなさない。
    if meta_p.exists() and _pid_alive(_read_json(meta_p).get("pid")):
        prog = _read_json(prog_p)
        done, total = prog.get("done", 0), prog.get("total", 0)
        if not (total > 0 and done >= total):
            return jsonify({"error": "ランキング更新は既に実行中です。"}), 409
    ROYALE_DIR.mkdir(parents=True, exist_ok=True)
    # 前回の進捗(done==total)を新ジョブの完了と誤判定しないよう初期化する。
    # 子プロセスが total を確定し progress を書き直すまでの窓を塞ぐ。
    try:
        prog_p.unlink()
    except FileNotFoundError:
        pass
    job_id = uuid.uuid4().hex[:12]
    workers = max(1, (os.cpu_count() or 2) - 1)
    cmd = [sys.executable, "tools/run_tournament.py",
           "--games-per-pair", "2",
           "--workers", str(workers),
           "--progress-file", str(ROYALE_DIR / "_refresh.progress"),
           "--out", str(ROYALE_RANKING_FILE)]
    proc = subprocess.Popen(
        cmd, cwd=str(APP_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    (ROYALE_DIR / "_refresh.meta").write_text(json.dumps({
        "job_id": job_id, "pid": proc.pid, "agents": len(metas),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    return jsonify({"job_id": job_id, "agents": len(metas)})


@app.route("/api/royale/refresh", methods=["GET"])
def api_royale_refresh_status():
    """ランキング更新ジョブの進捗（稼働中か・done/total）を返す。"""
    running = _pid_alive(_read_json(ROYALE_DIR / "_refresh.meta").get("pid"))
    prog = _read_json(ROYALE_DIR / "_refresh.progress")
    done, total = prog.get("done", 0), prog.get("total", 0)
    # 全試合消化済みなら完了扱い。子プロセスを wait() していないため終了直後は
    # ゾンビが kill(pid,0) で生存判定されうるが、done>=total を優先して稼働中を打ち消す。
    complete = total > 0 and done >= total
    if complete:
        running = False
    return jsonify({"running": running, "done": done, "total": total,
                    "complete": complete})


def _royale_hof_load():
    try:
        entries = json.loads(ROYALE_HOF_FILE.read_text(encoding="utf-8"))
        return entries if isinstance(entries, list) else []
    except (OSError, ValueError):
        return []


@app.route("/api/royale/hof", methods=["GET"])
def api_royale_hof_list():
    """殿堂入りリストを返す（新しい順）。"""
    return jsonify({"entries": _royale_hof_load()})


@app.route("/api/royale/hof", methods=["POST"])
def api_royale_hof_add():
    """全勝ち抜き者を殿堂入りに登録する（クライアント申告制）。"""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:24]
    if not name:
        return jsonify({"error": "名前を入力してください。"}), 400
    ROYALE_DIR.mkdir(parents=True, exist_ok=True)
    entries = _royale_hof_load()
    entries.insert(0, {
        "name": name,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    entries = entries[:200]
    ROYALE_HOF_FILE.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"ok": True, "entries": entries})


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
        # 人vs人: 相手にデッキを知られないよう、ラベルは「ニックネーム」のみ（デッキ名は出さない）。
        nick = (request.form.get("nickname") or "").strip()[:20] or "プレイヤー1"
        g["labels"] = {0: nick}
        g["host_name"] = nick   # ロビー表示用（デッキ名は出さない）
        g["deck_label"] = "オンライン対戦"
        g["created_at"] = datetime.now(timezone.utc).isoformat()
        GAMES[gid] = g
        _evict_if_needed()
        return jsonify({"gid": gid, "token": token, "role": "host", "status": "waiting"})

    # --- 人 vs AI / AI vs AI ---
    # random          : 自分も相手も無作為（完全ランダム）
    # random_opponent : 自分側はフォームで選び、相手だけ無作為
    is_random = (request.form.get("random") or "").lower() in ("1", "true", "on", "yes")
    is_random_opp = (request.form.get("random_opponent") or "").lower() in ("1", "true", "on", "yes")
    try:
        if is_random:
            player_type, player_agent, player_label = "human", None, "あなた"
            player_deck, deck_label = _random_player_deck(g["rng"])
            opponent_type, opponent_agent, opponent_deck, opponent_label = _random_opponent(gid, g["rng"], player_deck)
        else:
            # 自分側: 人=保存済み/登録AIのデッキ、AI観戦=登録エージェント
            if match_mode == "ai":
                player_type, player_agent, player_deck, player_label = _selected_player_agent_side(gid)
                deck_label = player_label
            else:
                player_deck, deck_label = _selected_player_deck()
                player_type, player_agent, player_label = "human", None, "あなた"
            # 相手: ランダム or フォーム選択
            if is_random_opp:
                opponent_type, opponent_agent, opponent_deck, opponent_label = _random_opponent(gid, g["rng"], player_deck)
            else:
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
    g["recorder"] = ReplayRecorder([player_label, opponent_label], episode_id=gid)
    # 「もう一度対戦する」用に、解決済みの対戦内容を保存（ランダム対戦でも同条件で再戦できる）。
    # エージェントは状態を持たない callable なのでオブジェクトをそのまま再利用する。
    g["rematch"] = {
        "mode": match_mode,
        "player_type": player_type, "player_agent": player_agent,
        "player_deck": list(player_deck), "player_label": player_label,
        "opponent_type": opponent_type, "opponent_agent": opponent_agent,
        "opponent_deck": list(opponent_deck), "opponent_label": opponent_label,
        "deck_label": deck_label,
    }
    GAMES[gid] = g
    # 過去対戦パネル用に、再現できる選択(ランダム対戦は除く)を履歴へ記録。
    if not is_random and not is_random_opp:
        _record_battle_history(request.form, match_mode, player_label, opponent_label)
    _evict_if_needed()  # 他対戦を破棄する際 ptr が切り替わるので、この後で再アクティブ化する
    _activate(g)
    try:
        _advance_until_human(g)
    except SelectionError as exc:
        return _selection_error(str(exc))
    return jsonify(_state_json(g))


# ---- 組合せ(combo)＝デッキ+エージェント の保存API ----
@app.route("/api/combos", methods=["GET"])
def api_combos_list():
    return jsonify({"combos": list_user_combos()})


@app.route("/api/combos", methods=["POST"])
def api_combos_save():
    data = request.get_json(silent=True) or request.form
    try:
        combo = save_user_combo(
            (data.get("name") or "").strip(),
            (data.get("deck_id") or "").strip(),
            (data.get("agent_id") or "").strip(),
            (data.get("deck_label") or "").strip(),
            (data.get("agent_label") or "").strip(),
            (data.get("deck_mode") or "user").strip())
    except SelectionError as exc:
        return _selection_error(str(exc))
    return jsonify({"ok": True, "combo": combo})


@app.route("/api/combos/<combo_id>", methods=["DELETE"])
def api_combos_delete(combo_id):
    delete_user_combo(combo_id)
    return jsonify({"ok": True})


# ---- 対戦履歴（過去と同条件でワンクリック再戦するための再現用フォーム保存）----
BATTLE_HISTORY_FILE = LOGS_DIR / "battle_history.json"
BATTLE_HISTORY_MAX = 60


def _record_battle_history(form, mode, player_label, opponent_label):
    """対戦開始時に、再現用フォーム＋表示ラベルを履歴に追記（直近のみ保持）。
    アップロード系(file)や seed は再現に使わないので除外する。"""
    try:
        keep = {k: v for k, v in dict(form).items()
                if k != "seed" and "file" not in k}
        entry = {"ts": int(time.time() * 1000), "mode": mode,
                 "playerLabel": player_label, "opponentLabel": opponent_label,
                 "label": f"{player_label} vs {opponent_label}", "form": keep}
        hist = _read_json(BATTLE_HISTORY_FILE) if BATTLE_HISTORY_FILE.exists() else {}
        items = hist.get("items", []) if isinstance(hist, dict) else []
        items.insert(0, entry)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        BATTLE_HISTORY_FILE.write_text(
            json.dumps({"items": items[:BATTLE_HISTORY_MAX]}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception:
        pass


@app.route("/api/battle_history", methods=["GET"])
def api_battle_history():
    """過去の対戦を、同条件(form)で重複排除して直近順に返す（右パネル用）。"""
    hist = _read_json(BATTLE_HISTORY_FILE) if BATTLE_HISTORY_FILE.exists() else {}
    items = hist.get("items", []) if isinstance(hist, dict) else []
    seen, uniq = set(), []
    for it in items:
        key = json.dumps(it.get("form", {}), sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    return jsonify({"items": uniq[:20]})


@app.route("/api/rematch", methods=["POST"])
def api_rematch():
    """「もう一度対戦する」: 直前の対戦と同じ相手・デッキ・条件で新しい対戦を始める。

    元ゲームに保存した rematch スペック（解決済みのデッキ/エージェント/ラベル）を
    そのまま再利用するので、ランダム対戦でも相手は再抽選されず同条件になる。
    """
    src = GAMES.get(_request_gid())
    if src is None or not src.get("rematch"):
        return jsonify({"error": "再戦情報が見つかりません（対戦が期限切れの可能性）。"
                                 "お手数ですが相手を選び直してください。"}), 404
    spec = src["rematch"]
    gid = uuid.uuid4().hex
    g = _new_game_state(gid)
    g["rng"] = random.Random(request.form.get("seed") or None)
    player_deck, opponent_deck = list(spec["player_deck"]), list(spec["opponent_deck"])
    obs, start = battle_start(player_deck, opponent_deck)
    if obs is None:
        _destroy_game(gid)
        return jsonify({"error": f"battle start failed (errorType={start.errorType})"}), 500
    g["ptr"] = start.battlePtr
    g["obs"], g["result"], g["running"], g["events"] = obs, -1, True, []
    g["mode"] = spec["mode"]
    g["controlled"] = {HUMAN}
    g["player_type"], g["player_agent"], g["player_label"] = (
        spec["player_type"], spec["player_agent"], spec["player_label"])
    g["opponent_type"], g["opponent_agent"] = spec["opponent_type"], spec["opponent_agent"]
    g["opponent_label"], g["deck_label"] = spec["opponent_label"], spec["deck_label"]
    g["recorder"] = ReplayRecorder([spec["player_label"], spec["opponent_label"]], episode_id=gid)
    g["rematch"] = spec  # 連続で再戦できるよう引き継ぐ
    GAMES[gid] = g
    _evict_if_needed()
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
    g["labels"][1] = (request.form.get("nickname") or "").strip()[:20] or "プレイヤー2"
    _activate(g)
    obs, start = battle_start(g["decks"][0], g["decks"][1])
    if obs is None:
        return jsonify({"error": f"battle start failed (errorType={start.errorType})"}), 500
    g["ptr"] = start.battlePtr
    g["obs"], g["result"], g["running"], g["events"], g["status"] = obs, -1, True, [], "playing"
    g["recorder"] = ReplayRecorder([g["labels"].get(0, "プレイヤー1"),
                                    g["labels"].get(1, "プレイヤー2")], episode_id=g["gid"])
    try:
        _advance_until_human(g)
    except SelectionError as exc:
        return _selection_error(str(exc))
    return jsonify({"gid": g["gid"], "token": token, "role": "guest", "status": "playing"})


@app.route("/api/rooms", methods=["GET"])
def api_rooms():
    """募集中（相手待ち）のオンライン対戦ルーム一覧。

    URLを配らなくても、このロビーから直接参加できるようにするためのもの。
    古すぎる放置ルーム（既定60分超）は除外して表示する。"""
    now = datetime.now(timezone.utc)
    rooms = []
    for gid, g in GAMES.items():
        if g.get("mode") != "pvp" or g.get("status") != "waiting":
            continue
        if (g.get("decks") or [None, None])[1] is not None:
            continue
        created = g.get("created_at")
        age_sec = None
        if created:
            try:
                age_sec = int((now - datetime.fromisoformat(created)).total_seconds())
            except ValueError:
                age_sec = None
        if age_sec is not None and age_sec > 3600:
            continue
        rooms.append({"gid": gid, "host": g.get("host_name") or "プレイヤー1",
                      "ageSec": age_sec})
    rooms.sort(key=lambda r: (r["ageSec"] is None, r["ageSec"] or 0))
    return jsonify({"rooms": rooms})


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
    g["obs"] = _record_select(g, picks)
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
        g["obs"] = _record_select(g, picks)
        g["ver"] = g.get("ver", 0) + 1
        _advance_until_human(g)
    except SelectionError as exc:
        return jsonify({"error": str(exc), **_state_json(g)}), 400
    return jsonify(_state_json(g))


# ─────────────────────────────────────────────────────────────
# サンドボックス（お試し）: 両者を手動/AIで操作して局面を作り、
# search_* で What-if 試行（手を試す→効果ログ観察→任意ノードへ巻き戻す）を行う。
# ─────────────────────────────────────────────────────────────

def _sandbox_opp_agent(gid, agent_id, opp_deck):
    """相手をAIにするとき、登録AIのコードを相手デッキ(固定)で動かす callable を作る。"""
    code = read_user_agent_code(agent_id)
    name = next((a["name"] for a in list_user_agents() if a["id"] == agent_id), agent_id)
    agent, _used_deck, _src = _materialize_agent(
        gid, "opponent", code, f"AI（{name}）", opp_deck,
        forced_deck=opp_deck, extra_src=user_agent_dir(agent_id))
    return agent, f"AI（{name}）"


@app.route("/api/sandbox/new", methods=["POST"])
def api_sandbox_new():
    """サンドボックス開始: 自分/相手のデッキを両方選び、両者手動で局面を作る。"""
    gid = uuid.uuid4().hex
    g = _new_game_state(gid)
    g["rng"] = random.Random(request.form.get("seed") or None)
    try:
        my_deck, my_label = _selected_deck(
            "deck_mode", "deck_id", "user_deck_id", "deck_file",
            agent_id_field="deck_agent_id", pw_field="deck_password")
        _require_basic_deck(my_deck)
        opp_deck, opp_label = _selected_deck(
            "opp_deck_mode", "opp_deck_id", "opp_user_deck_id", "opp_deck_file",
            agent_id_field="opp_deck_agent_id", pw_field="opp_deck_password")
        _require_basic_deck(opp_deck)
    except SelectionError as exc:
        _destroy_game(gid)
        return _selection_error(str(exc))
    obs, start = battle_start(my_deck, opp_deck)
    if obs is None:
        _destroy_game(gid)
        return jsonify({"error": f"battle start failed (errorType={start.errorType})"}), 500
    g["ptr"] = start.battlePtr
    g["obs"], g["result"], g["running"], g["events"] = obs, -1, True, []
    g["mode"] = "sandbox"
    g["controlled"] = {0, 1}          # 既定は両者手動
    g["opp_manual"] = True
    g["player_label"], g["opponent_label"] = "プレイヤー1", "プレイヤー2"
    g["deck_label"] = f"{my_label} / {opp_label}"
    g["my_deck"], g["opp_deck"] = list(my_deck), list(opp_deck)
    g["opponent_type"], g["opponent_agent"] = "rule", None
    g["search"] = None
    g["recorder"] = ReplayRecorder([g["player_label"], g["opponent_label"]], episode_id=gid)
    GAMES[gid] = g
    _evict_if_needed()
    _activate(g)
    try:
        _advance_until_human(g)
    except SelectionError as exc:
        return _selection_error(str(exc))
    return jsonify(_state_json(g, HUMAN, reveal_all=True))


@app.route("/api/sandbox/select", methods=["POST"])
def api_sandbox_select():
    """現在の手番プレイヤー（自分/相手どちらでも）の1手を進める。"""
    g = _get_game(_request_gid())
    if g is None or g["obs"] is None or g.get("mode") != "sandbox":
        return jsonify(NO_GAME)
    if g["result"] != -1:
        return jsonify(_state_json(g, HUMAN, reveal_all=True))
    ob = to_observation_class(g["obs"])
    sel = ob.select
    if sel is None:
        return jsonify(_state_json(g, HUMAN, reveal_all=True))
    picks = (request.get_json(silent=True) or {}).get("picks", [])
    n = len(sel.option)
    ok = (isinstance(picks, list) and all(isinstance(p, int) for p in picks)
          and sel.minCount <= len(picks) <= min(sel.maxCount, n)
          and all(0 <= p < n for p in picks) and len(set(picks)) == len(picks))
    if not ok:
        return jsonify({"error": "invalid picks", **_state_json(g, HUMAN, reveal_all=True)}), 400
    g["events"] = []
    g["obs"] = _record_select(g, picks)
    g["ver"] = g.get("ver", 0) + 1
    try:
        _advance_until_human(g)
    except SelectionError as exc:
        return jsonify({"error": str(exc), **_state_json(g, HUMAN, reveal_all=True)}), 400
    return jsonify(_state_json(g, HUMAN, reveal_all=True))


@app.route("/api/sandbox/config", methods=["POST"])
def api_sandbox_config():
    """相手を「手動 / AI」で切り替える。AI化時は登録AIを選べる。"""
    g = _get_game(_request_gid())
    if g is None or g.get("mode") != "sandbox":
        return jsonify(NO_GAME)
    body = request.get_json(silent=True) or {}
    opp_manual = bool(body.get("opp_manual", True))
    g["opp_manual"] = opp_manual
    if opp_manual:
        g["controlled"] = {0, 1}
        g["opponent_type"], g["opponent_agent"] = "rule", None
        g["opponent_label"] = "プレイヤー2"
    else:
        agent_id = (body.get("opponent_agent_id") or "").strip()
        try:
            if agent_id:
                agent, label = _sandbox_opp_agent(g["gid"], agent_id, g["opp_deck"])
                g["opponent_type"], g["opponent_agent"] = "custom", agent
                g["opponent_label"] = label
            else:
                g["opponent_type"], g["opponent_agent"] = "rule", None
                g["opponent_label"] = "ルールベースAI"
        except SelectionError as exc:
            return _selection_error(str(exc))
        except Exception as exc:
            return _selection_error(f"AIの読み込みに失敗しました: {exc}")
        g["controlled"] = {HUMAN}
        # 相手(index1)の手番が残っていれば消化する
        if g["result"] == -1 and g["obs"] is not None:
            _activate(g)
            try:
                _advance_until_human(g)
            except SelectionError as exc:
                return jsonify({"error": str(exc), **_state_json(g, HUMAN, reveal_all=True)}), 400
    return jsonify(_state_json(g, HUMAN, reveal_all=True))


def _search_state_json(search_state, your_index):
    """SearchState(Observation) を描画用 JSON にする。盤面の向きは your_index を下段に固定。"""
    ob = search_state.observation
    st = ob.current
    out = {"searchId": search_state.searchId, "result": -1}
    if st is None:
        out.update({"board": None, "select": None, "logs": [], "mover": None})
        return out
    if getattr(st, "result", -1) is not None and st.result >= 0:
        out["result"] = st.result

    def side(idx):
        p = st.players[idx]
        act = p.active[0] if p.active and p.active[0] else None
        hand = [card_meta(c.id) for c in p.hand] if p.hand is not None else None
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

    you, opp = your_index, 1 - your_index
    out["board"] = {"turn": st.turn, "you": side(you), "opp": side(opp)}
    out["mover"] = st.yourIndex
    try:
        out["logs"] = translate_logs(ob, your_index)
    except Exception:
        out["logs"] = []
    sel = ob.select
    if sel is not None and out["result"] == -1:
        opts = []
        for i, o in enumerate(sel.option):
            try:
                otype = OptionType(o.type).name
            except ValueError:
                otype = str(o.type)
            pi = getattr(o, "playerIndex", None)
            side_ = "opp" if (pi is not None and pi != you) else "you"
            opts.append({
                "index": i, "label": render_option(o, st, sel),
                "card": option_card(o, st, sel), "optionType": otype,
                "area": getattr(o, "area", None), "srcIndex": getattr(o, "index", None),
                "playerIndex": pi, "side": side_,
                "inPlayArea": getattr(o, "inPlayArea", None),
                "inPlayIndex": getattr(o, "inPlayIndex", None),
                "attackId": getattr(o, "attackId", None),
                "number": getattr(o, "number", None),
            })
        out["select"] = {"context": int(sel.context), "minCount": sel.minCount,
                         "maxCount": min(sel.maxCount, len(sel.option)), "options": opts}
    else:
        out["select"] = None
    return out


def _search_tree_summary(g):
    """探索ツリーの軽量サマリ（UI 描画用）。"""
    s = g.get("search") or {}
    nodes = s.get("nodes", {})
    return {"root": s.get("root"),
            "nodes": [{"searchId": n["searchId"], "parent": n["parent"],
                       "moveLabel": n.get("moveLabel"), "mover": n["view"].get("mover"),
                       "result": n["view"].get("result", -1)}
                      for n in nodes.values()]}


@app.route("/api/sandbox/search/begin", methods=["POST"])
def api_sandbox_search_begin():
    """現局面から What-if 探索を開始（相手の隠れ札を決定化、コイン固定オプション）。"""
    g = _get_game(_request_gid())
    if g is None or g["obs"] is None or g.get("mode") != "sandbox":
        return jsonify(NO_GAME)
    if g["obs"].get("search_begin_input") is None or g["obs"].get("select") is None:
        return jsonify({"error": "この局面からは試行を開始できません（選択待ちの局面で開始してください）。"}), 400
    manual_coin = bool((request.get_json(silent=True) or {}).get("manual_coin", False))
    ob = to_observation_class(g["obs"])
    yi = ob.current.yourIndex if ob.current else HUMAN
    det = build_determinization(ob, yi, g.get("my_deck") or [], g.get("opp_deck"))
    _activate(g)
    try:
        root_state = search_begin(ob, manual_coin=manual_coin, **det)
    except Exception as exc:
        try:
            search_end()
        except Exception:
            pass
        return jsonify({"error": f"探索の開始に失敗しました: {exc}"}), 400
    if root_state is None:
        try:
            search_end()
        except Exception:
            pass
        return jsonify({"error": "探索を開始できませんでした。"}), 400
    view = _search_state_json(root_state, yi)
    g["search"] = {"your_index": yi, "root": root_state.searchId,
                   "manual_coin": manual_coin, "nodes": {}}
    g["search"]["nodes"][root_state.searchId] = {
        "searchId": root_state.searchId, "parent": None, "picks": None,
        "moveLabel": "開始局面", "view": view}
    return jsonify({"node": view, "tree": _search_tree_summary(g)})


@app.route("/api/sandbox/search/step", methods=["POST"])
def api_sandbox_search_step():
    """探索ツリーの指定ノードから1手進める（任意ノードから分岐できる）。"""
    g = _get_game(_request_gid())
    if g is None or g.get("mode") != "sandbox" or not g.get("search"):
        return jsonify({"error": "試行が開始されていません。"}), 400
    body = request.get_json(silent=True) or {}
    sid = body.get("searchId")
    picks = body.get("picks", [])
    s = g["search"]
    parent = s["nodes"].get(sid)
    if parent is None:
        return jsonify({"error": "指定のノードが見つかりません。"}), 400
    psel = parent["view"].get("select")
    if psel is None:
        return jsonify({"error": "このノードは選択待ちではありません。"}), 400
    n = len(psel["options"])
    ok = (isinstance(picks, list) and all(isinstance(p, int) for p in picks)
          and psel["minCount"] <= len(picks) <= min(psel["maxCount"], n)
          and all(0 <= p < n for p in picks) and len(set(picks)) == len(picks))
    if not ok:
        return jsonify({"error": "invalid picks"}), 400
    _activate(g)
    try:
        ns = search_step(sid, picks)
    except Exception as exc:
        return jsonify({"error": f"手を進められませんでした: {exc}"}), 400
    yi = s["your_index"]
    view = _search_state_json(ns, yi)
    label = " / ".join(psel["options"][p]["label"] for p in picks) or "（決定）"
    s["nodes"][ns.searchId] = {"searchId": ns.searchId, "parent": sid,
                               "picks": picks, "moveLabel": label, "view": view}
    return jsonify({"node": view, "tree": _search_tree_summary(g)})


@app.route("/api/sandbox/search/goto", methods=["POST"])
def api_sandbox_search_goto():
    """指定ノードの盤面/選択肢を返す（＝巻き戻し。ツリーは破壊しない）。"""
    g = _get_game(_request_gid())
    if g is None or g.get("mode") != "sandbox" or not g.get("search"):
        return jsonify({"error": "試行が開始されていません。"}), 400
    sid = (request.get_json(silent=True) or {}).get("searchId")
    node = g["search"]["nodes"].get(sid)
    if node is None:
        return jsonify({"error": "指定のノードが見つかりません。"}), 400
    return jsonify({"node": node["view"], "tree": _search_tree_summary(g)})


@app.route("/api/sandbox/search/end", methods=["POST"])
def api_sandbox_search_end():
    """探索を終了しメモリを解放、ライブ盤面へ戻る。"""
    g = _get_game(_request_gid())
    if g is None or g.get("mode") != "sandbox":
        return jsonify(NO_GAME)
    if g.get("search"):
        _activate(g)
        try:
            search_end()
        except Exception:
            pass
        g["search"] = None
    return jsonify(_state_json(g, HUMAN, reveal_all=True))


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
