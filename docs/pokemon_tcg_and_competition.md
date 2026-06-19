# ポケモンカード & PTCG AI Battle Challenge 理解ノート

このドキュメントは、対戦シミュレータと対戦アプリを正しく扱うための知識をまとめたものです。

出典:
- Kaggleコンペ概要 / Data / Discussion（公式ホスト投稿）
- cabt エンジン公式APIドキュメント（https://matsuoinstitute.github.io/cabt/）
- 配布シミュレータのソース `data/sample_submission/cg/api.py`
- ポケモン公式ルール（The Pokémon Company）

---

## 1. コンペ概要（PTCG AI Battle Challenge - Simulation）

- 目的: ポケモンカードゲーム(TCG)をプレイするAI対戦エージェントを構築する。
- 種別: Featured Simulation Competition（AI vs AI のラダー戦）。
- 本コンペは「Simulation」トラック。別に「Hackathon」トラックがあり、賞金はHackathon側。
  Simulation単体は Knowledge（メダル・ポイント）扱い。
- ポイント: ルールベースだけでは上位は難しく、不確実性（相手の手札・引き・コイン）の下での
  先読み・適応・最適意思決定が問われる。

### 提出形式
- `.tar.gz` バンドル。トップ階層（ネストさせない）に `main.py` と `deck.csv` を置く。
  - 作成例: `tar -czvf submission.tar.gz *`
- `main.py` は `agent(obs_dict: dict) -> list[int]` を実装する。
- 1日あたり最大5エージェント提出可。最終評価は最新2提出を使用。

### 評価（ランキング）
- 各提出はスキルレーティング（ガウス分布 N(μ, σ²)）でモデル化。μ=推定スキル、σ=不確実性。
- 初期 μ₀ = 600。提出直後に「自分自身とのValidation Episode」を行い、動けばプールに参加。
- Episode（対戦）の勝敗で μ が上下、引き分けは両者の μ を平均方向へ寄せる。
  σ は情報が得られるほど縮小。勝敗の点差はレートに影響しない（勝ち負けのみ）。
- リーダーボードは自分の最高スコアの提出のみ表示。

### スケジュール（2026年）
- 6/16 開始 / 8/9 参加・チーム締切 / 8/16 最終提出締切 / 〜8/31頃 集計→確定。

### デッキ構築
- 参加者は Data タブのカードプールから自分でデッキを組める（公式Q&Aで明言）。
- デッキは60枚。

---

## 2. 配布データ（Data タブ / data/reference/）

カードのメタデータと参照資料。英語版(EN)と日本語版(JP)があり、内容は言語以外同一。

| ファイル | 内容 |
|---|---|
| `Card_ID_List_EN.pdf` / `_JP.pdf` | 全カードの一覧（ID・名前・拡張・コレクション番号・カード画像） |
| `EN_Card_Data.csv` / `JP_Card_Data.csv` | カードメタデータ（構造化） |

### CSVスキーマ（17列）
`カードID, カード名, エキスパンションマーク, コレクション番号,
進化段階/種類, ルール, カテゴリ, 進化前, HP, タイプ, 弱点, 抵抗力, にげる,
ワザ名, コスト, ダメージ, 効果の説明`

- 1枚のカードでワザが複数ある場合、ワザ行が複数行に分かれる（同じカードIDで繰り返し）。
- `n/a` や空欄は「該当なし」。

### カード種別の分布（JP_Card_Data.csv、2026-06時点）
- ポケモン/たね: 595、ポケモン/1進化: 345、ポケモン/2進化: 116
- グッズ: 77、サポート: 61、ポケモンのどうぐ: 27、スタジアム: 26
- 特殊エネルギー: 12、基本エネルギー: 8

### 基本エネルギーのカードID（重要）
| ID | エネルギー |
|----|------|
| 1 | 草 |
| 2 | 炎 |
| 3 | 水 |
| 4 | 雷 |
| 5 | 超 |
| 6 | 闘 |
| 7 | 悪 |
| 8 | 鋼 |

---

## 3. ポケモンカードゲームの基本ルール

### 勝利条件（いずれか）
1. 自分のサイド（プライズ）を全部取り切る。
2. 相手の場（バトル場＋ベンチ）のポケモンが0匹になる。
3. 相手の番の最初に、相手の山札が0枚（ドローできない）。

### 場の構成
- バトル場（Active）: 1匹。実際に戦うポケモン。
- ベンチ（Bench）: 最大5匹。
- 山札（Deck）/ 手札（Hand）/ トラッシュ（Discard）/ サイド（Prize, 通常6枚）/ スタジアム。

