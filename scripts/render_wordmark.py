"""Render the Whalewatch lockup into ``app/api/assets/wordmark.png``, for the
Excel exports' About sheet (:mod:`app.api.xlsx_export`).

The lockup is whalewatch-web's ``Wordmark`` (src/modules/Wordmark/Wordmark.tsx):
the mark beside WHALEWATCH in Archivo bold at width 124, tracked 0.02em. Its
``tone="inherit"``, every part in one ink, because the brand tone's accent line
is ~1.9:1 on a white sheet. The ink is Ledger's surface, the colour of the
Data sheet's header row. A workbook can hold no font of its own, nor an SVG
that Excel before 2016 would draw, so the lockup is a PNG, drawn at
:data:`SCALE` times the size it is shown at so it stays sharp zoomed in.

Drawn by headless Chrome, so the type is the web's own font file, laid out as
the web lays it out. Needs Chrome and a whalewatch-web checkout beside this
repo (its public/fonts)::

    uv run python -m scripts.render_wordmark [--web ../whalewatch-web] [--chrome PATH]

Run it again when the web's Wordmark changes, and commit the PNG.
"""

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Final

from app.api.xlsx_export import WORDMARK, WORDMARK_SCALE

ROOT: Final = Path(__file__).resolve().parent.parent
SCALE: Final = WORDMARK_SCALE
#: The type size the lockup is shown at on the sheet, in CSS pixels.
TYPE_PX: Final = 22
INK: Final = "#232532"
#: The mark's geometry: Wordmark.tsx's LINE, its stroke and its dot.
LINE: Final = "3.5,12 6.6,25 9.7,15.5 12.8,24 16,11 19.2,23 22.3,13 25.4,21.5 28.5,5.5"

MAC_CHROME: Final = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _page(font: Path) -> str:
    return f"""<!doctype html>
<html><head><style>
@font-face {{
  font-family: "Archivo";
  font-weight: 400 700;
  font-stretch: 110% 125%;
  src: url("{font.as_uri()}") format("woff2");
}}
html, body {{ margin: 0; background: transparent; }}
#lockup {{
  display: inline-flex; align-items: center; gap: 0.5em; line-height: 1;
  font-size: {TYPE_PX}px; color: {INK};
  /* A pixel of room each side for the stroke's antialiasing. */
  padding: 1px;
}}
#lockup svg {{ width: 1.45em; height: 1.45em; flex: none; }}
#lockup span {{
  font-family: "Archivo"; font-weight: 700; font-variation-settings: "wdth" 124;
  letter-spacing: 0.02em; text-transform: uppercase; white-space: nowrap;
}}
</style></head><body>
<div id="lockup">
  <svg viewBox="0 0 32 32" aria-hidden="true">
    <polyline points="{LINE}" fill="none" stroke="currentColor" stroke-width="2.6"
      stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="28.5" cy="5.5" r="2.6" fill="currentColor"/>
  </svg>
  <span>Whalewatch</span>
</div>
<script>
document.fonts.ready.then(() => {{
  if (!document.fonts.check('700 {TYPE_PX}px "Archivo"')) throw new Error("Archivo did not load");
  const box = document.getElementById("lockup").getBoundingClientRect();
  document.title = JSON.stringify([Math.ceil(box.width), Math.ceil(box.height)]);
}});
</script>
</body></html>
"""


def _chrome(chrome: str, *args: str) -> str:
    return subprocess.run(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--allow-file-access-from-files",
            "--virtual-time-budget=5000",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def render(web: Path, chrome: str, out: Path) -> tuple[int, int]:
    """Write the PNG to ``out``; returns its size in pixels."""
    font = web / "public" / "fonts" / "archivo-latin-wdth-wght.woff2"
    if not font.is_file():
        raise SystemExit(f"no Archivo at {font}: pass --web the whalewatch-web checkout")
    with tempfile.TemporaryDirectory() as directory:
        page = Path(directory) / "wordmark.html"
        page.write_text(_page(font))
        dom = _chrome(chrome, "--dump-dom", page.as_uri())
        found = re.search(r"<title>(\[\d+,\d+\])</title>", dom)
        if found is None:
            raise SystemExit("Chrome drew no lockup: did the font load?")
        width, height = json.loads(found.group(1))
        shot = Path(directory) / "wordmark.png"
        _chrome(
            chrome,
            f"--screenshot={shot}",
            f"--window-size={width},{height}",
            f"--force-device-scale-factor={SCALE}",
            "--default-background-color=00000000",
            page.as_uri(),
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(shot, out)
    return width * SCALE, height * SCALE


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--web", type=Path, default=ROOT.parent / "whalewatch-web")
    parser.add_argument("--chrome", default=shutil.which("google-chrome") or MAC_CHROME)
    args = parser.parse_args()
    width, height = render(args.web, args.chrome, WORDMARK)
    print(f"{WORDMARK.relative_to(ROOT)}: {width}x{height}")


if __name__ == "__main__":
    main()
