"""対戦開始前の相手/デッキ選択に関する純粋なヘルパー。

このモジュールは配布シミュレータ(cg)を import しない。
macOS 上でも CSV パースやカスタム AI の読み込みを単体検証できるようにするため。
"""
from __future__ import annotations

import csv
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


def save_user_deck(name: str, deck: list[int], decks_dir: Path | None = None) -> dict[str, str]:
    """ユーザー作成デッキをCSV+メタJSONとして保存する。"""
    name = (name or "保存デッキ").strip()[:80]
    validate_deck_for_builder(deck)
    base = decks_dir or USER_DECKS_DIR
    base.mkdir(parents=True, exist_ok=True)
    deck_id = f"{_slugify_deck_name(name)}_{int(time.time() * 1000)}"
    csv_path = base / f"{deck_id}.csv"
    meta_path = base / f"{deck_id}.json"
    csv_path.write_text("\n".join(str(cid) for cid in deck) + "\n", encoding="utf-8")
    meta_path.write_text(json.dumps({"id": deck_id, "name": name}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"id": deck_id, "name": name, "path": str(csv_path)}


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


def list_user_decks(decks_dir: Path | None = None) -> list[dict[str, str]]:
    base = decks_dir or USER_DECKS_DIR
    if not base.exists():
        return []
    decks: list[dict[str, str]] = []
    for csv_path in sorted(base.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True):
        deck_id = csv_path.stem
        name = deck_id
        meta_path = csv_path.with_suffix(".json")
        if meta_path.exists():
            try:
                name = json.loads(meta_path.read_text(encoding="utf-8")).get("name", name)
            except json.JSONDecodeError:
                pass
        decks.append({"id": deck_id, "name": name, "path": str(csv_path)})
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


def parse_decklist_comments(code: str) -> list[int] | None:
    """エージェントのコードから「カードID = 枚数」のデッキリスト注記を復元する。

    `Makuhita = 673  # ×2` / `Switch = 1123  # x2` のような行を集計する。
    合計がちょうど60枚になった場合のみデッキとして採用する（誤検出を避けるため）。
    """
    deck: list[int] = []
    for line in code.splitlines():
        m = re.search(r"=\s*(\d+)\s*#\s*[×xX*]\s*(\d+)", line)
        if m:
            deck.extend([int(m.group(1))] * int(m.group(2)))
    if len(deck) != 60:
        return None
    try:
        validate_deck_for_builder(deck)
    except SelectionError:
        return None
    return deck


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
