"""PDF内にURLが存在するか、複数経路で探す。"""
import pymupdf

doc = pymupdf.open("data/Card_ID List_JP.pdf")

# 1) 全ページのリンク総数
total_links = 0
sample = []
for i in range(min(doc.page_count, 60)):
    ls = [l for l in doc[i].get_links() if l.get("uri")]
    total_links += len(ls)
    if ls and len(sample) < 3:
        sample.append(ls[0]["uri"])
print(f"[get_links] 先頭60ページのuriリンク数: {total_links}  例: {sample}")

# 2) ページ注釈(annots)を確認
p = doc[0]
annots = list(p.annots()) if p.annots() else []
print(f"[annots] page0 注釈数: {len(annots)}")
for a in annots[:5]:
    print(f"   type={a.type} info={a.info}")

# 3) 生のxrefをスキャンして 'URI' / 'http' を含むオブジェクトを探す
found = 0
for xref in range(1, min(doc.xref_length(), 3000)):
    try:
        s = doc.xref_object(xref, compressed=False)
    except Exception:
        continue
    if "URI" in s or "http" in s:
        found += 1
        if found <= 3:
            print(f"   xref {xref}: {s[:200]}")
print(f"[xref scan] URI/httpを含むオブジェクト(先頭3000): {found}")
