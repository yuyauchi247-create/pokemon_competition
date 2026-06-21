"""共通ヘルパー: 配布シミュレータ(cg) の場所解決・カード名解決・盤面/選択肢の整形。

配布物は data/sample_submission/ にある前提（cg/ , main.py , deck.csv）。
このモジュールを import すると cg パッケージが import 可能になる。
カード名は data/JP_Card_Data.csv があれば日本語名を優先する。
"""
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_SUBMISSION = ROOT / "data" / "sample_submission"
if not (SAMPLE_SUBMISSION / "cg").is_dir():
    raise FileNotFoundError(
        f"配布シミュレータが見つかりません: {SAMPLE_SUBMISSION/'cg'}\n"
        f"data/sample_submission/ に cg/ ・ main.py ・ deck.csv を配置してください。"
    )
if str(SAMPLE_SUBMISSION) not in sys.path:
    sys.path.insert(0, str(SAMPLE_SUBMISSION))

# cg は data/sample_submission 配下のパッケージ
from cg.api import (  # noqa: E402
    to_observation_class, all_card_data, all_attack,
    OptionType, SelectContext, AreaType, EnergyType, CardType, LogType,
)
from cg.game import battle_start, battle_select, battle_finish  # noqa: E402
from cg.sim import Battle  # noqa: E402


def set_active_battle(ptr):
    """以降の battle_select / battle_finish が操作する対戦を切り替える。

    libcg は battle_ptr ごとに対戦状態を持つため、複数対戦を同時に扱える。
    各リクエストで対象対戦の ptr に切り替えてから cg を呼ぶこと
    （Flask をシングルスレッドで動かす前提で安全）。
    """
    Battle.battle_ptr = ptr


def active_battle():
    return Battle.battle_ptr


# ---- マスタ（遅延ロード）----
_CARD = None
_ATTACK = None
_JP = None
_IMG = None
CARD_IMAGES_DIR = ROOT / "data" / "card_images"


def _img_map():
    """{cardId(str): filename} を data/card_image_map.json から読む。"""
    global _IMG
    if _IMG is None:
        p = ROOT / "data" / "card_image_map.json"
        _IMG = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return _IMG


def card_image_file(cid):
    """カードIDの画像ファイル名（例 '721.jpeg'）。無ければ None。"""
    return _img_map().get(str(cid))


def _cards():
    global _CARD
    if _CARD is None:
        _CARD = {c.cardId: c for c in all_card_data()}
    return _CARD


def _jp_names():
    """data/JP_Card_Data.csv から {cardId: 日本語名} を作る。無ければ空。"""
    global _JP
    if _JP is not None:
        return _JP
    _JP = {}
    path = ROOT / "data" / "JP_Card_Data.csv"
    if path.exists():
        for enc in ("utf-8-sig", "cp932"):
            try:
                with path.open(encoding=enc, newline="") as f:
                    reader = csv.reader(f)
                    next(reader, None)  # ヘッダ
                    for row in reader:
                        if len(row) >= 2 and row[0].strip().isdigit():
                            _JP[int(row[0])] = row[1].strip()
                break
            except (UnicodeDecodeError, StopIteration):
                _JP = {}
                continue
    return _JP


def card_name(cid):
    """日本語名を優先。無ければ英語名。"""
    jp = _jp_names().get(cid)
    if jp:
        return jp
    c = _cards().get(cid)
    return c.name if c else f"card#{cid}"


def card_meta(cid):
    """UI用のカード情報（名前・タイプ・HP・種別・フラグ・画像URL）。"""
    img = f"/card_img/{cid}" if card_image_file(cid) else None
    c = _cards().get(cid)
    if not c:
        return {"id": cid, "name": card_name(cid), "type": "COLORLESS",
                "hp": 0, "cardType": "UNKNOWN", "flags": [], "img": img}
    flags = [f for f, v in (("ex", c.ex), ("MEGA", c.megaEx), ("tera", c.tera),
             ("ACE", c.aceSpec)) if v]
    try:
        etype = EnergyType(c.energyType).name
    except ValueError:
        etype = "COLORLESS"
    try:
        ctype = CardType(c.cardType).name
    except ValueError:
        ctype = "UNKNOWN"
    return {"id": cid, "name": card_name(cid), "type": etype,
            "hp": c.hp, "cardType": ctype, "flags": flags, "img": img}


