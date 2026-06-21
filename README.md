# ポケカAIコンペ 対戦アプリ

このリポジトリは、Kaggle コンペ「The Pokémon Company - PTCG AI Battle Challenge」で配布されているシミュレータを使い、人間がブラウザ上でAIエージェントと対戦できるようにするための開発・検証環境です。

目的は、Kaggle に提出する `main.py` 形式のAIエージェントを、対戦相手としてアップロードして遊べるポケモンカードアプリを再現することです。

## 動作環境（重要）

このアプリの対戦シミュレータ本体は **Linux x86-64 専用** のネイティブライブラリ
（`data/sample_submission/cg/libcg.so`）に依存します。Linux x86-64 上で動かしてください。

- OS: Linux x86-64
- 必要ランタイム: glibc 2.14 以上 / libstdc++ (GLIBCXX 3.4.29 以上, GCC 9 以降相当)
  - Ubuntu 20.04 以降など、近年のディストリビューションであれば満たします。
- Python: 3.12 以上
- パッケージ管理: uv

補足:

- macOS / Windows や arm64 ホストでは、配布 `.so` を**直接**は起動できません
  （Linux x86-64 用のため）。ただし下記「起動方法 A. Docker」を使えば、
  amd64 エミュレーション経由でこれらのホストでも動作します（Apple Silicon Mac で確認済み）。
- コード編集や `tests/` の単体テストはどのOSでも可能です。

## コンペ概要

対象コンペ:

https://www.kaggle.com/competitions/pokemon-tcg-ai-battle

このコンペでは、提出用AIは基本的に `data/sample_submission/main.py` のような形式です。

```python
def agent(obs_dict: dict) -> list[int]:
    ...
```

- 初期デッキ選択時は `obs.select == None` になり、AIは60枚のカードIDリストを返します。
- 対戦中は `obs.select.option` に合法手の候補が入り、AIは選択する option index の `list[int]` を返します。
- 返すリストの長さは `obs.select.minCount` 以上、`obs.select.maxCount` 以下である必要があります。
- index は `0 <= index < len(obs.select.option)` の範囲で、重複不可です。

このリポジトリのWebアプリは、その提出用AIを「対戦相手」として読み込み、人間がブラウザから対戦できるようにします。

## アプリ構成

主なファイル:

- `tools/webapp/server.py`
  - Flask製のWebサーバーです。
  - 配布シミュレータ `data/sample_submission/cg` を呼び出して対戦を進行します。
  - 人間の入力を `/api/select` で受け取り、AI側の手番を自動で進めます。

- `tools/webapp/templates/`
  - ブラウザUI（各画面のテンプレート）です。共通レイアウト `base.html` を各ページ
    （`home.html` / `play.html` / `setup.html` / `battle.html` / `royale.html` / `decks.html`
    / `builder.html` / `agents.html` / `replay.html` / `evaluate.html` / `guide.html` ほか）が継承します。
  - 画面・URL・遷移の詳細は `docs/screens.md` を参照してください。

- `tools/webapp/selection.py`
  - デッキCSVの読み込み・検証、カスタムAI `main.py` の読み込みなど、対戦開始前の選択処理をまとめたヘルパーです。
  - 配布シミュレータ(cg)を import しないため、どのOSでも単体テストできます。

- `tools/sim_env.py`
  - 配布シミュレータ `cg` の import パス解決、カード名・カード詳細・選択肢表示などの共通処理です。

- `tools/play_vs_ai.py`
  - ブラウザを使わずターミナルで人 vs AI 対戦を試す最小CLIです（動作確認用）。

- `data/sample_submission/main.py`
  - Kaggle提出AIのサンプルです。

- `data/sample_submission/deck.csv`
  - 配布サンプルデッキ（60枚）。現在はUIの選択肢からは外しており、内部フォールバック用途のみです。

- `data/sample_submission/cg/`
  - 配布シミュレータ本体（`libcg.so` ほか）。

- `data/JP_Card_Data.csv`
  - 日本語カード名・カード詳細データ。

- `data/card_images/` と `data/card_image_map.json`
  - UI 表示用のカード画像と、カードID→画像ファイル名の対応表。

## 実装済みの画面

ホーム `/` から各機能へ進みます（詳細・遷移図は `docs/screens.md`）。

- **使い方ガイド** `/guide` … 初めての人向けの操作・流れ・FAQ。
- **デッキ作成** `/decks` → `/builder` … 保存済みデッキの一覧と、カードから60枚を組んで保存。
- **AIエージェント登録** `/agents` … 対戦/評価に使うAI（`.py` / `.ipynb`）を登録・管理。
- **対戦** `/play` → `/setup?mode=` … あなた vs AI / 人 vs 人（オンライン）/ AI vs AI（観戦）。相手はルールベースAI・登録AI・アップロードAIから選択。
- **バトルロワイヤル** `/royale` … 勝率上位5体に 5位→1位 の順で挑戦。全制覇で殿堂入り。
- **対戦画面** `/battle?gid=` … プレイマット型UI。手札・自分/相手のバトル場とベンチ、カードクリックで詳細、合法手の選択、対戦ログ（通常/効果ドローの区別・マリガン表示など）、各種アニメーション。
- **対戦ログ（リプレイ）** `/replay` … 保存済みの対戦を盤面で再生。
- **個別AI評価** `/evaluate` … 1体を選び他の全AIと対戦して対フィールド勝率を測定。

## セットアップ

