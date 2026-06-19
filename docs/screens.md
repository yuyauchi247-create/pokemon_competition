# 画面構成と画面遷移

ポケカ対戦Webアプリの画面・URL・つながりをまとめたドキュメント。
（サーバー: `tools/webapp/server.py` / テンプレート: `tools/webapp/templates/`）

## 画面（ページ）一覧

| URL | テンプレート | 役割 |
|---|---|---|
| `/` | `home.html` | **ホーム**: 「デッキ作成 / AIエージェント登録 / 対戦」の3入口 |
| `/decks` | `decks.html` | **デッキ一覧**: 保存済み＋サンプルを表示。「＋新しいデッキを作成」→ `/builder` |
| `/builder` | `builder.html` | **デッキ作成**: カードから60枚を組んで保存 |
| `/agents` | `agents.html` | **AIエージェント登録**: 登録済み一覧＋新規登録(.py/.ipynb)・削除 |
| `/play` | `play.html` | **対戦モード選択**: あなた vs AI / 人 vs 人 / AI vs AI |
| `/setup?mode=` | `setup.html` | **対戦設定**（モード別）: デッキ・AIを選んで開始 |
| `/battle?gid=` | `battle.html` | **対戦画面**: プレイマット型UI |
| `/join?gid=` | `join.html` | **オンライン参加**: 招待URLからゲストがデッキを選んで参加 |

共通レイアウトは `base.html`（ヘッダー・CSS・モーダル・通信ユーティリティ）を各ページが継承。

## 画面遷移図

```
/  ホーム
├─ デッキ作成 → /decks ──「＋新しいデッキを作成」→ /builder ──保存──→ /decks
├─ AIエージェント登録 → /agents （登録すると対戦設定の「登録Aから選ぶ」に出る）
└─ 対戦 → /play
          ├─ あなた vs AI → /setup?mode=human ─┐
          ├─ 人 vs 人      → /setup?mode=pvp  ─┤ POST /api/new
          └─ AI vs AI     → /setup?mode=ai   ─┘
                                                │
              human/ai → /battle?gid=          │
              pvp      → /battle?gid=（待機）→ 招待URL /join?gid= を共有
                                  └ ゲスト参加(/api/join) → 両者 /battle?gid=
```

## 各画面の詳細

### ホーム `/`（home.html）
3つの大きなカード: 🃏デッキ作成 / 🤖AIエージェント登録 / ⚔️対戦。

### デッキ一覧 `/decks`（decks.html）
- 「あなたの保存済みデッキ」と「サンプルデッキ」をカード画像付きで一覧表示。
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
- **人 vs AI**: 自分の手番に操作。相手AIは自動進行。
- **AI vs AI**: 観戦コントロール（▶再生/⏸一時停止/速度）。両者の手札を公開。
- **人 vs 人**: トークンで参加者識別。自分の手札のみ表示し、ポーリングで相手の手を反映。待機中は招待URLを表示。

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
| `GET /api/config` | サンプル/保存済みデッキ＋登録AI一覧 |
| `GET/POST /api/agents`, `DELETE /api/agents/<id>` | 登録AIの一覧・登録・削除 |
| `GET /api/cards`, `/api/card/<id>` | デッキ作成用カード情報 |
| `POST /api/user_decks`, `GET /api/user_decks/<id>` | ユーザーデッキ保存・取得 |
| `GET /card_img/<id>` | カード画像 |

## 状態管理（複数同時対戦）

- ネイティブ `libcg` は `battle_ptr` で対戦を区別できるため、サーバーは `gid` ごとに対戦を保持（`GAMES`、最大16）。各リクエストで対象対戦の `battle_ptr` に切り替えてから cg を呼ぶ（Flaskはシングルスレッドで競合なし）。
- `mode`: `human` / `ai` / `pvp`。pvp はプレイヤー枠(0/1)を匿名トークンで識別し、`status: waiting→playing`、`ver`（手の通し番号）でポーリング重複を防止。

## 公開（オンライン）時の注意

- カスタムAI(.py/.ipynb)は `exec` 実行のため、公開サーバーでは `POKECA_ALLOW_AGENT_UPLOAD=0` で登録/アップロードを無効化すること（RCE対策。本格運用はサンドボックス化が必要）。
- 対戦状態はメモリ上のみ（再起動で消える）。
