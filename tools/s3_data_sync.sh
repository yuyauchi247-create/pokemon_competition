#!/usr/bin/env bash
# ローカル/本番の data/user_decks・data/user_agents を S3 経由で共有する双方向同期。
#
# 方針: push(ローカル→S3) → pull(S3→ローカル) を --delete 無しで行う「和集合」同期。
#   - ファイル名(デッキ/AIのID)はタイムスタンプで衝突しないので、両環境の新規分が安全に合流する。
#   - --delete を使わないので、片方での削除は伝播しない（誤消し事故を防ぐ。削除は手動運用）。
#   - 単一共有ファイル(meta.json のリネーム等)が両環境で同時編集されると last-write-wins。
#
# 使い方:
#   POKECA_S3_DATA_BUCKET=pokeca-data-662185224054 tools/s3_data_sync.sh
#   （cron で数分ごとに回す。起動時にも1回呼ぶ。）
#
# 前提: aws CLI がインストール済みで、対象バケットへの read/write 権限を持つ認証が設定済み。
set -euo pipefail

BUCKET="${POKECA_S3_DATA_BUCKET:-pokeca-data-662185224054}"
REGION="${AWS_REGION:-ap-northeast-1}"

# リポジトリルート（このスクリプトの2つ上）を基準にする
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="$ROOT/data"

DIRS=(user_decks user_agents user_combos)
EXCLUDES=(--exclude ".DS_Store" --exclude "*/.DS_Store" --exclude "_subtest*" --exclude "__pycache__/*" --exclude "*/__pycache__/*")

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }
log() { echo "[$(ts)] s3-sync: $*"; }

for d in "${DIRS[@]}"; do
  mkdir -p "$DATA/$d"
  # push: ローカルの新規/更新を S3 へ（削除はしない）
  aws s3 sync "$DATA/$d" "s3://$BUCKET/$d/" --region "$REGION" --no-progress "${EXCLUDES[@]}"
  # pull: S3(=他環境の分も含む) をローカルへ（削除はしない）
  aws s3 sync "s3://$BUCKET/$d/" "$DATA/$d" --region "$REGION" --no-progress "${EXCLUDES[@]}"
done

log "synced ${DIRS[*]} <-> s3://$BUCKET"
