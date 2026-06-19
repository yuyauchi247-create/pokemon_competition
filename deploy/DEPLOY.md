# 本番デプロイ手順書（AWS Lightsail VPS / mypokeca.com）

ポケカ対戦アプリを AWS Lightsail（常時稼働 VPS）へデプロイし、
`https://mypokeca.com` で公開する手順。自動 HTTPS は Caddy、
本番アプリは gunicorn(1 worker/1 thread) で動かす。

- ドメイン: **mypokeca.com**（Route 53 登録済み / 有効期限 2027-06-20）
- ホストゾーンID: **Z0566346CVRG29505QAL**
- 構成: Caddy(80/443) → webapp(gunicorn 5000) を Docker Compose で起動
- 設定ファイル: リポジトリ直下 `compose.prod.yaml` / `deploy/Caddyfile`

> 重要: libcg.so は ctypes のプロセスグローバル状態を使うため並行実行不可。
> gunicorn は 1 worker / 1 thread で全リクエストを直列化している。
> 総当たり戦の実行中は他リクエストがブロックされる（PoC では許容）。

---

## 0. 前提

- ローカルに `data/card_images/`（174MB）と `data/sample_submission/cg/libcg.so` が存在すること
  （どちらも git 管理外。初回 rsync でVPSへ送る）
- ローカルに AWS CLI 認証済み（アカウント 662185224054）
- ローカルに rsync / ssh があること（macOS は標準装備）

---

## 1. Lightsail インスタンス作成（コンソール）

URL: https://lightsail.aws.amazon.com/ls/webapp/home/instances

1. **Create instance**
2. リージョン: **Tokyo (ap-northeast-1)**（日本からの遅延が小さい）
3. プラットフォーム: **Linux/Unix**
4. Blueprint: **OS Only → Ubuntu 22.04 LTS**
5. プラン: **$12/月（2GB RAM / 2 vCPU / 60GB SSD）** を推奨
   - 最小は $7（1GB）でも動くが、Docker ビルド時にメモリが厳しいことがある
6. インスタンス名: `pokeca-prod` など → **Create instance**

### 固定IP（Static IP）を割り当て

1. Lightsail → **Networking** → **Create static IP**
2. リージョン Tokyo、上で作ったインスタンスにアタッチ
3. 払い出された IP をメモ（以後 `<STATIC_IP>` と表記）
   - 静的IPはインスタンスにアタッチされている限り無料

### ファイアウォール（80/443 を開放）

1. インスタンス → **Networking** タブ → IPv4 Firewall
2. 既定で 22(SSH) は開いている。以下を **Add rule** で追加:
   - HTTP / TCP / **80**
   - HTTPS / TCP / **443**

---

## 2. DNS（A レコード）を設定

`<STATIC_IP>` を実際の固定IPに置き換えて、ローカルから実行:

```bash
cat > /tmp/dns.json <<'JSON'
{
  "Comment": "point apex and www to Lightsail",
  "Changes": [
    {"Action":"UPSERT","ResourceRecordSet":{
      "Name":"mypokeca.com","Type":"A","TTL":300,
      "ResourceRecords":[{"Value":"<STATIC_IP>"}]}},
    {"Action":"UPSERT","ResourceRecordSet":{
      "Name":"www.mypokeca.com","Type":"A","TTL":300,
      "ResourceRecords":[{"Value":"<STATIC_IP>"}]}}
  ]
}
JSON

aws route53 change-resource-record-sets \
  --hosted-zone-id Z0566346CVRG29505QAL \
  --change-batch file:///tmp/dns.json
```

反映確認（数分かかることがある）:
```bash
dig +short mypokeca.com
```

---

## 3. VPS の初期セットアップ（Docker 導入）

SSH 接続（Lightsail コンソールの「Connect using SSH」でも可。鍵を使う場合は
Lightsail の Account → SSH keys からダウンロードした鍵を指定）:

```bash
ssh ubuntu@<STATIC_IP>
```

VPS 上で Docker と Compose プラグインを導入:
```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# ubuntu ユーザーで docker を sudo 無しで使えるように
sudo usermod -aG docker ubuntu
# 反映のため一度ログアウト→再ログイン
exit
```

