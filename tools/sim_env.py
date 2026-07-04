"""共通ヘルパー: 配布シミュレータ(cg) の場所解決・カード名解決・盤面/選択肢の整形。

配布物は data/sample_submission/ にある前提（cg/ , main.py , deck.csv）。
このモジュールを import すると cg パッケージが import 可能になる。
カード名は data/JP_Card_Data.csv があれば日本語名を優先する。
"""
import csv
import json
import random
import sys
from collections import Counter
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
    search_begin, search_step, search_end, search_release,
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


def translate_logs(ob, human_index=0, st=None):
    """Observation.logs を、人間視点の日本語イベント列に変換する。
    返り値: [{"who": "you"|"opp"|"", "text": str}]

    st: ドロー種別判定の状態を delta をまたいで引き継ぐための dict。
        obs.logs は select 毎の差分のため、AIターンは複数 delta に分割される。
        PLAY（原因カード）とその効果ドローが別 delta になっても正しく紐付けるため、
        呼び出し側（_collect_events/_snap）が同じ dict を渡して状態を保持する。
        None の場合は1回ぶんで完結（後方互換）。"""
    events = []
    if not ob or not ob.logs:
        return events

    def who(pi):
        if pi is None:
            return ""
        return "you" if pi == human_index else "opp"

    def label(pi):
        return "あなた" if pi == human_index else "相手"

    # ドロー種別判定の状態（st があれば delta をまたいで引き継ぐ）
    if st is None:
        st = {}
    turn_player = st.get("turn_player")          # 現在ターンのプレイヤー
    pending_turn_draw = st.get("pending", False)  # TURN_START直後の通常ドロー待ち
    last_play_name = st.get("last_play")          # 直前に使ったカード（効果ドローの原因）

    for lg in ob.logs:
        try:
            t = LogType(lg.type)
        except ValueError:
            continue
        pi = getattr(lg, "playerIndex", None)
        w = who(pi)
        nm = (card_name(lg.cardId) if getattr(lg, "cardId", None) else "")
        txt = None
        # 連続ドロー集約用の内部タグ:
        #   drawturn=ターン開始の通常ドロー / draweff=カード効果のドロー
        #   drawn=初期手札の名前公開 / drawh=初期手札などの伏せ
        kind = None
        dnm = None      # 引いたカード名（自分の公開ドローのみ）
        cause = None    # 効果ドローの原因カード名（直前のPLAY）
        play_cid = None  # カード使用(PLAY)時のカードID（中央アニメ表示用）
        if t == LogType.TURN_START:
            txt = f"―― {label(pi)}のターン ――"
            turn_player = pi
            pending_turn_draw = True   # この直後の同player DRAW＝通常ドロー
            last_play_name = None      # ターンが変われば効果の原因はリセット
        elif t == LogType.TURN_END:
            last_play_name = None
        elif t == LogType.HAS_BASIC_POKEMON:
            # 最初の手札にたねポケモンが無いと引き直し（マリガン）。
            # 開始時に「引いた」が大量に並ぶのはこの引き直しが理由なので明示する。
            if getattr(lg, "hasBasicPokemon", None) is False:
                txt = (f"↻ {label(pi)}は最初の手札にたねポケモンが無く、"
                       f"手札を引き直し（マリガン）")
            # たねポケモンがある場合は通知不要（ノイズ削減のため出さない）
        elif t in (LogType.DRAW, LogType.DRAW_REVERSE):
            # 相手が引いたカード名は隠れ情報なので伏せる（自分の引きのみ公開）
            named = (t == LogType.DRAW and w == "you" and nm)
            dnm = nm if named else None
            if pending_turn_draw and pi == turn_player:
                kind = "drawturn"           # ターン開始の通常ドロー
                pending_turn_draw = False
            elif turn_player is not None and last_play_name:
                kind, cause = "draweff", last_play_name   # カード効果のドロー
            elif turn_player is not None:
                kind = "draweff"            # ターン中だが原因不明→効果扱い
            else:
                kind = "drawn" if named else "drawh"      # 開始時の手札/マリガン
            txt = "(draw)"  # プレースホルダ。実際の文言は _compress_draws で生成
        elif t == LogType.PLAY:
            txt = f"{label(pi)}が「{nm}」を出した／使った"
            last_play_name = nm   # 直後のドローはこのカードの効果とみなす
            play_cid = getattr(lg, "cardId", None)   # 中央アニメ表示用のカードID
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
            events.append({"who": w, "text": txt, "_k": kind,
                           "_nm": dnm, "_cause": cause, "_cid": play_cid})
    # 状態を呼び出し側に書き戻す（次 delta へ引き継ぐ）
    st["turn_player"] = turn_player
    st["pending"] = pending_turn_draw
    st["last_play"] = last_play_name
    return _compress_draws(events)


_DRAW_KINDS = ("drawturn", "draweff", "drawn", "drawh")


def _compress_draws(events):
    """ドローを種別ごとに集約し、通常ドロー/効果ドローを区別して文言化する。

    - 通常ドロー(drawturn): 「○○が「X」を引いた（ターン開始のドロー）」（常に1枚）。
    - 効果ドロー(draweff): 原因カードを明示し、2枚以上はまとめる。
      例「あなたが「博士の研究」の効果で7枚引いた（A、B…）」。
    - 開始手札(drawn/drawh): 伏せは2枚以上で「N枚引いた」、自分の公開は4枚以上で集約。
    連続する同種・同原因のドローを1行にまとめる。内部タグは除去する。
    """
    out = []
    i, n = 0, len(events)
    while i < n:
        e = events[i]
        k = e.get("_k")
        if k not in _DRAW_KINDS:
            ev = {"who": e["who"], "text": e["text"]}
            if e.get("_cid"):
                ev["cardId"] = e["_cid"]   # カード使用(PLAY)の中央アニメ表示用
            out.append(ev)
            i += 1
            continue
        cause = e.get("_cause")
        who = e["who"]
        j = i
        while (j < n and events[j].get("_k") == k
               and events[j]["who"] == who
               and events[j].get("_cause") == cause):
            j += 1
        grp = events[i:j]
        cnt = len(grp)
        lab = "あなた" if who == "you" else "相手"
        names = [g.get("_nm") for g in grp if g.get("_nm")]
        if k == "drawturn":
            # 通常ドローは1枚ずつ（名前があれば公開）。
            for g in grp:
                if g.get("_nm"):
                    txt = f"{lab}が「{g['_nm']}」を引いた（ターン開始のドロー）"
                else:
                    txt = f"{lab}がカードを引いた（ターン開始のドロー）"
                out.append({"who": who, "text": txt})
        elif k == "draweff":
            cc = f"「{cause}」の効果で" if cause else "効果で"
            if names:
                if cnt >= 2:
                    txt = f"{lab}が{cc}{cnt}枚引いた（{'、'.join(names)}）"
                else:
                    txt = f"{lab}が{cc}「{names[0]}」を引いた"
            else:
                txt = (f"{lab}が{cc}カードを{cnt}枚引いた" if cnt >= 2
                       else f"{lab}が{cc}カードを引いた")
            out.append({"who": who, "text": txt})
        elif k == "drawh":
            txt = (f"{lab}がカードを{cnt}枚引いた" if cnt >= 2
                   else f"{lab}がカードを引いた")
            out.append({"who": who, "text": txt})
        else:  # drawn（開始手札の名前公開）
            if cnt >= 4:
                out.append({"who": who,
                            "text": f"{lab}が{cnt}枚引いた（{'、'.join(names)}）"})
            else:
                for nm in names:
                    out.append({"who": who, "text": f"{lab}が「{nm}」を引いた"})
        i = j
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


# ワザID → そのワザを持つカードID の逆引き（こうまんしれい等「他ポケモンのワザ」表示用）
_ATTACK_TO_CARD = None


def attack_source_card(aid):
    """指定ワザ(attackId)を持つカードの cardId を返す。無ければ None。"""
    global _ATTACK_TO_CARD
    if _ATTACK_TO_CARD is None:
        _ATTACK_TO_CARD = {}
        for c in all_card_data():
            for a in (getattr(c, "attacks", None) or []):
                _ATTACK_TO_CARD.setdefault(a, c.cardId)
    return _ATTACK_TO_CARD.get(aid)


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


# ---- What-if 探索（search_begin）用の決定化ヘルパ ----