def option_card(o, state=None, select=None):
    """選択肢が参照しているカードの card_meta を返す（無ければ None）。"""
    cid = getattr(o, "cardId", None)
    if not cid:
        cid = _card_at(state, getattr(o, "area", None),
                       getattr(o, "index", None), getattr(o, "playerIndex", None),
                       select=select)
    return card_meta(cid) if cid else None


# ---- 日本語カード詳細（JP CSV を 1ワザ=1行 で集約）----
_JP_DETAIL = None


def _jp_detail():
    global _JP_DETAIL
    if _JP_DETAIL is not None:
        return _JP_DETAIL
    _JP_DETAIL = {}
    path = ROOT / "data" / "JP_Card_Data.csv"
    if not path.exists():
        return _JP_DETAIL
    for enc in ("utf-8-sig", "cp932"):
        try:
            with path.open(encoding=enc, newline="") as f:
                reader = csv.reader(f)
                next(reader, None)
                for row in reader:
                    if len(row) < 17 or not row[0].strip().isdigit():
                        continue
                    cid = int(row[0])
                    nz = lambda s: ("" if s.strip() in ("", "n/a") else s.strip())
                    d = _JP_DETAIL.setdefault(cid, {
                        "stage": nz(row[4]), "rule": nz(row[5]), "category": nz(row[6]),
                        "evolvesFrom": nz(row[7]), "hp": nz(row[8]), "typeJP": nz(row[9]),
                        "weakness": nz(row[10]), "resistance": nz(row[11]),
                        "retreat": nz(row[12]), "moves": [], "text": ""})
                    mv = nz(row[13])
                    eff = nz(row[16])
                    if mv:
                        d["moves"].append({"name": mv, "cost": nz(row[14]),
                                           "damage": nz(row[15]), "effect": eff})
                    elif eff and not d["text"]:
                        # トレーナーズ等：ワザ名がなく効果説明のみ
                        d["text"] = eff
            break
        except (UnicodeDecodeError, StopIteration):
            _JP_DETAIL = {}
            continue
    return _JP_DETAIL


def card_detail(cid):
    """ポップアップ用の詳細情報（日本語）。各ワザに attackId を紐づける。"""
    base = card_meta(cid)
    jp = _jp_detail().get(cid, {})
    c = _cards().get(cid)
    atks = list(c.attacks) if c else []  # ワザID（順番）
    jp_moves = jp.get("moves", [])
    # 「コストかダメージがある行」をワザとみなす（特性等は除外）
    attack_rows = [m for m in jp_moves if (m.get("cost") or m.get("damage"))]
    # ワザ数が一致するときだけ順番で attackId を割当（ズレ防止）
    aligned = (len(attack_rows) == len(atks))
    ai = 0
    moves = []
    for m in jp_moves:
        nm = dict(m)
        nm["attackId"] = None
        if (m.get("cost") or m.get("damage")):
            if aligned and ai < len(atks):
                nm["attackId"] = atks[ai]
            ai += 1
        moves.append(nm)
    base["moves"] = moves
    base["text"] = jp.get("text", "")
    for k in ("stage", "rule", "category", "evolvesFrom", "weakness",
              "resistance", "retreat", "typeJP"):
        base[k] = jp.get(k, "")
    return base


# ---- ログ（できごと）の日本語化 ----
COND_NAME = {0: "どく", 1: "やけど", 2: "ねむり", 3: "まひ", 4: "こんらん"}


