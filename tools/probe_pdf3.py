"""PDFの各所のページに画像が埋め込まれているか調査し、サンプルを描画保存。"""
import pymupdf

doc = pymupdf.open("data/Card_ID List_JP.pdf")
print(f"総ページ数: {doc.page_count}")

for pno in [0, 1, 2, 3, 10, 50, 100, 500, 1000, 1305]:
    if pno >= doc.page_count:
        continue
    page = doc[pno]
    imgs = page.get_images(full=True)
    info = page.get_image_info(xrefs=True)
    txt = page.get_text().strip().replace("\n", " | ")[:70]
    print(f"p{pno}: get_images={len(imgs)} image_info={len(info)} text='{txt}'")

# 画像がありそうなページを描画して目視
for pno in [2, 3, 50]:
    if pno < doc.page_count:
        pix = doc[pno].get_pixmap(dpi=80)
        pix.save(f"data/_pdf_page{pno}.png")
        print(f"saved data/_pdf_page{pno}.png ({pix.width}x{pix.height})")
