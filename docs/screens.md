# 画面構成と画面遷移

ポケカ対戦Webアプリの画面・URL・つながりをまとめたドキュメント。
（サーバー: `tools/webapp/server.py` / テンプレート: `tools/webapp/templates/`）

## 画面（ページ）一覧

| URL | テンプレート | 役割 |
|---|---|---|
| `/` | `home.html` | **ホーム**: 使い方ガイド / デッキ作成 / AI登録 / 対戦 / バトルロワイヤル / リプレイ / 個別AI評価 の入口 |
| `/guide` | `guide.html` | **使い方ガイド**: 初めての人向けの操作・流れ・FAQ |
| `/decks` | `decks.html` | **デッキ一覧**: 保存済みデッキを表示（サンプルデッキは非表示）。「＋新しいデッキを作成」→ `/builder` |
| `/builder` | `builder.html` | **デッキ作成**: カードから60枚を組んで保存 |
| `/agents` | `agents.html` | **AIエージェント登録**: 登録済み一覧＋新規登録(.py/.ipynb)・削除 |
| `/play` | `play.html` | **対戦モード選択**: あなた vs AI / 人 vs 人 / AI vs AI / バトルロワイヤル |
| `/setup?mode=` | `setup.html` | **対戦設定**（モード別）: デッキ・AIを選んで開始 |
| `/battle?gid=` | `battle.html` | **対戦画面**: プレイマット型UI（`&royale=N` でロワイヤル連携） |
| `/royale` | `royale.html` | **バトルロワイヤル**: 勝率上位5体に5位→1位で挑戦。殿堂入りリスト・デッキ選択 |
| `/join?gid=` | `join.html` | **オンライン参加**: 招待URLからゲストがデッキを選んで参加 |
| `/replay` | `replay.html` | **対戦ログ（リプレイ）**: 保存済みの対戦を盤面で再生 |
| `/evaluate` | `evaluate.html` | **個別AI評価**: 1体を選び他の全AIと対戦して対フィールド勝率を測定 |
| `/tournament` | `tournament.html` | **総当たり戦**: 全AIの総当たり（※ホームでは一時停止表示の場合あり） |

共通レイアウトは `base.html`（ヘッダー・CSS・モーダル・通信ユーティリティ）を各ページが継承。

## 画面遷移図

```
/  ホーム
├─ 使い方ガイド → /guide
├─ デッキ作成 → /decks ──「＋新しいデッキを作成」→ /builder ──保存──→ /decks
├─ AIエージェント登録 → /agents （登録すると対戦設定の「登録AIから選ぶ」に出る）
├─ 対戦 → /play
│         ├─ あなた vs AI    → /setup?mode=human ─┐
│         ├─ 人 vs 人         → /setup?mode=pvp  ─┤ POST /api/new
│         ├─ AI vs AI        → /setup?mode=ai   ─┘
│         └─ バトルロワイヤル → /royale
│                                                │
│             human/ai → /battle?gid=            │
│             pvp      → /battle?gid=（待機）→ 招待URL /join?gid= を共有
│                                 └ ゲスト参加(/api/join) → 両者 /battle?gid=
├─ バトルロワイヤル → /royale ──挑戦──→ /battle?gid=&royale=N
│                         （勝利→次の相手 / 全制覇→殿堂入り登録）
├─ 対戦ログ（リプレイ）→ /replay
└─ 個別AI評価 → /evaluate
```

## 各画面の詳細

### ホーム `/`（home.html）
カードタイル: 📖使い方ガイド / 🃏デッキ作成 / 🤖AIエージェント登録 / ⚔️対戦 / 👑バトルロワイヤル / 🎬対戦ログ（リプレイ）/ 📊個別AI評価。

### 使い方ガイド `/guide`（guide.html）
- 初めての人向け。できること、はじめての対戦までの流れ、対戦画面の操作、バトルロワイヤル、対戦ログの見かた、個別AI評価、FAQ。

### デッキ一覧 `/decks`（decks.html）
- 「あなたの保存済みデッキ」をカード画像付きで一覧表示（サンプルデッキはUIから非表示）。
- 右上「＋ 新しいデッキを作成」→ `/builder`。

### デッキ作成 `/builder`（builder.html）
- カード一覧（ID 1〜1267、検索・絞り込み）→ クリックで詳細→追加。60枚チェック（4枚制限・ACE SPEC・たね必須）。
- 保存 → `POST /api/user_decks` → `/decks` に戻る。

### AIエージェント登録 `/agents`（agents.html）
- 登録フォーム（表示名＋ .py/.ipynb）→ `POST /api/agents` でサーバー保存（`data/user_agents/<id>/`）。
- 登録済み一覧（名前・デッキ取得元）＋削除。`POKECA_ALLOW_AGENT_UPLOAD=0` のとき登録は無効。

### 対戦モード選択 `/play`（play.html）
- あなた vs AI / 人 vs 人（オンライン）/ AI vs AI を選ぶ → `/setup?mode=...`。

### 対戦設定 `/setup`（setup.html）
- **human**: あなたのデッキ ＋ 相手AI（ルール / 登録AI / アップロード）＋相手デッキ。
- **ai**: プレイヤー1・プレイヤー2 をそれぞれ（ルール / 登録AI / アップロード）＋デッキで設定。
- **pvp**: あなたのデッキのみ → 「部屋を作成」。
- 送信 → `POST /api/new` → `/battle?gid=`（pvp はトークンを localStorage に保存）。

