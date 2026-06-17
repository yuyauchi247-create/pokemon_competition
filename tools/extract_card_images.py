"""配布PDFから全カードの券面画像を抽出して data/card_images/ に保存する。

各カードID k の画像は PDF の page = 38 + k に1枚埋め込まれている
（先頭39ページはテキスト索引、p39 がカードID 1）。
"""
import json
import pymupdf

from sim_env import all_card_data, ROOT

FIRST_IMG_PAGE = 39  # カードID 1 のページ
doc = pymupdf.open("data/Card_ID List_JP.pdf")
imgdir = ROOT / "data" / "card_images"
imgdir.mkdir(exist_ok=True)

manifest, count, total = {}, 0, 0
for c in all_card_data():
    cid = c.cardId
    p = FIRST_IMG_PAGE + (cid - 1)
    if p >= doc.page_count:
        continue
    imgs = doc[p].get_images(full=True)
    if not imgs:
        continue
    d = doc.extract_image(imgs[0][0])
    fn = f"{cid}.{d['ext']}"
    (imgdir / fn).write_bytes(d["image"])
    manifest[cid] = fn
    count += 1
    total += len(d["image"])

(ROOT / "data" / "card_image_map.json").write_text(
    json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
print(f"抽出 {count} 枚, 合計 {round(total/1e6, 1)} MB -> data/card_images/")
