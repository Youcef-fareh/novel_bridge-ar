from pathlib import Path
from selectolax.parser import HTMLParser

ch_path = Path('example for html to some wbsite to help in scraping/wtr lab/chpter/Read All Gods, Starting with SSS-level Talent RAW English Translation - WTR-LAB.html')
html = ch_path.read_text(encoding='utf-8', errors='ignore')
tree = HTMLParser(html)

print("=== H1 tags ===")
for h in tree.css('h1, h2, h3'):
    print(f"<{h.tag}> class={h.attributes.get('class')} text={h.text(strip=True)[:50]}")

print("\n=== Containers with paragraphs ===")
for div in tree.css('div, article, main, section'):
    ps = div.css('p')
    if len(ps) > 10:
        cls = div.attributes.get('class')
        tag = div.tag
        id_attr = div.attributes.get('id')
        print(f"Tag={tag} id={id_attr} class={cls} | p_count={len(ps)}")

# Let's inspect the text of the first few paragraphs
main_container = None
for div in tree.css('div, article, main, section'):
    ps = div.css('p')
    if len(ps) > 20:
        main_container = div
        break

if main_container:
    print("\nSample paragraphs:")
    for p in main_container.css('p')[:5]:
        print("  P:", repr(p.text(strip=True)[:60]))