def translate_logs(ob, human_index=0):
    """Observation.logs を、人間視点の日本語イベント列に変換する。
    返り値: [{"who": "you"|"opp"|"", "text": str}]"""
    events = []
    if not ob or not ob.logs:
        return events

    def who(pi):
        if pi is None:
            return ""
        return "you" if pi == human_index else "opp"

    def label(pi):
        return "あなた" if pi == human_index else "相手"

    for lg in ob.logs:
        try:
            t = LogType(lg.type)
        except ValueError:
            continue
        pi = getattr(lg, "playerIndex", None)
        w = who(pi)
        nm = (card_name(lg.cardId) if getattr(lg, "cardId", None) else "")
        txt = None
        kind = None  # 連続ドロー集約用の内部タグ（"drawn"=名前公開, "drawh"=伏せ）
        dnm = None
        if t == LogType.TURN_START:
            txt = f"―― {label(pi)}のターン ――"
        elif t == LogType.HAS_BASIC_POKEMON:
            # 最初の手札にたねポケモンが無いと引き直し（マリガン）。
            # 開始時に「引いた」が大量に並ぶのはこの引き直しが理由なので明示する。
            if getattr(lg, "hasBasicPokemon", None) is False:
                txt = (f"↻ {label(pi)}は最初の手札にたねポケモンが無く、"
                       f"手札を引き直し（マリガン）")
            # たねポケモンがある場合は通知不要（ノイズ削減のため出さない）
        elif t == LogType.DRAW:
            # 相手が引いたカード名は隠れ情報なので伏せる（自分の引きのみ公開）
            if w == "you" and nm:
                txt = f"あなたが「{nm}」を引いた"
                kind, dnm = "drawn", nm
            else:
                txt = f"{label(pi)}がカードを引いた"
                kind = "drawh"
        elif t == LogType.DRAW_REVERSE:
            txt = f"{label(pi)}がカードを引いた"
            kind = "drawh"
        elif t == LogType.PLAY:
            txt = f"{label(pi)}が「{nm}」を出した／使った"
        elif t == LogType.ATTACH:
            tgt = card_name(lg.cardIdTarget) if getattr(lg, "cardIdTarget", None) else ""
            txt = f"{label(pi)}が「{nm}」を{('「'+tgt+'」に') if tgt else ''}つけた"
        elif t == LogType.EVOLVE:
            tgt = card_name(lg.cardIdTarget) if getattr(lg, "cardIdTarget", None) else ""
            txt = f"{label(pi)}が{('「'+tgt+'」を') if tgt else ''}「{nm}」に進化させた"
        elif t in (LogType.SWITCH, LogType.CHANGE):
            # 自分の意思か相手の効果(ハリテヤマ等)かに関わらず誤解を避け、受動形で「誰の」を明示する
            txt = f"{label(pi)}のバトルポケモンが入れ替わった"
        elif t == LogType.ATTACK:
            txt = f"{label(pi)}の「{nm}」のワザ！"
        elif t == LogType.HP_CHANGE:
            v = getattr(lg, "value", None)
            if v is not None and v < 0:
                txt = f"「{nm}」に {abs(v)} ダメージ"
            elif v:
                txt = f"「{nm}」のHPが {v} 回復"
        elif t in (LogType.POISONED, LogType.BURNED, LogType.ASLEEP,
                   LogType.PARALYZED, LogType.CONFUSED):
            cond = {LogType.POISONED: "どく", LogType.BURNED: "やけど",
                    LogType.ASLEEP: "ねむり", LogType.PARALYZED: "まひ",
                    LogType.CONFUSED: "こんらん"}[t]
            rec = getattr(lg, "isRecover", False)
            txt = f"「{nm}」の{cond}が回復" if rec else f"「{nm}」は{cond}になった"
        elif t == LogType.COIN:
            txt = f"コイン: {'オモテ' if getattr(lg,'head',False) else 'ウラ'}"
        elif t == LogType.RESULT:
            reason = {1: "サイドを取り切った", 2: "山札切れ", 3: "場のポケモン全滅",
                      4: "カードの効果"}.get(getattr(lg, "reason", 0), "")
            txt = f"決着（{reason}）" if reason else "決着"
        if txt:
            events.append({"who": w, "text": txt, "_k": kind, "_nm": dnm})
    return _compress_draws(events)


