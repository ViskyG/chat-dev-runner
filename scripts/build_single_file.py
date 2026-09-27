#!/usr/bin/env python3
"""Build a self-contained offline HTML prototype; no dependencies or network."""
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def build() -> Path:
    html=(ROOT/'prototype/index.html').read_text()
    html=html.replace('<link rel="stylesheet" href="styles.css">','<style>\n'+(ROOT/'prototype/styles.css').read_text()+'\n</style>')
    html=html.replace('<script src="app.js" defer></script>','<script>\n'+(ROOT/'prototype/app.js').read_text()+'\n</script>')
    # The source currently puts its script at the body end; accept either spelling.
    html=html.replace('<script src="app.js"></script>','<script>\n'+(ROOT/'prototype/app.js').read_text()+'\n</script>')
    if 'href="styles.css"' in html or 'src="app.js"' in html:
        raise RuntimeError('Uninlined source asset')
    out=ROOT/'runner-prototype.html';out.write_text(html,encoding='utf-8');return out
if __name__=='__main__':print(build())
