"""対戦開始前の相手/デッキ選択に関する純粋なヘルパー。

このモジュールは配布シミュレータ(cg)を import しない。
macOS 上でも CSV パースやカスタム AI の読み込みを単体検証できるようにするため。
"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import re
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Callable, cast

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_SUBMISSION = ROOT / "data" / "sample_submission"
SAMPLE_DECKS_DIR = ROOT / "data" / "sample_decks"
USER_DECKS_DIR = ROOT / "data" / "user_decks"
USER_AGENTS_DIR = ROOT / "data" / "user_agents"
USER_COMBOS_DIR = ROOT / "data" / "user_combos"
FAVORITES_DIR = ROOT / "data" / "favorites"
CARD_DATA_CSV = ROOT / "data" / "JP_Card_Data.csv"
DEFAULT_SAMPLE_DECK = SAMPLE_SUBMISSION / "deck.csv"
VALID_CARD_MIN = 1
VALID_CARD_MAX = 1267
BASIC_ENERGY_IDS = set(range(1, 9))

# 同梱スターターデッキの定義（表示順・ラベル・CSVファイル名）。
# CSV は data/sample_decks/ にあり、PDF(Card_ID List_JP)の使用可能カード(ID 1..1267)のみで構成。
SAMPLE_DECKS: list[dict[str, str]] = [
    {"id": "01_fire_emboar", "name": "ひば炎スターター 〈エースバーン〉",
     "desc": "速攻アタッカー型。フレアターボで素早く殴る。"},
    {"id": "02_water_feraligatr", "name": "みず激流スターター 〈オーダイル〉",
     "desc": "高HP・大ダメージ型。おおなみ160で押し切る。"},
    {"id": "03_grass_rillaboom", "name": "くさパンチスターター 〈ゴリランダー〉",
     "desc": "エネ加速型。ウッドハンマー180の一撃。"},
    {"id": "04_lightning_magnezone", "name": "でんげきスターター 〈ジバコイル〉",
     "desc": "バランス型。フラッシュボルト160。"},
    {"id": "05_fighting_conkeldurr", "name": "かくとうスターター 〈ローブシン〉",
     "desc": "パワー型。ガッツスイング250の大技。"},
]


def _sample_deck_path(deck_id: str) -> Path:
    """サンプルデッキIDからCSVパスを返す（不正IDは弾く）。"""
    if not any(d["id"] == deck_id for d in SAMPLE_DECKS):
        raise SelectionError(f"不明なサンプルデッキです: {deck_id}")
    return SAMPLE_DECKS_DIR / f"{deck_id}.csv"


class SelectionError(ValueError):
    """相手/デッキ選択の入力が不正な場合のエラー。"""


AgentFunc = Callable[[dict], list[int]]


def parse_deck_csv_text(text: str) -> list[int]:
    """deck.csv 形式のテキストから 60 枚のカードIDを読む。

    配布サンプルのような「1行に1カードID」の形式を主対象にしつつ、
    CSV アップロードとして扱いやすいようにカンマ区切りセルも受け付ける。
    空行/空セルは無視する。
    """
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")

    values: list[int] = []
    try:
        rows = csv.reader(text.splitlines())
        for row in rows:
            for cell in row:
                cell = cell.strip()
                if not cell:
                    continue
                values.append(int(cell))
    except ValueError as exc:
        raise SelectionError("デッキCSVにはカードID（整数）だけを入力してください。") from exc

    if len(values) != 60:
        raise SelectionError(f"デッキは60枚である必要があります（現在 {len(values)} 枚）。")
    return values


def read_deck_csv_file(path: str | Path) -> list[int]:
    """deck.csv ファイルから 60 枚のカードIDを読む。"""
    p = Path(path)
    if not p.exists():
        raise SelectionError(f"デッキCSVが見つかりません: {p}")
    return parse_deck_csv_text(p.read_text(encoding="utf-8-sig"))


def sample_deck_options() -> list[dict[str, str]]:
    """UI に表示するサンプルデッキ一覧（id・name・desc）。"""
    return [dict(d) for d in SAMPLE_DECKS]


def read_sample_deck(deck_id: str) -> list[int]:
    """サンプルデッキIDから60枚のカードIDを読む。"""
    return read_deck_csv_file(_sample_deck_path(deck_id))


def deck_card_counts(deck: list[int]) -> list[dict[str, int]]:
    """デッキ(60枚のID列)を {cardId, count} の一覧に集計する。

    並び順はデッキCSVでの初出順を保つ（ポケモン→トレーナーズ→エネルギーの順に並ぶ）。
    """
    order: list[int] = []
    counts: dict[int, int] = {}
    for cid in deck:
        if cid not in counts:
            counts[cid] = 0
            order.append(cid)
        counts[cid] += 1
    return [{"cardId": cid, "count": counts[cid]} for cid in order]


def _card_rules() -> dict[int, dict[str, object]]:
    """デッキ検証に必要な最低限のカード属性をCSVから読む。"""
    rules: dict[int, dict[str, object]] = {}
    if not CARD_DATA_CSV.exists():
        return rules
    with CARD_DATA_CSV.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                cid = int(row["カード ID"])
            except (KeyError, ValueError):
                continue
            kind = row.get("ポケモンの進化の段階/エネルギー・トレーナーズの種類", "")
            rule = row.get("ルール", "")
            current = rules.setdefault(cid, {"basic": False, "ace_spec": False})
            current["basic"] = bool(current["basic"] or "たね" in kind)
            current["ace_spec"] = bool(current["ace_spec"] or "ACE SPEC" in rule)
    return rules


def deck_has_basic(deck: list[int]) -> bool:
    """デッキにたねポケモンが1枚以上あるか。

    たねが無いデッキは対戦開始時に永久にマリガン（引き直し）が続いて進行不能になるため、
    対戦に入れる前にこれで弾く。カードデータが無い環境では判定不能なので True（通す）。"""
    rules = _card_rules()
    if not rules:
        return True
    return any(bool(rules.get(cid, {}).get("basic")) for cid in deck)


def validate_deck_for_builder(deck: list[int]) -> None:
    """デッキ作成画面から保存する60枚デッキを検証する。"""
    if len(deck) != 60:
        raise SelectionError(f"デッキは60枚である必要があります（現在 {len(deck)} 枚）。")
    invalid = [cid for cid in deck if cid < VALID_CARD_MIN or cid > VALID_CARD_MAX]
    if invalid:
        raise SelectionError(f"使用可能カード（ID {VALID_CARD_MIN}〜{VALID_CARD_MAX}）以外が含まれています: {invalid[:5]}")

    counts: dict[int, int] = {}
    for cid in deck:
        counts[cid] = counts.get(cid, 0) + 1
    over = [cid for cid, count in counts.items() if count > 4 and cid not in BASIC_ENERGY_IDS]
    if over:
        raise SelectionError(f"同じカードは基本エネルギー以外4枚までです: {over[:5]}")

    rules = _card_rules()
    if rules:
        if not any(bool(rules.get(cid, {}).get("basic")) for cid in deck):
            raise SelectionError("たねポケモンを最低1枚入れてください。")
        ace_count = sum(count for cid, count in counts.items() if bool(rules.get(cid, {}).get("ace_spec")))
        if ace_count > 1:
            raise SelectionError("ACE SPECはデッキに1枚までです。")


def _slugify_deck_name(name: str) -> str:
    mapping = {"炎": "fire", "火": "fire", "水": "water", "草": "grass", "雷": "lightning", "闘": "fighting", "悪": "darkness", "超": "psychic", "鋼": "metal"}
    prefix = next((v for k, v in mapping.items() if k in name), "")
    ascii_part = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not prefix and ascii_part:
        prefix = ascii_part
    if not prefix:
        prefix = "deck"
    return prefix[:32] or "deck"


def _hash_deck_password(deck_id: str, password: str) -> str:
    """デッキ非公開用パスワードのハッシュ（deck_idを塩に使用）。平文は保存しない。"""
    return hashlib.sha256((deck_id + ":" + (password or "")).encode("utf-8")).hexdigest()


def read_deck_meta(deck_id: str, decks_dir: Path | None = None) -> dict:
    """保存デッキの meta.json を読む（無ければ空dict）。"""
    base = decks_dir or USER_DECKS_DIR
    try:
        m = json.loads((base / f"{deck_id}.json").read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def deck_is_private(deck_id: str, decks_dir: Path | None = None) -> bool:
    return read_deck_meta(deck_id, decks_dir).get("visibility") == "private"


def check_deck_password(deck_id: str, password: str, decks_dir: Path | None = None) -> bool:
    """非公開デッキのパスワード照合。公開デッキは常に True。"""
    m = read_deck_meta(deck_id, decks_dir)
    if m.get("visibility") != "private":
        return True
    return bool(m.get("password_hash")) and _hash_deck_password(deck_id, password) == m["password_hash"]


def _deck_meta_payload(deck_id, name, visibility, password, old_hash=None):
    # updated_at(最終編集時刻, ms)を content として保存する。ファイルmtimeはS3同期/rsyncで
    # 転送時刻に書き換わり「最終編集順」を保てないため、一覧の並び替えはこの値を使う。
    meta = {"id": deck_id, "name": name,
            "visibility": "private" if visibility == "private" else "public",
            "updated_at": int(time.time() * 1000)}
    if meta["visibility"] == "private":
        if password:
            meta["password_hash"] = _hash_deck_password(deck_id, password)
        elif old_hash:
            meta["password_hash"] = old_hash          # 更新時、パスワード未入力なら既存を保持
        else:
            meta["password_hash"] = _hash_deck_password(deck_id, "")
    return meta


def save_user_deck(name: str, deck: list[int], visibility: str = "public",
                   password: str = "", decks_dir: Path | None = None) -> dict[str, str]:
    """ユーザー作成デッキをCSV+メタJSONとして保存する。"""
    name = (name or "保存デッキ").strip()[:80]
    validate_deck_for_builder(deck)
    base = decks_dir or USER_DECKS_DIR
    base.mkdir(parents=True, exist_ok=True)
    deck_id = f"{_slugify_deck_name(name)}_{int(time.time() * 1000)}"
    csv_path = base / f"{deck_id}.csv"
    csv_path.write_text("\n".join(str(cid) for cid in deck) + "\n", encoding="utf-8")
    meta = _deck_meta_payload(deck_id, name, visibility, password)
    (base / f"{deck_id}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"id": deck_id, "name": name, "visibility": meta["visibility"], "path": str(csv_path)}


def update_user_deck(deck_id: str, name: str, deck: list[int], visibility: str | None = None,
                     password: str = "", decks_dir: Path | None = None) -> dict[str, str]:
    """既存の保存デッキ(deck_id)を上書き更新する。idは保持。"""
    path = _safe_user_deck_path(deck_id, decks_dir)   # 存在チェック込み
    name = (name or "保存デッキ").strip()[:80]
    validate_deck_for_builder(deck)
    old = read_deck_meta(deck_id, decks_dir)
    vis = visibility if visibility in ("public", "private") else old.get("visibility", "public")
    path.write_text("\n".join(str(cid) for cid in deck) + "\n", encoding="utf-8")
    meta = _deck_meta_payload(deck_id, name, vis, password, old.get("password_hash"))
    path.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"id": deck_id, "name": name, "visibility": meta["visibility"], "path": str(path)}


def delete_user_deck(deck_id: str, decks_dir: Path | None = None) -> None:
    """保存デッキ(csv+meta)を削除する。"""
    path = _safe_user_deck_path(deck_id, decks_dir)   # 存在チェック込み
    path.unlink(missing_ok=True)
    path.with_suffix(".json").unlink(missing_ok=True)


def _safe_user_deck_path(deck_id: str, decks_dir: Path | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", deck_id or ""):
        raise SelectionError(f"不明な保存デッキです: {deck_id}")
    base = decks_dir or USER_DECKS_DIR
    path = base / f"{deck_id}.csv"
    if not path.exists():
        raise SelectionError(f"保存デッキが見つかりません: {deck_id}")
    return path


def read_user_deck(deck_id: str, decks_dir: Path | None = None) -> list[int]:
    return read_deck_csv_file(_safe_user_deck_path(deck_id, decks_dir))


def touch_user_deck_opened(deck_id: str, decks_dir: Path | None = None) -> None:
    """デッキを「開いた」時刻(opened_at, ms)を meta に記録する（一覧の並び順用）。

    updated_at（最終編集時刻）は変更しない。meta が壊れていても例外は投げない。
    """
    base = decks_dir or USER_DECKS_DIR
    meta = read_deck_meta(deck_id, decks_dir)
    if not meta:
        meta = {"id": deck_id}
    meta["opened_at"] = int(time.time() * 1000)
    try:
        (base / f"{deck_id}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# ===== お気に入りカード（全利用者で共有・data/favorites/favorites.json） =====
# 構造: {"categories": [{"id": "...", "name": "水軸デッキ", "cards": [9, 12]}, ...]}
# 1枚のカードは複数カテゴリに所属できる（タグ型）。
DEFAULT_FAV_CATEGORY = "お気に入り"


def _sanitize_favorites(ids) -> list[int]:
    """有効なカードID(1..1267)のみを重複排除・順序保持で返す。"""
    seen: set[int] = set()
    out: list[int] = []
    for v in (ids or []):
        try:
            cid = int(v)
        except (TypeError, ValueError):
            continue
        if VALID_CARD_MIN <= cid <= VALID_CARD_MAX and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def _sanitize_fav_categories(data) -> list[dict]:
    """任意の入力をお気に入りカテゴリのリストに正規化する。

    - 新形式 {"categories": [...]} はそのまま検証。
    - 旧形式 {"cards": [...]} / 素のID配列は「お気に入り」カテゴリ1つに移行。
    """
    if isinstance(data, dict) and isinstance(data.get("categories"), list):
        raw = data["categories"]
    elif isinstance(data, dict) and "cards" in data:
        raw = [{"name": DEFAULT_FAV_CATEGORY, "cards": data.get("cards", [])}]
    elif isinstance(data, list):
        raw = [{"name": DEFAULT_FAV_CATEGORY, "cards": data}]
    else:
        raw = []
    out: list[dict] = []
    seen_ids: set[str] = set()
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()[:80] or f"カテゴリ{i + 1}"
        cid = str(c.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", cid):
            cid = f"cat_{int(time.time() * 1000)}_{i}"
        while cid in seen_ids:
            cid += "_"
        seen_ids.add(cid)
        out.append({"id": cid, "name": name, "cards": _sanitize_favorites(c.get("cards", []))})
    return out


def read_favorites(fav_dir: Path | None = None) -> dict:
    """お気に入りカテゴリを読み込む。ファイル無し/壊れは空扱い。旧形式は自動移行。"""
    base = fav_dir or FAVORITES_DIR
    path = base / "favorites.json"
    if not path.exists():
        return {"categories": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"categories": []}
    return {"categories": _sanitize_fav_categories(data)}


def save_favorites(data, fav_dir: Path | None = None) -> dict:
    """お気に入りカテゴリを保存し、正規化後の内容を返す。"""
    cats = _sanitize_fav_categories(data)
    base = fav_dir or FAVORITES_DIR
    base.mkdir(parents=True, exist_ok=True)
    payload = {"categories": cats, "updated": int(time.time() * 1000)}
    (base / "favorites.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"categories": cats}


def _deck_updated_at(deck_id: str, meta: dict, csv_path: Path) -> int:
    """デッキの最終編集時刻(ms)。優先: meta.updated_at → ID末尾の作成時刻 → ファイルmtime。"""
    ua = meta.get("updated_at")
    if isinstance(ua, (int, float)) and ua > 0:
        return int(ua)
    # 旧デッキ(updated_at 無し): ID末尾に埋まっている作成時刻(ms)をフォールバックに使う。
    m = re.search(r"_(\d{11,})$", deck_id)
    if m:
        return int(m.group(1))
    try:
        return int(csv_path.stat().st_mtime * 1000)
    except OSError:
        return 0


def list_user_decks(decks_dir: Path | None = None) -> list[dict[str, str]]:
    base = decks_dir or USER_DECKS_DIR
    if not base.exists():
        return []
    decks: list[dict] = []
    for csv_path in base.glob("*.csv"):
        deck_id = csv_path.stem
        name = deck_id
        visibility = "public"
        meta = {}
        meta_path = csv_path.with_suffix(".json")
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                name = meta.get("name", name)
                visibility = "private" if meta.get("visibility") == "private" else "public"
            except json.JSONDecodeError:
                meta = {}
        updated_at = _deck_updated_at(deck_id, meta, csv_path)
        opened_at = meta.get("opened_at")
        opened_at = int(opened_at) if isinstance(opened_at, (int, float)) and opened_at > 0 else 0
        decks.append({"id": deck_id, "name": name, "visibility": visibility,
                      "path": str(csv_path),
                      "updated_at": updated_at,
                      # 「最後に触った時刻」= 開いた(opened_at) と 編集した(updated_at) の新しい方
                      "last_opened": max(opened_at, updated_at)})
    # 直近で開いた（または編集した）順（降順）。同値は id で安定化。
    decks.sort(key=lambda d: (d["last_opened"], d["id"]), reverse=True)
    return decks


def extract_agent_code_from_ipynb(text: str) -> str:
    """Jupyter notebook(.ipynb) のJSONから、提出用 main.py 相当のコードを取り出す。

    優先順位:
      1. `%%writefile main.py` セルがあれば、その中身（マジック行を除く）を採用。
         （Kaggle提出ノートブックの定番。これがそのまま main.py になる。）
      2. 無ければ全コードセルを連結。`%`/`!` のマジック行と、
         提出物作成だけのセル（tarfile / submission.tar.gz）は除外する。
    """
    try:
        nb = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SelectionError("notebook(.ipynb)を読み込めませんでした（JSON不正）。") from exc
    cells = nb.get("cells", []) if isinstance(nb, dict) else []
    code_cells: list[str] = []
    for c in cells:
        if isinstance(c, dict) and c.get("cell_type") == "code":
            src = c.get("source", "")
            code_cells.append("".join(src) if isinstance(src, list) else str(src))

    # 1) %%writefile main.py セルを優先
    for src in code_cells:
        for i, line in enumerate(src.splitlines()):
            if line.strip().startswith("%%writefile") and "main.py" in line:
                return "\n".join(src.splitlines()[i + 1:]).strip()
            if line.strip():
                break  # セル先頭の実コードがマジックでなければ次のセルへ

    # 2) フォールバック: コードセルを連結（マジック/パッケージングは除外）
    parts: list[str] = []
    for src in code_cells:
        if "tarfile" in src or "submission.tar.gz" in src:
            continue
        cleaned = "\n".join(
            ln for ln in src.splitlines() if not ln.lstrip().startswith(("%", "!"))
        )
        if cleaned.strip():
            parts.append(cleaned)
    code = "\n\n".join(parts).strip()
    if not code:
        raise SelectionError("notebookからエージェントのコードを抽出できませんでした。")
    return code


_DECK_VAR_RE = re.compile(r"deck|decklist|cards", re.I)


def _eval_int(node, symbols):
    """ast ノードを int に評価（整数リテラル or 整数を指す変数名）。無理なら None。"""
    import ast
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Name):
        return symbols.get(node.id)
    return None


def _eval_int_list(node, symbols):
    """ast ノードを int のリストに評価（[id…]/[id]*n/連結+、要素は数値 or 変数名）。"""
    import ast
    if isinstance(node, ast.List):
        out = []
        for el in node.elts:
            v = _eval_int(el, symbols)
            if v is None:
                return None
            out.append(v)
        return out
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add):
            left = _eval_int_list(node.left, symbols)
            right = _eval_int_list(node.right, symbols)
            return None if left is None or right is None else left + right
        if isinstance(node.op, ast.Mult):
            for a, b in ((node.left, node.right), (node.right, node.left)):
                lst = _eval_int_list(a, symbols)
                cnt = _eval_int(b, symbols)
                if lst is not None and cnt is not None:
                    return lst * cnt
            return None
    return None


def _deck_from_list_literal(code: str):
    """`DECK = [...]` / `my_deck = [DREEPY, ...] * n + ...` のリスト定義からデッキを復元する。

    `DREEPY = 123` のような「変数名=整数」も対応表として解決する。
    """
    import ast
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    symbols = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
                symbols[node.targets[0].id] = node.value.value
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if not any(_DECK_VAR_RE.search(n) for n in names):
            continue
        vals = _eval_int_list(node.value, symbols)
        if vals and len(vals) == 60:
            try:
                validate_deck_for_builder(vals)
                return vals
            except SelectionError:
                continue
    return None


def parse_decklist_comments(code: str) -> list[int] | None:
    """エージェントのコードからデッキ(60枚のID列)を静的に復元する。

    対応する記法:
      1. `Makuhita = 673  # ×2` / `Switch = 1123  # x2` のコメント注記
      2. `DECK = [...]` / `my_deck = [673]*2 + [674]*2 + ...` のリスト定義
    どちらも合計60枚・使用可能カードのときのみ採用する（誤検出を避けるため）。
    """
    deck: list[int] = []
    for line in code.splitlines():
        m = re.search(r"=\s*(\d+)\s*#\s*[×xX*]\s*(\d+)", line)
        if m:
            deck.extend([int(m.group(1))] * int(m.group(2)))
    if len(deck) == 60:
        try:
            validate_deck_for_builder(deck)
            return deck
        except SelectionError:
            pass
    return _deck_from_list_literal(code)


def load_custom_agent(path: str | Path) -> AgentFunc:
    """main.py 形式のカスタム AI から agent(obs_dict) を読み込む。"""
    p = Path(path)
    if not p.exists():
        raise SelectionError(f"カスタムAIファイルが見つかりません: {p}")
    if p.suffix != ".py":
        raise SelectionError("カスタムAIは main.py のような Python ファイルを指定してください。")

    module_name = f"custom_pokemon_agent_{abs(hash(p.resolve()))}"
    spec = importlib.util.spec_from_file_location(module_name, p)
    if spec is None or spec.loader is None:
        raise SelectionError(f"カスタムAIを読み込めません: {p}")

    module = importlib.util.module_from_spec(spec)
    _exec_module_with_parent_on_path(module, spec, p.parent)
    agent = getattr(module, "agent", None)
    if not callable(agent):
        raise SelectionError("カスタムAIには callable な agent(obs_dict) 関数が必要です。")
    return cast(AgentFunc, agent)


def _exec_module_with_parent_on_path(module: ModuleType, spec, parent: Path) -> None:
    parent_s = str(parent)
    added = False
    if parent_s not in sys.path:
        sys.path.insert(0, parent_s)
        added = True
    try:
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        raise SelectionError(f"カスタムAIの読み込み中にエラーが発生しました: {exc}") from exc
    finally:
        if added:
            try:
                sys.path.remove(parent_s)
            except ValueError:
                pass


def validate_ai_picks(picks: object, min_count: int, max_count: int, option_count: int) -> list[int]:
    """AI が返した選択肢 index をシミュレータに渡す前に検証する。"""
    if not isinstance(picks, list) or not all(isinstance(p, int) for p in picks):
        raise SelectionError("AI は list[int] を返す必要があります。")
    limit = min(max_count, option_count)
    if not (min_count <= len(picks) <= limit):
        raise SelectionError(f"AI の選択数が不正です（{min_count}〜{limit} 個）。")
    if any(p < 0 or p >= option_count for p in picks):
        raise SelectionError("AI が範囲外の選択肢を返しました。")
    if len(set(picks)) != len(picks):
        raise SelectionError("AI が重複した選択肢を返しました。")
    return picks


# ===== 登録AIエージェント（サーバー保存） =====
# data/user_agents/<id>/ に main.py（.ipynb はコード抽出済み）と meta.json を保存する。

def agent_code_from_upload(filename: str, raw_text: str) -> str:
    """アップロード内容から main.py 相当のコードを得る（.ipynb はコード抽出）。"""
    if filename.lower().endswith(".ipynb"):
        return extract_agent_code_from_ipynb(raw_text)
    return raw_text


def save_user_agent(name: str, filename: str, raw_text: str,
                    agents_dir: Path | None = None) -> dict:
    """登録AIをサーバーに保存する。戻り値は meta(id,name,filename,deck_source)。"""
    name = (name or "登録AI").strip()[:80] or "登録AI"
    code = agent_code_from_upload(filename or "main.py", raw_text)
    if "def agent" not in code:
        raise SelectionError("agent(obs_dict) 関数が見つかりません。main.py 形式のファイルを登録してください。")
    base = agents_dir or USER_AGENTS_DIR
    base.mkdir(parents=True, exist_ok=True)
    ascii_part = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "agent"
    agent_id = f"{ascii_part[:32]}_{int(time.time() * 1000)}"
    d = base / agent_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "main.py").write_text(code, encoding="utf-8")
    deck = parse_decklist_comments(code)
    meta = {"id": agent_id, "name": name, "filename": filename or "main.py",
            "deck_source": "ファイル内のデッキ" if deck else "対戦時にAIが決定",
            "deck": deck_card_counts(deck) if deck else None}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def preview_agent_upload(filename: str, raw_text: str) -> dict:
    """保存せずに、登録予定AIのデッキ内容プレビューを返す（登録前確認用）。"""
    code = agent_code_from_upload(filename or "main.py", raw_text)
    if "def agent" not in code:
        raise SelectionError("agent(obs_dict) 関数が見つかりません。main.py 形式のファイルを登録してください。")
    deck = parse_decklist_comments(code)
    return {"filename": filename or "main.py",
            "deck_source": "ファイル内のデッキ" if deck else "対戦時にAIが決定",
            "deck": deck_card_counts(deck) if deck else None}


def list_user_agents(agents_dir: Path | None = None) -> list[dict]:
    base = agents_dir or USER_AGENTS_DIR
    if not base.exists():
        return []
    agents: list[dict] = []
    for d in sorted([p for p in base.iterdir() if p.is_dir()],
                    key=lambda p: p.stat().st_mtime, reverse=True):
        meta_path = d / "meta.json"
        if meta_path.exists():
            try:
                agents.append(json.loads(meta_path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
    return agents


def user_agent_dir(agent_id: str, agents_dir: Path | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", agent_id or ""):
        raise SelectionError(f"不明なエージェントです: {agent_id}")
    base = agents_dir or USER_AGENTS_DIR
    d = base / agent_id
    if not (d / "main.py").exists():
        raise SelectionError(f"登録エージェントが見つかりません: {agent_id}")
    return d


def read_user_agent_code(agent_id: str, agents_dir: Path | None = None) -> str:
    return (user_agent_dir(agent_id, agents_dir) / "main.py").read_text(encoding="utf-8")


def delete_user_agent(agent_id: str, agents_dir: Path | None = None) -> None:
    import shutil
    shutil.rmtree(user_agent_dir(agent_id, agents_dir), ignore_errors=True)


# ---- 組合せ(combo): デッキ+エージェントの保存（AI対戦の自分側用） ----
# cg 非依存なので、ここで単体テストできる。各組合せは1ファイル(JSON)。

def _safe_combo_path(combo_id: str, combos_dir: Path | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", combo_id or ""):
        raise SelectionError(f"不明な組合せです: {combo_id}")
    base = combos_dir or USER_COMBOS_DIR
    return base / f"{combo_id}.json"


def save_user_combo(name: str, deck_id: str, agent_id: str,
                    deck_label: str = "", agent_label: str = "",
                    deck_mode: str = "user", combos_dir: Path | None = None) -> dict:
    """デッキ+エージェントの組合せを保存する。deck_id/agent_id は必須。
    deck_mode は sample/user/agent（デッキの出所）。"""
    name = (name or "保存した組合せ").strip()[:80]
    if not deck_id or not agent_id:
        raise SelectionError("デッキとエージェントの両方を選んでください。")
    if deck_mode not in ("sample", "user", "agent"):
        deck_mode = "user"
    base = combos_dir or USER_COMBOS_DIR
    base.mkdir(parents=True, exist_ok=True)
    combo_id = f"combo_{int(time.time() * 1000)}"
    payload = {
        "id": combo_id, "name": name,
        "deck_id": deck_id, "agent_id": agent_id, "deck_mode": deck_mode,
        "deck_label": deck_label or deck_id, "agent_label": agent_label or agent_id,
        "created_at": int(time.time() * 1000),
    }
    (base / f"{combo_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def list_user_combos(combos_dir: Path | None = None) -> list[dict]:
    base = combos_dir or USER_COMBOS_DIR
    if not base.exists():
        return []
    out: list[dict] = []
    for p in sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return out


def read_user_combo(combo_id: str, combos_dir: Path | None = None) -> dict:
    path = _safe_combo_path(combo_id, combos_dir)
    if not path.exists():
        raise SelectionError(f"組合せが見つかりません: {combo_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def delete_user_combo(combo_id: str, combos_dir: Path | None = None) -> None:
    _safe_combo_path(combo_id, combos_dir).unlink(missing_ok=True)


def _expand_deck_counts(counts) -> list[int]:
    """meta.json の deck（[{cardId,count}]）を 60 枚のID列へ展開する。"""
    ids: list[int] = []
    for c in counts or []:
        if not isinstance(c, dict):
            continue
        cid = c.get("cardId")
        n = c.get("count", 0)
        try:
            ids.extend([int(cid)] * int(n))
        except (TypeError, ValueError):
            continue
    return ids


def read_user_agent_deck(agent_id: str, agents_dir: Path | None = None) -> list[int] | None:
    """登録AIの保存済みデッキ(60枚のID列)を返す。無ければ None。

    agent_dir/deck.csv → meta.json の deck(枚数集計) の順に探す。
    Kaggle出力などから後付けで保存したデッキを対戦・選択で使うために用いる。
    """
    d = user_agent_dir(agent_id, agents_dir)
    p = d / "deck.csv"
    if p.exists():
        try:
            ids = [int(x) for x in p.read_text(encoding="utf-8-sig").split() if x.strip().isdigit()]
            if len(ids) == 60:
                return ids
        except (ValueError, OSError):
            pass
    meta_path = d / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            ids = _expand_deck_counts(meta.get("deck"))
            if len(ids) == 60:
                return ids
        except (json.JSONDecodeError, OSError):
            pass
    return None


def update_user_agent_deck(agent_id, deck, source, agents_dir=None):
    """登録AIの meta.json にデッキ(60枚)を後から設定する（登録時のAI問い合わせ結果など）。"""
    d = user_agent_dir(agent_id, agents_dir)
    meta_path = d / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        meta = {"id": agent_id, "name": agent_id}
    meta["deck"] = deck_card_counts(deck)
    meta["deck_source"] = source
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta
