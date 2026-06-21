# 提出物のしくみ解説（チーム共有用）

このコンペ（ポケカ対戦AI）に**自分のAIを提出する**ための入門資料です。
具体例として `submissions/improved_lucario_robust_v1/main.py`（総当たり1位ベースを改良したAI）を
使い、「提出物が何で・コードがどう動くか」をイメージできるようにします。

ポケカやKaggleが初めてでも読めるように、用語から説明します。

---

## 1. まずスコアの仕組みを知る（ここが一番大事）

提出すると**μ=600**から始まり、**他の人のAIと自動対戦してレートが上下**します（TrueSkill系）。

| ポイント | 内容 |
|---|---|
| 初期値 | 全員 **600 からスタート**。提出直後の600は「評価結果」ではなく**出発点** |
| 収束 | 最初は不確実性が大きく対戦が頻繁 → **数時間〜1日かけて**本来のレートに近づく |
| 何で決まる | **勝ち / 引き分け / 負け のみ**。点差（大勝ち）は無関係 |
| 審査の関門 | **自分のコピーとのミラー戦**で“落ちない（エラーで投了しない）”ことが必須 |
| 強い帯 | 公開上位は **915〜1084** あたり。ここへ登れるかを見る |

> つまり「とにかく合法手を返し続けて勝つ」が正義。**エラーで1回落ちると敗北**になるので、
> “**落ちない設計**”がレートに直結します（→ 6章の堅牢ラッパ）。

---

## 2. 提出物の中身

提出は **`submission.tar.gz`**（gzip圧縮のtar。**.zipは不可**）。中身は3つ：

```
submission.tar.gz
├── main.py     ← AI本体（agent関数）
├── deck.csv    ← 60枚デッキ（カードIDが1行に1枚）
└── cg/         ← 対戦シミュレータ（同梱が必須）
```

`submissions/improved_lucario_robust_v1/submission.tar.gz` がそのまま提出できる完成品です。

---

## 3. AIの「基本契約」＝ agent(obs) → indexのリスト

AI本体は **`agent(obs_dict)` という関数1つ**。対戦中、シミュレータが「今こういう状況だよ。
この選択肢から選んで」と `obs`（観測）を渡してくるので、**選んだ選択肢の番号（index）のリスト**を返します。

```python
def agent(obs_dict: dict) -> list[int]:
    ...
    return [2]   # 「3番目の選択肢を選ぶ」という意味（0始まり）
```

### obs（観測）の読み方 — 最低限これだけ

| 書き方 | 意味 |
|---|---|
| `obs.select` | 今聞かれている「選択」。**None なら最初のデッキ提出** → 60枚のIDを返す |
| `obs.select.option` | 選べる**選択肢のリスト**。返すのはこの**index** |
| `obs.select.minCount / maxCount` | 返す個数の下限・上限（例：1枚選ぶ＝min1/max1） |
| `obs.select.context` | 今が**どの場面か**（`MAIN`=自分の番、`SWITCH`=入替、`DISCARD`=トラッシュ等） |
| `option.type` | その選択肢の種類（`ATTACK`攻撃 / `PLAY`カード使用 / `ATTACH`エネ付け / `EVOLVE`進化 …） |
| `obs.current.players` | 両プレイヤーの盤面（`active`バトル場 / `bench`ベンチ / `hand`手札 / `prize`サイド …） |

ほとんどのAIは、この `option` を1つずつ**点数化して、一番高いものを選ぶ**だけです。次章で実物を見ます。

---

## 4. main.py の解剖（このサンプルで具体化）

このAIは「勝てるAIの王道パターン」でできています。**3部構成**で捉えると分かりやすいです：

