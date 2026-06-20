# ポケカ対戦アプリ 実行用イメージ
#
# 配布シミュレータ libcg.so は Linux x86-64 専用のため、必ず amd64 で動かす。
# (Apple Silicon など arm64 ホストでは Docker のエミュレーションで amd64 実行する)
FROM --platform=linux/amd64 python:3.12-slim

# libcg.so は glibc 2.14+ / libstdc++ (GLIBCXX 3.4.29+) を要求する。
# bookworm ベースは glibc 2.36 / GCC 12 系で要件を満たすが、libstdc++6 を明示的に入れる。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 実行時に必要なのは flask のみ（kaggle/pymupdf は実対戦では不要）。
# 本番(compose.prod.yaml)では gunicorn 経由で起動するため同梱しておく。
# pandas/numpy は一部の登録AIが import するため同梱（無いとロードに失敗する）。
RUN pip install --no-cache-dir "flask>=3.1.3" "gunicorn>=21.2" "numpy" "pandas"

# アプリ一式をコピー
COPY . /app

# 0.0.0.0 で待ち受け、コンテナ外からアクセスできるようにする
ENV POKECA_WEBAPP_HOST=0.0.0.0 \
    POKECA_WEBAPP_PORT=5000

EXPOSE 5000

CMD ["python", "tools/webapp/server.py"]