デプロイ先ディレクトリを作成（再ログイン後）:
```bash
ssh ubuntu@<STATIC_IP>
sudo mkdir -p /opt/pokeca
sudo chown ubuntu:ubuntu /opt/pokeca
exit
```

---

## 4. 初回フルアップロード（ローカルから rsync）

コード + data（card_images / libcg.so / CSV / 既存ユーザーデータ）を**まとめて**送る。
リポジトリのルートで実行:

```bash
cd /Users/yauchiyu/Documents/pokemon_competition

rsync -az --info=progress2 \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='node_modules' \
  --exclude='.playwright-mcp' \
  --exclude='kaggle.json' \
  --exclude='.kaggle' \
  --exclude='data/reference/kaggle_notebooks' \
  ./ ubuntu@<STATIC_IP>:/opt/pokeca/
```

> この初回だけ `data/` を含めて送る（card_images 174MB を含むため数分かかる）。
> 以降の自動デプロイ(GitHub Actions)は `data/` に触れないので、ここで送った
> card_images・libcg.so・ユーザーデータは保持される。

---

## 5. 初回起動

VPS 上で:
```bash
ssh ubuntu@<STATIC_IP>
cd /opt/pokeca
docker compose -f compose.prod.yaml up -d --build
docker compose -f compose.prod.yaml logs -f   # 起動ログ確認(Ctrl-C で抜ける)
```

確認:
- `https://mypokeca.com` をブラウザで開く（証明書取得に初回 10〜30 秒）
- Caddy が Let's Encrypt 証明書を自動取得（80/443 開放 & DNS 反映済みが条件）

---

## 6. CI/CD（main 更新で自動デプロイ）

`.github/workflows/deploy.yml` が `main` への push で発火し、
コードを rsync → `docker compose up -d --build` する。

### 6-1. デプロイ用 SSH 鍵を作成（ローカル）

```bash
ssh-keygen -t ed25519 -f ~/.ssh/pokeca_deploy -N "" -C "github-actions-deploy"
```

公開鍵を VPS の authorized_keys に追加:
```bash
ssh-copy-id -i ~/.ssh/pokeca_deploy.pub ubuntu@<STATIC_IP>
# ssh-copy-id が無ければ:
# cat ~/.ssh/pokeca_deploy.pub | ssh ubuntu@<STATIC_IP> 'cat >> ~/.ssh/authorized_keys'
```

### 6-2. GitHub Secrets を登録

リポジトリ → Settings → Secrets and variables → Actions → New repository secret:

| Secret 名 | 値 |
|---|---|
| `LIGHTSAIL_HOST` | `<STATIC_IP>` |
| `LIGHTSAIL_USER` | `ubuntu` |
| `LIGHTSAIL_SSH_KEY` | `~/.ssh/pokeca_deploy` の**中身全体**（秘密鍵） |

gh CLI で一括登録する場合（ローカル、リポジトリ内で）:
```bash
gh secret set LIGHTSAIL_HOST  --body "<STATIC_IP>"
gh secret set LIGHTSAIL_USER  --body "ubuntu"
gh secret set LIGHTSAIL_SSH_KEY < ~/.ssh/pokeca_deploy
```

### 6-3. 動作確認

`main` に何か push する（または Actions タブ → Deploy to Lightsail → Run workflow）。
ワークフローが成功し、変更が `https://mypokeca.com` に反映されれば完了。

---

## 運用メモ

- **data/ の参照データ(CSV 等)を更新したとき**: CI は data/ を同期しないので、
  手動で `4. 初回フルアップロード` の rsync を再実行する（card_images 等は変わらないので普段は不要）。
- **ログ確認**: `cd /opt/pokeca && docker compose -f compose.prod.yaml logs -f`
- **再起動**: `docker compose -f compose.prod.yaml restart`
- **停止**: `docker compose -f compose.prod.yaml down`
- **AI アップロードは本番で無効**（`POKECA_ALLOW_AGENT_UPLOAD=0`）。
  任意コード exec による RCE を防ぐため。AI 追加はローカルで行い rsync で反映する。