```
┌─────────────────────────────────────────────┐
│ ① 準備      デッキ読込 / カードID定数 / 補助関数  │  ← 53〜144行
├─────────────────────────────────────────────┤
│ ② 方策      LucarioPolicy：全選択肢を点数化→最善 │  ← 146〜580行 (心臓部)
├─────────────────────────────────────────────┤
│ ③ 入口+保険  agent()：方策を呼び、落ちないよう包む │  ← 582〜648行
└─────────────────────────────────────────────┘
```

### ① デッキ読込（53–57行）

`deck.csv` を読み、最初の呼び出し（`obs.select is None`）で**この60枚を返す**＝デッキ提出。

```python
DECK_PATH = "deck.csv"
if not os.path.exists(DECK_PATH):
    DECK_PATH = "/kaggle_simulations/agent/deck.csv"   # 本番はこのパスに置かれる
with open(DECK_PATH, "r", encoding="utf-8") as f:
    my_deck = [int(line) for line in f.read().splitlines() if line.strip()]
```

> **なぜ2パス見るか**：ローカルでは `deck.csv`、Kaggle本番では `/kaggle_simulations/agent/deck.csv`
> に置かれるため、両対応にしている。

### ② カードID定数（20–47行）

カードは数字IDで届くので、**意味の分かる名前**を付けておく（可読性＝バグ防止）。

```python
class C:
    HARIYAMA = 674          # 非exアタッカー（Crustle対策の要）
    RIOLU = 677
    MEGA_LUCARIO_EX = 678   # 主役
    BOSS_ORDERS = 1182      # 相手のベンチを引きずり出す
    ...
```

### ③ 意思決定の心臓：点数化 → 一番高いのを選ぶ（172–182行）

**これが全AI共通の核**。「全選択肢に点数を付け、降順に並べ、上位を返す」。

```python
def choose(self) -> list[int]:
    if self.context == SelectContext.MAIN:
        self._plan_attack()                       # ← ④ ターン頭に攻撃計画

    scores = [self._score_option(o) for o in self.select.option]   # 各選択肢に点数
    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)  # 高い順
    return ranked[: self.select.maxCount]         # 上位を必要数だけ返す
```

点数の付け方は `_score_option`（375行）。選択肢の**種類ごと**に専用の採点へ振り分ける：

```python
def _score_option(self, option) -> float:
    if option.type == OptionType.PLAY:   return self._score_play(option)    # カード使用
    if option.type == OptionType.ATTACH: return self._score_attach(option)  # エネ付け
    if option.type == OptionType.EVOLVE: return self._score_evolve(option)  # 進化
    if option.type == OptionType.ATTACK: ...                                # 攻撃
    ...
```

> **コツ**：点数は「やってほしい順」に大きくするだけ。例えばこのAIは
> 進化(`EVOLVE`)＞エネ付け(`ATTACH`)＞攻撃(`ATTACK`) のように**準備を先・攻撃を最後**に並ぶ点数設計。

### ④ AttackPlan：ターンの頭に「誰でどこを殴るか」を決める（281行〜）

毎ターン最初に「最適なアタッカー×技×標的」を1回計算して `plan` に保存。以降の採点が
**この計画と矛盾しない**ように加点される（＝行動に一貫性が出る）。

### ⑤ 相手読み：Crustle対面を検知して戦法を切替（232・394行）

このコンペ最大のポイント。**Crustle(ID 345) は ex のダメージを無効化**する壁。
そこで「相手がCrustle壁なら、ex攻撃の点数を下げて**非exのハリヤマに迂回**」する。

```python
def _opponent_is_crustle_wall(self) -> bool:
    return self._opponent_has({344, 345})        # 相手にDwebble/Crustleがいるか

# _score_option の攻撃採点より：
if self._opponent_is_crustle_wall() and ... self.opponent.active[0].id == 345 ...:
    return -1                                    # ex攻撃を「選ばない」点数に
```

> これが無いと、壁デッキに延々と無効打点を撃ち続けて負ける。**相手を見て重みを変える**のが
> 強さの分かれ目（“盗める設計”の代表例）。

### ⑥ 堅牢ラッパ（582–648行）＝ 今回の改良点(v1)

元の方策 `agent` を **`_agent_impl` に改名**し、新しい `agent` が**安全網**として包む。
**例外や不正な手＝投了** を防ぐのが目的（1章のミラー審査・レート維持に直結）。

```python
def agent(obs_dict: dict) -> list[int]:
    try:
        obs = to_observation_class(obs_dict)
    except Exception:
        return list(my_deck) if (obs_dict or {}).get("select") is None else [0]
    try:
        return _sanitize(_agent_impl(obs_dict), obs)   # 通常はここ。結果も合法か検査
    except Exception:
        return _legal_fallback(obs)                    # 万一落ちても合法手を返す
```

- `_sanitize`（614行）：返り値が**枚数違反・範囲外・重複**でも合法な形に矯正
- `_legal_fallback`（601行）：最悪でも `minCount〜maxCount` を満たす選択を返す

> **なぜ効くか**：元コードは `try/except` が**0個**で「予期せぬ盤面＝例外＝敗北」だった。
> ミラー審査やKaggle本番の未知の相手で起きうる事故を、**挙動を変えずに**握りつぶせる。
> 実測でもベース相手に 12-8（60%）で**下回らず**、対フィールド68%。

---

## 5. 作って試して出す（開発ループ）

```
1. 強い既存AIをフォーク（ゼロから書かない）
2. 軽い改良を1つ入れる（落ちない化・相手読み・点数調整 など）
3. アプリの「個別AI評価」で 対フィールド勝率 を測る  ← 全員総当たりより速い
4. ベースと比べて良ければ submission.tar.gz にして提出
5. レートの推移（数時間〜1日）を見て、2に戻る
```

### submission.tar.gz の作り方

```sh
cd submissions/improved_lucario_robust_v1
rm -rf _pkg && mkdir _pkg
cp main.py deck.csv _pkg/
cp -r ../../data/sample_submission/cg _pkg/cg
( cd _pkg && tar -czf ../submission.tar.gz main.py deck.csv cg ) && rm -rf _pkg
tar -tzf submission.tar.gz   # 中身確認: main.py / deck.csv / cg/
```

---

## 6. 次の改良の方向性（v2候補）

39体を読んだ結果、**大半が「1手先しか読まない貪欲・デッキ直書き・相手手札を読まない」**。
ここを突く/埋めるのが上積みの近道：

1. **アンチ貪欲**：相手に返しKOされる手を、同価値の安全手があれば避ける
2. **軽い相手読み**：トラッシュ/サイド追跡＋「ボスの指令」を警戒したベンチ保護
3. **前方探索**：エンジンの `search_begin/step` で1手読み（時間予算に注意）

---

## 用語ミニ辞典

| 用語 | 意味 |
|---|---|
| obs（観測） | 今の盤面と「聞かれている選択」の情報のかたまり |
| select / option | 今の選択 / その選択肢リスト（返すのはこのindex） |
| context | 今の場面（MAIN自分の番 / SWITCH入替 / DISCARDトラッシュ …） |
| prize（サイド） | 取ると勝ちに近づくカード。先に規定枚取った方が勝ち |
| active / bench | バトル場 / 控え |
| ex / メガex | 強力だが倒されると相手のサイドが多く取られるポケモン |
| Crustle壁 | exの打点を無効化する耐久デッキ。非ex打点で崩す |
| ミラー審査 | 自分のコピーと対戦して“落ちないか”確認する提出時チェック |

---

## 付録A. `cg.api` リファレンス（提出物の核）

全エージェントが `from cg.api import ...` する**APIの本体**。提供物は大きく3つ：
**(1) 列挙(enum)** ／ **(2) 観測の型（盤面データ構造）** ／ **(3) 関数（変換・カードDB・探索）**。
（出典：`data/sample_submission/cg/api.py`）

