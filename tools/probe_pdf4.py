"""画像ページの開始位置を特定し、カードIDとの対応を描画で照合する。"""
import pymupdf
from sim_env import card_name

doc = pymupdf.open("data/Card_ID List_JP.pdf")

# 画像ページの開始位置を探す
first_img = None
for pno in range(0, 60):
    if len(doc[pno].get_images(full=True)) > 0:
        first_img = pno
        break
print(f"最初の画像ページ: p{first_img}")
print(f"総ページ {doc.page_count} / 画像ページ数(推定) {doc.page_count - first_img}")

# 仮説: カードID k の画像は page = first_img + (k-1)
def page_for(cid):
    return first_img + (cid - 1)

for cid in [1, 12, 721, 723, 1267]:
    p = page_for(cid)
    if p < doc.page_count:
        pix = doc[p].get_pixmap(dpi=90)
        fn = f"data/_check_card{cid}.png"
        pix.save(fn)
        print(f"card {cid} ({card_name(cid)}) -> p{p} -> {fn}")