### 対戦画面 `/battle`（battle.html）
- cabt-viewer 風プレイマット（相手ベンチ↑／中央アクティブ対面／自分ベンチ↓、左右にサイド・トラッシュ、ポケボール透かし）。右に対戦ログ。
- 演出（人 vs AI）: コイントス、攻撃/ダメージ（「Nダメージ！」＋HP漸減）、きぜつ、効果/特性/入れ替えのトースト通知。カードはクリックで詳細表示、面伏せは裏面画像で選択。
- 対戦ログの工夫: 通常ドローと「○○の効果で引いた」を区別、マリガン（たね無し引き直し）を明示、連続ドローはまとめて表示。
- 対戦終了後は「もう一度対戦しますか？」（ロワイヤル時は専用導線）。
- **人 vs AI**: 自分の手番に操作。相手AIは自動進行。
- **AI vs AI**: 観戦コントロール（▶再生/⏸一時停止/速度）。両者の手札を公開。
- **人 vs 人**: トークンで参加者識別。自分の手札のみ表示し、ポーリングで相手の手を反映。待機中は招待URLを表示。

### バトルロワイヤル `/royale`（royale.html）
- 登録AIの**勝率上位5体**に **5位 → 1位** の順で挑戦するソロモード。
- 開始画面に **殿堂入りリスト**・**挑戦ラダー**（クリア/挑戦中/ロック）・**デッキ選択**を表示。
- 「挑戦開始」→ `POST /api/new`（相手＝そのランクのAI）→ `/battle?gid=&royale=N`。
- 対戦画面は勝敗に応じて専用導線（勝利→次の相手へ `?cleared=N` / 敗北→再挑戦）。
- 全制覇（1位撃破）で任意の名前を **殿堂入り** 登録（`POST /api/royale/hof`）。
- 相手ラダーは総当たり結果 `data/logs/royale/ranking.json` を元に決定（現存AIのTOP5）。殿堂入りは `data/logs/royale/hof.json`。
- 進行状況はブラウザの localStorage に保存（端末ごと）。

### 対戦画面のバトルロワイヤル連携 `/battle?gid=&royale=N`
- `royale=N`（第N戦）が付くと、対戦終了バナーが「バトルロワイヤルへ戻る（次の相手へ／再挑戦）」になる。

### オンライン参加 `/join`（join.html）
- 招待URL `/join?gid=XXX` → デッキ選択 → `POST /api/join` → トークン保存 → `/battle?gid=`。

## API エンドポイント

| メソッド/パス | 用途 |
|---|---|
| `POST /api/new` | 対戦開始（human/ai は即開始、pvp は部屋作成して待機）。`gid`（pvpは`token`も）を返す |
| `POST /api/join` | pvp: ゲスト参加して対戦開始 |
| `GET /api/state?gid=&token=` | 盤面・選択肢・ログ（viewer視点。自分の手札のみ。`ver`/`yourTurn`/`status`） |
| `POST /api/select` | 自分の手番の選択を送信（pvpは相手の手番だと409） |
| `POST /api/step` | AI vs AI を1手進める |
| `GET /api/config` | 保存済みデッキ＋登録AI一覧（サンプルデッキは空。デッキ選択不可AIはcards空で除外） |
| `GET/POST /api/agents`, `DELETE /api/agents/<id>` | 登録AIの一覧・登録・削除 |
| `POST /api/evaluate`, `GET /api/evaluate/<job_id>` | 個別AI評価の開始・進捗/結果 |
| `GET /api/royale/ranking` | バトルロワイヤルの相手ラダー（現存TOP5） |
| `GET/POST /api/royale/hof` | 殿堂入りリストの取得・登録 |
| `GET /api/cards`, `/api/card/<id>` | デッキ作成用カード情報 |
| `POST /api/user_decks`, `GET /api/user_decks/<id>`, `DELETE /api/user_decks/<id>` | ユーザーデッキ保存・取得・削除 |
| `GET /card_img/<id>` | カード画像 |
| `GET /card_back` | カード裏面画像（サイド/相手手札/山札の面伏せ表示用） |

## 状態管理（複数同時対戦）

- ネイティブ `libcg` は `battle_ptr` で対戦を区別できるため、サーバーは `gid` ごとに対戦を保持（`GAMES`、最大16）。各リクエストで対象対戦の `battle_ptr` に切り替えてから cg を呼ぶ（Flaskはシングルスレッドで競合なし）。
- `mode`: `human` / `ai` / `pvp`。pvp はプレイヤー枠(0/1)を匿名トークンで識別し、`status: waiting→playing`、`ver`（手の通し番号）でポーリング重複を防止。
- バトルロワイヤルのランキング/殿堂入りは `data/logs/royale/`（マウント済みで再ビルド後も残る）に保存。挑戦の進行状況はブラウザの localStorage。

## 公開（オンライン）時の注意

- カスタムAI(.py/.ipynb)は `exec` 実行のため、公開サーバーでは `POKECA_ALLOW_AGENT_UPLOAD=0` で登録/アップロードを無効化すること（RCE対策。本格運用はサンドボックス化が必要）。
- 対戦状態はメモリ上のみ（再起動で消える）。