### ゲームの流れ
1. 各自60枚デッキ、よく切る。
2. 手札7枚を引く。たねポケモンがなければマリガン（引き直し）。
3. たねポケモン1匹をバトル場、任意でベンチに展開。
4. サイドを6枚伏せる。
5. 先攻・後攻を決める。

### 自分の番でできること
- 手札からたねポケモンをベンチに出す（何枚でも、空きがある限り）。
- 進化させる（たね→1進化→2進化。出したターンや進化したてはさらに進化不可。最初の番は不可）。
- エネルギーを手札から1ターンに1枚つける。
- トレーナーズを使う:
  - グッズ: 1ターンに何枚でも。
  - サポート: 1ターンに1枚だけ。
  - スタジアム: 1ターンに1枚。場に1枚だけ存在。
  - ポケモンのどうぐ: ポケモンに付ける。
- 特性（Ability）を使う。
- にげる（1ターン1回、必要数のエネルギーをトラッシュしてベンチと入れ替え）。
- ワザを使う（必要エネルギーが揃っているもの）→ 使うと番が終わる。

### ダメージと弱点・抵抗力
- ワザのダメージをHPから引く。HPが0以上のダメージ＝きぜつ。
- 弱点: そのタイプから受けるダメージが増える（多くは×2）。
- 抵抗力: 受けるダメージが減る（多くは-30）。
- きぜつ → 相手がサイドを取る。ポケモンexは2枚、メガシンカex(megaEx)は3枚取られる。

### 特殊状態（Special Conditions）
- どく(POISON)・やけど(BURN)・ねむり(SLEEP)・まひ(PARALYZE)・こんらん(CONFUSE)。
- バトル場のポケモンのみ。にげる/入れ替え/進化で回復するものもある。

---

## 4. シミュレータ（cabt エンジン）の仕組み

バトルは cabt Engine（kaggle-environments 用のTCGシミュレータ）上で進行。
**エンジンは常に「合法手のみ」を提示する。** 反則手は選べない。

### エージェントの基本動作
毎ターン、エージェントは Observation（`obs_dict`）を受け取り、
選んだオプションの index を `list[int]` で返す。

```python
def agent(obs_dict: dict) -> list[int]:
    # obs_dict["select"] が None → 初期デッキ選択フェーズ。60枚のカードIDを返す
    # それ以外 → obs["select"]["option"] の中から選んだ index を返す
    return [...]
```

- 返すリストの長さは `select.minCount` 以上 `select.maxCount` 以下。
- index は `0 <= i < len(option)`、重複不可。

### Observation の構造（api.py の dataclass）
- `Observation.logs`: 前回の選択以降に起きたイベント列（`Log`の配列）。
- `Observation.current`: 現在の盤面（`State`）。初期デッキ選択時は None。
- `Observation.select`: 選択肢情報（`SelectData`）。初期デッキ選択時は None。

`State`（current）の主なフィールド:
- `turn`: ターン数（1=先攻の最初の番、0=先攻の番の前）。
- `yourIndex`: 選択しているプレイヤー（0 or 1）。
- `firstPlayer`: 先攻プレイヤー。未定なら -1。
- `supporterPlayed / stadiumPlayed / energyAttached / retreated`: そのターンの使用済みフラグ。
- `result`: 勝者index。未決着は -1。
- `players[2]`: 各プレイヤーの `PlayerState`。
- `stadium`: スタジアムカード（0 or 1）。

`PlayerState`:
- `active`: バトル場ポケモン（0 or 1要素。伏せ中は None）。
- `bench`: ベンチ（最大5）。
- `hand`: 手札（自分のみ中身が見える。相手は handCount のみ）。
- `prize`: サイド（伏せは None。先頭=下、末尾=上）。
- `deckCount` / `discard` / `handCount` / `benchMax`。
- `poisoned / burned / asleep / paralyzed / confused`: 特殊状態フラグ。

### SelectData / Option
- `SelectData.context`（`SelectContext`）: 何を選んでいるか。
  例: 0=MAIN（行動選択）、1=SETUP_ACTIVE_POKEMON、2=SETUP_BENCH_POKEMON、
  3=SWITCH、35=ATTACK、37=EVOLVE、41=IS_FIRST（先攻？）、42=MULLIGAN（引き直す？）。
- `Option.type`（`OptionType`）: 選択肢の種類。
  0=NUMBER, 1=YES, 2=NO, 3=CARD, 4=TOOL_CARD, 5=ENERGY_CARD, 6=ENERGY,
  7=PLAY(手札から出す), 8=ATTACH(つける), 9=EVOLVE(進化), 10=ABILITY(特性),
  11=DISCARD, 12=RETREAT(にげる), 13=ATTACK(ワザ), 14=END(ターン終了),
  15=SKILL(効果の順番), 16=SPECIAL_CONDITION。