def _compress_draws(events):
    """連続するドローを1行に集約してログのノイズを抑える。

    - 伏せドロー（相手/自分の非公開）は2枚以上で「○○がカードをN枚引いた」。
    - 自分の名前公開ドローは4枚以上（=開始手札やドローサポートのまとめ引き）で
      「あなたがN枚引いた（A、B、…）」。通常の1〜3枚は元の1行ずつのまま。
    内部タグ(_k/_nm)は除去して {who,text} のみ返す。
    """
    out = []
    i, n = 0, len(events)
    while i < n:
        e = events[i]
        k = e.get("_k")
        if k in ("drawn", "drawh"):
            j = i
            while (j < n and events[j].get("_k") == k
                   and events[j]["who"] == e["who"]):
                j += 1
            grp = events[i:j]
            cnt = len(grp)
            lab = "あなた" if e["who"] == "you" else "相手"
            if k == "drawh" and cnt >= 2:
                out.append({"who": e["who"],
                            "text": f"{lab}がカードを{cnt}枚引いた"})
            elif k == "drawn" and cnt >= 4:
                names = "、".join(g.get("_nm") or "" for g in grp)
                out.append({"who": e["who"],
                            "text": f"{lab}が{cnt}枚引いた（{names}）"})
            else:
                out.extend({"who": g["who"], "text": g["text"]} for g in grp)
            i = j
        else:
            out.append({"who": e["who"], "text": e["text"]})
            i += 1
    return out


def _attack_obj(aid):
    global _ATTACK
    if _ATTACK is None:
        _ATTACK = {a.attackId: a for a in all_attack()}
    return _ATTACK.get(aid)


def attack_label(aid):
    a = _attack_obj(aid)
    if not a:
        return f"attack#{aid}"
    return f"{a.name}(ダメージ{a.damage})"


def _lead_int(s):
    """文字列の先頭の数字を取り出す（'100×' -> 100）。無ければ None。"""
    s = (s or "").strip()
    n = ""
    for ch in s:
        if ch.isdigit():
            n += ch
        else:
            break
    return int(n) if n else None


def read_deck(path=None):
    p = Path(path) if path else SAMPLE_SUBMISSION / "deck.csv"
    rows = p.read_text(encoding="utf-8").splitlines()
    return [int(rows[i]) for i in range(60)]


# ---- 選択肢ラベル ----
OPT_LABEL = {
    OptionType.NUMBER: "数を選ぶ",
    OptionType.YES: "はい",
    OptionType.NO: "いいえ",
    OptionType.CARD: "カード",
    OptionType.TOOL_CARD: "どうぐ",
    OptionType.ENERGY_CARD: "エネルギーカード",
    OptionType.ENERGY: "エネルギー",
    OptionType.PLAY: "手札から出す",
    OptionType.ATTACH: "付ける",
    OptionType.EVOLVE: "進化",
    OptionType.ABILITY: "特性",
    OptionType.DISCARD: "トラッシュ",
    OptionType.RETREAT: "にげる",
    OptionType.ATTACK: "ワザ",
    OptionType.END: "ターン終了",
    OptionType.SKILL: "効果の順番",
    OptionType.SPECIAL_CONDITION: "特殊状態",
}

AREA_JP = {
    AreaType.DECK: "山札", AreaType.HAND: "手札", AreaType.DISCARD: "トラッシュ",
    AreaType.ACTIVE: "バトル場", AreaType.BENCH: "ベンチ", AreaType.PRIZE: "サイド",
    AreaType.STADIUM: "スタジアム", AreaType.ENERGY: "エネ", AreaType.TOOL: "どうぐ",
    AreaType.PRE_EVOLUTION: "進化前", AreaType.PLAYER: "プレイヤー", AreaType.LOOKING: "確認中",
}


