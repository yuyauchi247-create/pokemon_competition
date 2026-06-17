"""PDFのハイパーリンク(券面画像)を抽出し、URL形式とCardID対応を調べる。"""
import pymupdf

doc = pymupdf.open("data/Card_ID List_JP.pdf")

for pno in (0, 1):
    page = doc[pno]
    links = [l for l in page.get_links() if l.get("uri")]
    words = page.get_text("words")  # (x0,y0,x1,y1, word, block,line,wordno)
    print(f"\n===== page {pno}: links={len(links)} =====")
    for l in links[:6]:
        r = l["from"]  # Rect
        # 同じ行(yが近い)で最も左にある「数字」をCardIDとみなす
        row_words = [w for w in words if abs(w[1] - r.y0) < 6]
        row_words.sort(key=lambda w: w[0])
        ids = [w[4] for w in row_words if w[4].isdigit()]
        print(f"  y={r.y0:.0f} cardId?={ids[:1]} uri={l['uri']}")