### A-1. 列挙（enum）— 「種類」を表す数値の名前

| enum | 用途 | 主な値 |
|---|---|---|
| `OptionType` | 選択肢の種類 | `PLAY`(7) 使用 / `ATTACH`(8) エネ付け / `EVOLVE`(9) 進化 / `ABILITY`(10) 特性 / `RETREAT`(12) 逃げる / `ATTACK`(13) 攻撃 / `END`(14) ターン終了 / `CARD`(3) カード選択 / `ENERGY`(6) / `YES`(1)/`NO`(2)/`NUMBER`(0) |
| `SelectContext` | 今**何を**聞かれているか | `MAIN`(0) 自分の番 / `SWITCH`(3) 入替 / `TO_HAND`(7) サーチ / `DISCARD`(8) トラッシュ / `DAMAGE_COUNTER`(13) / `HEAL`(17) / `EVOLVES_TO`(19) …全60種弱 |
| `AreaType` | カードの**置き場所** | `DECK`1 / `HAND`2 / `DISCARD`3 / `ACTIVE`4 / `BENCH`5 / `PRIZE`6 / `STADIUM`7 |
| `EnergyType` | エネ/タイプ | `GRASS`1 `FIRE`2 `WATER`3 `LIGHTNING`4 `PSYCHIC`5 `FIGHTING`6 `DARKNESS`7 `METAL`8 `COLORLESS`0 … |
| `CardType` | カード種別 | `POKEMON`0 / `ITEM`1 / `TOOL`2 / `SUPPORTER`3 / `STADIUM`4 / `BASIC_ENERGY`5 / `SPECIAL_ENERGY`6 |
| `SpecialConditionType` | 状態異常 | `POISON`/`BURN`/`SLEEP`/`PARALYZE`/`CONFUSE` |

### A-2. 観測の型（`agent` に届く `obs` の中身）

`obs = to_observation_class(obs_dict)` で下の構造になる。**入れ子**で辿る：

```
Observation
├─ select : SelectData | None   ← 「今の選択」。None＝最初のデッキ提出
├─ current: State | None        ← 「今の盤面」。None＝デッキ提出時
└─ logs   : list[Log]           ← 前回からの出来事（ログ）
```

**SelectData（= obs.select）**：返すindexはここの `option`。
| フィールド | 意味 |
|---|---|
| `option: list[Option]` | 選択肢。**返り値はこのindex** |
| `context: SelectContext` | 今の場面（MAIN等） |
| `minCount / maxCount` | 返す個数の下限・上限（maxCountは必ず `len(option)` 以下） |
| `deck: list[Card] | None` | 山札から選ぶ時だけ入る |

**Option（= 各選択肢）**：`type` で種類を見て、必要な属性を読む。
| フィールド | 意味 |
|---|---|
| `type: OptionType` | 選択肢の種類（ATTACK/PLAY/…） |
| `index` | 対象カードの位置 / `area: AreaType` | 対象の置き場所 / `playerIndex` | 誰の |
| `attackId` | 技ID（`ATTACK`時） / `cardId` | カードID / `number` | 数値（NUMBER時） |

**State（= obs.current）**：盤面と手番の情報。
| フィールド | 意味 |
|---|---|
| `players: list[PlayerState]` | 2人分の盤面（自分/相手） |
| `yourIndex` | 自分は0か1か / `turn` | ターン数 / `firstPlayer` | 先攻side(-1=未定) |
| `result` | 勝者index（-1=未決着） / `supporterPlayed`,`stadiumPlayed`,`energyAttached`,`retreated` | 今ターンの使用済みフラグ |

