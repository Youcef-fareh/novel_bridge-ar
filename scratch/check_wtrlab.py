import json
from pathlib import Path
from selectolax.parser import HTMLParser

p = Path('example for html to some wbsite to help in scraping/wtr lab/Read All Gods, Starting with SSS-level Talent RAW English Translation - WTR-LAB.html')
html = p.read_text(encoding='utf-8', errors='ignore')
tree = HTMLParser(html)
next_data = tree.css_first('script#__NEXT_DATA__')
data = json.loads(next_data.text())
pageProps = data.get('props', {}).get('pageProps', {})

# If pageProps has 'series' or 'serie' or 'data' or similar
for k in pageProps.keys():
    v = pageProps[k]
    if isinstance(v, dict):
        print(f"Dict key {k}: {list(v.keys())}")
    elif isinstance(v, list) and v:
        print(f"List key {k}[0]: {type(v[0])}")
        if isinstance(v[0], dict):
            print(f"  sample item keys in {k}: {list(v[0].keys())}")

# Let's search inside data for raw_id or chapter or total_chapters
def find_keys(obj, target, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if target.lower() in k.lower():
                print(f"Found '{k}' at {path}.{k}: {str(v)[:100]}")
            find_keys(v, target, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj[:3]):
            find_keys(item, target, f"{path}[{i}]")

print("\n--- Searching for 'chapter' in __NEXT_DATA__ ---")
find_keys(data, 'chapter')
print("\n--- Searching for 'total' in __NEXT_DATA__ ---")
find_keys(data, 'total')
print("\n--- Searching for 'raw_id' in __NEXT_DATA__ ---")
find_keys(data, 'raw_id')