リポジトリのルート（Linux x86-64 環境）で実行します。

```bash
uv sync
```

依存関係（`pyproject.toml`）:

```toml
flask>=3.1.3
kaggle>=1.6
```

`kaggle` はコンペ配布ファイルを取得・確認するために含めています。

## 起動方法

### A. Docker（推奨。macOS/arm64 など x86-64 以外のホストでも動く）

このアプリの本体ライブラリは Linux x86-64 専用ですが、Docker を使えば
amd64 エミュレーション経由でどのホストでも動かせます（Apple Silicon の Mac でも動作確認済み）。

ビルドして起動:

```bash
docker compose up -d --build
```

ブラウザで開きます（ホスト側ポートは 8000 に割り当てています）。

```text
http://127.0.0.1:8000
```

ログ確認・停止:

```bash
docker compose logs -f      # ログ追従
docker compose down         # 停止・後片付け
```

ポートを変えたい場合は `compose.yaml` の `ports`（`"8000:5000"` の左側）を編集します。

補足:

- `Dockerfile` はベースに `python:3.12-slim`（glibc 2.36 / GCC 12 系）を使い、
  libcg.so の要件（glibc 2.14+ / GLIBCXX 3.4.29+）を満たします。
- 実行時依存は flask のみインストールします（kaggle/pymupdf は実対戦に不要）。
- `--platform linux/amd64` を指定しているため、arm64 ホストでは Docker が
  自動でエミュレーション実行します（その分やや低速になります）。

### B. Linux x86-64 ホストで直接（uv）

Linux x86-64 環境では Docker なしで直接動かせます。

```bash
uv sync
uv run python tools/webapp/server.py
```

ブラウザで開きます（既定ポートは 8000）。

```text
http://127.0.0.1:8000
```

ポートを変えたい場合:

```bash
POKECA_WEBAPP_PORT=5001 uv run python tools/webapp/server.py
```

外部からアクセスできるようにホストを変えたい場合:

```bash
POKECA_WEBAPP_HOST=0.0.0.0 uv run python tools/webapp/server.py
```

## 使い方

1. サーバーを起動します。

   ```bash
   uv run python tools/webapp/server.py
   ```

2. ブラウザで `http://127.0.0.1:8000` を開きます（Docker・直接起動とも既定 8000）。
3. ホームから「対戦」→「あなた vs AI」を選びます（初めての方は「使い方ガイド」へ）。
4. 自分のデッキを選びます（保存済みデッキ、または登録AIのデッキ。新規作成は「デッキ作成」から）。
5. 相手AIを選びます（ルールベースAI / 登録AI / アップロードAI）。
6. `この設定で対戦開始` を押します。
7. 対戦画面で、自分の手番にカードや選択肢を操作します。途中でブラウザを閉じても、同じURLを開けば続きから再開できます（サーバー起動中）。

## カスタムAIの形式

アップロードする `main.py` は、少なくとも以下のように `agent` 関数を持つ必要があります。

```python
import random
from cg.api import Observation, to_observation_class


def agent(obs_dict: dict) -> list[int]:
    obs: Observation = to_observation_class(obs_dict)

    if obs.select is None:
        # 初期デッキ選択時は60枚のカードIDを返す
        return [1] * 60

    # 対戦中は合法手の option index を返す
    return random.sample(
        list(range(len(obs.select.option))),
        obs.select.maxCount,
    )
```

実際には `data/sample_submission/main.py` を参考にしてください。

注意:
サンプルの `main.py` は `deck.csv` を同じディレクトリから読む作りです。このWebアプリでは、アップロードしたAIを一時ディレクトリに保存し、サンプル `deck.csv` も同じ場所にコピーして読み込めるようにしています。

## デッキCSVの形式

基本形式は `data/sample_submission/deck.csv` と同じです。

```text
カードID
カードID
カードID
...
```

条件:

- 60枚ちょうど
- 各値は整数のカードID
- 空行・空セルは無視
- UTF-8 / UTF-8 BOM付きに対応
- カンマ区切りのCSVセルも読み取れます

## 動作確認コマンド

単体テスト（どのOSでも可能）:

```bash
uv run python -m unittest tests.test_webapp_selection -v
```

構文チェック:

```bash
uv run python -m py_compile tools/webapp/selection.py tools/webapp/server.py tools/sim_env.py
```

ターミナルでの対戦確認（Linux x86-64 のみ）:

```bash
uv run python tools/play_vs_ai.py          # 人 vs ランダムAI
uv run python tools/play_vs_ai.py --demo   # AI vs AI（入力不要）
```

Kaggle CLI 確認（任意。配布ファイルの取得・確認用）:

```bash
uv run kaggle --version
uv run kaggle competitions files -c pokemon-tcg-ai-battle
```

Kaggleの認証情報はチャットやGitに貼らないでください。通常は `~/.kaggle/` 配下に置きます。

## 今後の開発メモ

実装済み（主なもの）: ブラウザでのデッキ作成、AIエージェント登録（`.py`/`.ipynb`）、人 vs AI / 人 vs 人 / AI vs AI、対戦ログのリプレイ保存、個別AI評価、バトルロワイヤル（殿堂入り）。

次に進める候補:

1. バトルロワイヤルの殿堂入りをサーバー側で勝利検証する（現状はクライアント申告制）。
2. 公開サーバーでのカスタムAI実行のサンドボックス化（現状は `POKECA_ALLOW_AGENT_UPLOAD=0` で無効化して回避）。