**PlayerState（= players[i]）**：
| フィールド | 意味 |
|---|---|
| `active: list[Pokemon|None]` | バトル場（要素0か1。Noneは裏向き） |
| `bench: list[Pokemon]` | ベンチ / `benchMax` | ベンチ上限 |
| `hand: list[Card]|None` | 手札（**相手の手札はNone＝見えない**） / `handCount` | 手札枚数 |
| `deckCount` | 山札残 / `discard: list[Card]` | トラッシュ / `prize: list[Card|None]` | サイド |
| `poisoned/burned/asleep/paralyzed/confused` | バトル場の状態異常 |

**Pokemon / Card（場のカード）**：
| Pokemon | `id`(カードID) `serial`(個体識別) `hp` `maxHp` `energies: list[EnergyType]` `tools` `preEvolution` `appearThisTurn` |
| Card | `id` `serial` `playerIndex` |

> **重要**：場のカードの `id` は **CardData ID**（=`deck.csv`やDBと同じ番号）。
> 名前やHPなど**スペックはここには無い**ので、`all_card_data()` のDBと突き合わせる（次項）。

### A-3. カードDB（スペックを引く）

```python
all_card_data() -> list[CardData]   # 全カードの仕様
all_attack()    -> list[Attack]     # 全ワザの仕様
```

**CardData**（カード仕様）：
| フィールド | 意味 |
|---|---|
| `cardId` `name` `cardType` `hp` `retreatCost` | 基本情報 |
| `weakness` `resistance` `energyType: EnergyType|None` | 弱点/抵抗/タイプ |
| `basic` `stage1` `stage2` | 進化段階 / `evolvesFrom: str|None` | 進化前名 |
| `ex` `megaEx` `tera` `aceSpec` | レアリティ系フラグ（**KO時のサイド枚数に直結**：ex=2枚, megaEx=3枚） |
| `attacks: list[int]` | 使えるワザID（→ `all_attack()` で詳細） / `skills: list[Skill]` | 特性 |

**Attack**（ワザ仕様）：`attackId` `name` `text` `damage` `energies: list[EnergyType]`（必要エネ）

> サンプルAIが冒頭で `card_table = {c.cardId: c for c in all_card_data()}` を作るのはこのため。
> 「場のPokemon.id → card_table[id] で弱点/exフラグ/技を引く」が定番。

### A-4. 関数まとめ

| 関数 | 用途 |
|---|---|
| `to_observation_class(obs_dict) -> Observation` | dictの観測を**型付きオブジェクトに変換**（最初に必ず呼ぶ） |
| `all_card_data() -> list[CardData]` | カード仕様DB |
| `all_attack() -> list[Attack]` | ワザ仕様DB |
| `search_begin(obs, your_deck, your_prize, opponent_deck, opponent_prize, opponent_hand, opponent_active, ...)` | **前方探索の開始**（1手先のシミュ）。※相手の山/手札/サイドを**自分で予測して渡す**必要がある＝相手読みが前提 |
| `search_step(search_id, select) -> SearchState` | 探索を1手進める / `search_end()`,`search_release(id)` | 後始末 |

> 探索(`search_*`)は上級者向け（v2候補③）。**相手の隠れ情報を推定して入力**する設計なので、
> 「相手読み」を作り込むほど効く。ただしCPU時間に注意。

### A-5. この `main.py` での使われ方（対応表）

| コード（main.py） | 使っている cg.api |
|---|---|
| `obs = to_observation_class(obs_dict)` | `to_observation_class` |
| `self.select.option` を採点 | `Observation.select`(SelectData) / `Option` |
| `option.type == OptionType.ATTACK` 等で分岐 | `OptionType` |
| `self.context == SelectContext.MAIN` | `SelectContext` |
| `card_table = {c.cardId: c for c in all_card_data()}` | `all_card_data` / `CardData` |
| `pokemon.energies` `pokemon.hp` で打点判断 | `Pokemon` |
| `op_data.weakness == EnergyType.FIGHTING` | `CardData.weakness` / `EnergyType` |
| Crustle(345)の `ex` 無効化を回避 | `CardData.ex` の意味（KO時サイド2枚） |