### Enum 早見
- `AreaType`: 1=DECK, 2=HAND, 3=DISCARD, 4=ACTIVE, 5=BENCH, 6=PRIZE,
  7=STADIUM, 8=ENERGY, 9=TOOL, 10=PRE_EVOLUTION, 11=PLAYER, 12=LOOKING。
- `EnergyType`: 0=COLORLESS, 1=GRASS, 2=FIRE, 3=WATER, 4=LIGHTNING,
  5=PSYCHIC, 6=FIGHTING, 7=DARKNESS, 8=METAL, 9=DRAGON, 10=RAINBOW, 11=TEAM_ROCKET。
- `CardType`: 0=POKEMON, 1=ITEM(グッズ), 2=TOOL(どうぐ), 3=SUPPORTER,
  4=STADIUM, 5=BASIC_ENERGY, 6=SPECIAL_ENERGY。
- `CardData` の重要フラグ: `basic/stage1/stage2`、`ex`（exは負けるとサイド2枚）、
  `megaEx`（メガシンカexは3枚）、`tera`（テラスはベンチにいる間ワザのダメージを受けない）、
  `aceSpec`（ACE SPECはデッキに1枚まで）。

### Game API（game.py）
- `battle_start(deck0, deck1)` → `(Observation | None, StartData)`。失敗時 obs=None。
- `battle_select(select_list)` → 新しい Observation（1ステップ進める）。
- `battle_finish()` → 対戦終了（メモリ解放）。
- `visualize_data()` → 盤面を人間可読の文字列に（デバッグ用）。

### Sim API（api.py / sim.py）
- `all_card_data()` → 全カードの `CardData`（名前・HP・タイプ・ワザID 等）。
- `all_attack()` → 全ワザの `Attack`（名前・ダメージ・必要エネルギー）。
- 探索系: `search_begin(...)` / `search_step(...)` / `search_end()` / `search_release(...)`
  （状態遷移の先読みシミュレーションに使う）。

---

## 5. 公式ルールとシミュレータの差分（公式ホスト投稿の要約）

**重要: 本コンペでは「シミュレータの挙動が正」として扱われる。**

1. 一部のワザは、公式では「宣言はできるが効果が解決できずターン終了」になる場面でも、
   シミュレータでは最初から選択不可になることがある。例:
   - デッキからベンチにたねを出す効果だが、ベンチに空きがない。
   - カードを引く効果だが、山札が0枚。
   - 相手の手札に干渉する効果だが、相手の手札が0枚。
   → 最終結果は同じでゲームへの影響は軽微、との見解。

2. メガジガルデex のワザ「ゼロをなくすもの（Nullifying Zero）」:
   公式ではダメージを割り当てる対象順を選べるが、シミュレータでは選べず、
   コインは左から右へ自動。きぜつ処理は同時なので競技上は影響なし、との見解。

3. 両者のポケモンが同時にきぜつしたときのサイドを取る順番が公式と異なる。
   - シミュレータ: 次の番のプレイヤーが選んで取る→相手が選んで取る、を順次。
   - 競技では両者が取り切ると引き分け扱いのため、勝敗に影響なし、との見解。

補足（Q&A）:
- エースバーン(カードID 666)は特性により対戦準備ターンに手札からバトル場に出せる
  （`select` の選択肢に含まれるので、適切な index を返せば出せる）。

---

## 6. このプロジェクトでの対応関係

- `tools/sim_env.py`: cg のimportパス解決＋カード名/詳細/盤面/ログ整形の共通処理。
  - `JP_Card_Data.csv` から日本語名・ワザ・弱点等を補完。
- `tools/webapp/server.py`: Flask。`battle_start/select/finish` を呼んで人 vs AI を進行。
- `tools/webapp/selection.py`: デッキCSVの検証・カスタムAI読み込み（cg非依存でテスト可能）。
- `data/sample_submission/`: 配布シミュレータ本体（libcg.so = Linux x86-64）。
- `data/reference/`: コンペ公式の生データ（PDF・EN/JP CSV）。Gitには載せない（再取得可能）。

参考リンク:
- コンペ: https://www.kaggle.com/competitions/pokemon-tcg-ai-battle
- APIドキュメント: https://matsuoinstitute.github.io/cabt/
- 差分の告知: https://www.kaggle.com/competitions/pokemon-tcg-ai-battle/discussion/708586
