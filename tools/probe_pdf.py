"""配布PDF(Card_ID List_JP.pdf)の構造を調べる。画像抽出の可否判断用。"""
import pymupdf

PDF = "data/Card_ID List_JP.pdf"
doc = pymupdf.open(PDF)
print(f"ページ数: {doc.page_count}")

for pno in (0, 1):
    page = doc[pno]
    imgs = page.get_images(full=True)
    txt = page.get_text().strip()
    print(f"\n===== page {pno} =====")
    print(f"画像数: {len(imgs)}")
    # 画像の配置（bbox）を取得
    info = page.get_image_info(xrefs=True)
    for i, im in enumerate(info[:8]):
        b = im.get("bbox")
        print(f"  img[{i}] xref={im.get('xref')} bbox=({b[0]:.0f},{b[1]:.0f},{b[2]:.0f},{b[3]:.0f}) "
              f"{im.get('width')}x{im.get('height')}")
    print("  --- テキスト(先頭400字) ---")
    print("  " + txt[:400].replace("\n", " | "))