def _any_basic_id(pool=None):
    """基本たねポケモンの cardId を1つ返す（探索の相手アクティブ仮定用）。

    pool（デッキのID列）が渡されればその中のたねを優先。無ければ全カードから探す。
    """
    cards = _cards()
    if pool:
        for cid in pool:
            c = cards.get(cid)
            if c and getattr(c, "basic", False):
                return cid
    for cid, c in cards.items():
        if getattr(c, "basic", False):
            return cid
    return next(iter(cards), 0)


def _ensure_basic(deck_pred, pool):
    """予測デッキにたねポケモンが1枚も無ければ先頭を差し替える。

    search_begin は「相手デッキに最低1枚のたねポケモン」を要求するため。
    """
    cards = _cards()
    if any(getattr(cards.get(cid), "basic", False) for cid in deck_pred):
        return deck_pred
    if deck_pred:
        deck_pred[0] = _any_basic_id(pool)
    return deck_pred


def _seen_cards(pl):
    """プレイヤーの「見えている札」(手札・場・トラッシュ)を Counter で数える。"""
    c = Counter()
    for card in (pl.hand or []):
        c[card.id] += 1
    for p in (pl.active + pl.bench):
        if p is None:
            continue
        c[p.id] += 1
        for grp in (p.energyCards, p.tools, p.preEvolution):
            for card in grp:
                c[card.id] += 1
    for card in pl.discard:
        c[card.id] += 1
    return c


def _hidden_pool(deck_list, seen, need):
    """デッキ60枚から見えている札を除いた「隠れ札」を need 枚シャッフルして返す。"""
    remaining = []
    for cid, n in Counter(deck_list).items():
        remaining.extend([cid] * max(0, n - seen.get(cid, 0)))
    if len(remaining) < need:  # 念のため不足分を補う（デッキ推定がズレていても落ちないように）
        fill = deck_list or [_any_basic_id()]
        remaining += random.choices(fill, k=need - len(remaining))
    random.shuffle(remaining)
    return remaining


def build_determinization(obs, your_index, my_deck, opp_deck=None):
    """search_begin に渡す隠れ情報(山札/サイド/相手手札/相手アクティブ)を1つ仮定する。

    PIMC の「決定化」。obs は agent に渡ってきた Observation、your_index は探索する側。
    my_deck は自分の60枚（既知）。opp_deck は分かっていれば相手の60枚
    （サンドボックスでは自分で選ぶので既知）。未指定なら自デッキを相手プールに流用する。
    返り値は search_begin のキーワード引数 dict。
    """
    state = obs.current
    me = state.players[your_index]
    opp = state.players[1 - your_index]

    # 自分の山札/サイド = 自デッキ − 見えている自分の札
    my_hidden = _hidden_pool(my_deck, _seen_cards(me), me.deckCount + len(me.prize))
    your_deck = my_hidden[:me.deckCount]
    your_prize = my_hidden[me.deckCount:me.deckCount + len(me.prize)]

    # 相手: デッキ既知なら「相手デッキ − 見えている相手の札」から手札/山札/サイドを配分。
    #        未知なら自デッキを mirror prior として流用する。
    if opp_deck:
        need = opp.deckCount + len(opp.prize) + opp.handCount
        opp_hidden = _hidden_pool(opp_deck, _seen_cards(opp), need)
        opp_hand = opp_hidden[:opp.handCount]
        opp_deck_pred = opp_hidden[opp.handCount:opp.handCount + opp.deckCount]
        opp_prize = opp_hidden[opp.handCount + opp.deckCount:need]
    else:
        pool = my_deck
        opp_deck_pred = random.choices(pool, k=opp.deckCount) if opp.deckCount else []
        opp_prize = random.choices(pool, k=len(opp.prize)) if opp.prize else []
        opp_hand = random.choices(pool, k=opp.handCount) if opp.handCount else []
    opp_deck_pred = _ensure_basic(opp_deck_pred, opp_deck or my_deck)

    opp_active = []
    if len(opp.active) > 0 and opp.active[0] is None:  # 相手の場が裏向きならたねを仮定
        opp_active = [_any_basic_id(opp_deck or my_deck)]

    return dict(your_deck=your_deck, your_prize=your_prize,
                opponent_deck=opp_deck_pred, opponent_prize=opp_prize,
                opponent_hand=opp_hand, opponent_active=opp_active)