def _card_at(state, area, index, player, select=None):
    """(エリア, index, プレイヤー) から実際のカードを引いて id を返す。無ければ None。

    山札(DECK)からの選択は select.deck を、確認中(LOOKING)は state.looking を参照する。
    """
    if area is None or index is None:
        return None
    try:
        a = AreaType(area)
    except ValueError:
        return None
    # 山札サーチ等: カード実体は select.deck に入る
    if a == AreaType.DECK:
        deck = getattr(select, "deck", None) if select is not None else None
        if deck and 0 <= index < len(deck) and deck[index] is not None:
            return getattr(deck[index], "id", None)
        return None
    # 確認中（トップを見る等）: state.looking に入る
    if a == AreaType.LOOKING:
        looking = getattr(state, "looking", None) if state is not None else None
        if looking and 0 <= index < len(looking) and looking[index] is not None:
            return getattr(looking[index], "id", None)
        return None
    if state is None:
        return None
    pi = player if player is not None else state.yourIndex
    try:
        p = state.players[pi]
    except (IndexError, ValueError):
        return None
    arr = None
    if a == AreaType.HAND:
        arr = p.hand
    elif a == AreaType.ACTIVE:
        arr = p.active
    elif a == AreaType.BENCH:
        arr = p.bench
    elif a == AreaType.DISCARD:
        arr = p.discard
    elif a == AreaType.PRIZE:
        arr = p.prize
    if arr is None or index >= len(arr) or arr[index] is None:
        return None
    return getattr(arr[index], "id", None)


def render_option(o, state=None, select=None) -> str:
    """1つの選択肢(Option)を人間に読める短い文字列にする。
    state / select を渡すと、エリア+index で参照されるカードの名前も解決する。"""
    try:
        label = OPT_LABEL.get(OptionType(o.type), str(o.type))
    except ValueError:
        label = f"type{o.type}"
    parts = [label]
    if getattr(o, "attackId", None) is not None:
        parts.append(attack_label(o.attackId))
    # カード名: cardId があれば直接、無ければ盤面/デッキ参照から解決
    cid = getattr(o, "cardId", None)
    if not cid:
        cid = _card_at(state, getattr(o, "area", None),
                       getattr(o, "index", None), getattr(o, "playerIndex", None),
                       select=select)
    if cid:
        parts.append(card_name(cid))
    # 付ける/進化などは対象ポケモンも表示
    if getattr(o, "inPlayArea", None) is not None and state is not None:
        tgt = _card_at(state, o.inPlayArea, getattr(o, "inPlayIndex", None),
                       state.yourIndex, select=select)
        if tgt:
            parts.append(f"→「{card_name(tgt)}」")
    if getattr(o, "number", None) is not None:
        parts.append(f"={o.number}")
    return " ".join(parts)


def render_board(state) -> str:
    """盤面(State)をざっくりテキスト化。"""
    you = state.yourIndex
    opp = 1 - you
    lines = [f"--- ターン{state.turn} (あなた=player{you}) ---"]
    for who, idx in (("相手", opp), ("自分", you)):
        p = state.players[idx]
        act = p.active[0] if p.active and p.active[0] else None
        act_s = (f"{card_name(act.id)} HP{act.hp}/{act.maxHp} "
                 f"エネ{len(act.energies)}") if act else "(なし)"
        bench = ", ".join(f"{card_name(b.id)}(HP{b.hp})" for b in p.bench) or "(なし)"
        lines.append(
            f"[{who}] バトル場: {act_s} | ベンチ: {bench} | "
            f"手札{p.handCount} 山札{p.deckCount} サイド{len(p.prize)}"
        )
    # 自分の手札は中身が見えるので名前つきで列挙
    me = state.players[you]
    if me.hand:
        hand = "  ".join(f"#{i}:{card_name(c.id)}" for i, c in enumerate(me.hand))
        lines.append(f"[自分の手札] {hand}")
    return "\n".join(lines)
