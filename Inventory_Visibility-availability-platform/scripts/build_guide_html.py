#!/usr/bin/env python3
"""Render INTERVIEW-GUIDE.md into a self-contained, print-ready HTML file.

Open the result in a browser and use Print -> Save as PDF.
"""
from __future__ import annotations

import pathlib

import markdown

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "INTERVIEW-GUIDE.md"
OUT = ROOT / "INTERVIEW-GUIDE.html"

CSS = """
:root{
  --ink:#1a1f22; --muted:#5c666b; --rule:#d8ddd9; --rule-soft:#e8ece8;
  --accent:#1d6a5a; --accent2:#37678e; --red:#ae2e20; --code-bg:#f4f6f4;
  --quote-bg:#f0f4f2; --quote-bar:#1d6a5a;
}
*{box-sizing:border-box}
html{-webkit-print-color-adjust:exact;print-color-adjust:exact}
body{
  margin:0 auto; max-width:820px; padding:48px 44px 80px;
  color:var(--ink); background:#fff;
  font:16px/1.62 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
}
h1,h2,h3,h4{line-height:1.25;font-weight:650;margin:1.6em 0 .55em}
h1{font-size:30px;margin-top:0;letter-spacing:-.01em}
h2{font-size:23px;padding-bottom:.28em;border-bottom:2px solid var(--rule);
   margin-top:1.9em}
h3{font-size:19px;color:#232a2d}
h4{font-size:16.5px;color:var(--muted);text-transform:uppercase;
   letter-spacing:.04em;font-weight:600}
p{margin:.7em 0}
a{color:var(--accent2);text-decoration:none}
strong{font-weight:650}
hr{border:0;border-top:1px solid var(--rule);margin:2.4em 0}
ul,ol{padding-left:1.4em;margin:.7em 0}
li{margin:.28em 0}
code{
  font:13.5px/1.5 "SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
  background:var(--code-bg);padding:.12em .38em;border-radius:4px;
}
pre{
  background:var(--code-bg);border:1px solid var(--rule-soft);border-radius:8px;
  padding:14px 16px;overflow:auto;margin:1em 0;
}
pre code{background:none;padding:0;font-size:13px;line-height:1.55}
blockquote{
  margin:1em 0;padding:.65em 1.1em;background:var(--quote-bg);
  border-left:4px solid var(--quote-bar);border-radius:0 6px 6px 0;
  color:#26302e;
}
blockquote p{margin:.4em 0}
table{
  border-collapse:collapse;width:100%;margin:1.1em 0;font-size:14px;
  border:1px solid var(--rule);
}
th,td{border:1px solid var(--rule);padding:7px 11px;text-align:left;
  vertical-align:top}
thead th{background:#eef2ee;font-weight:650}
tbody tr:nth-child(even){background:#fafbfa}
h1,h2,h3,h4{break-after:avoid}
table,pre,blockquote{break-inside:avoid}
@page{margin:16mm 14mm}
@media print{
  body{max-width:none;padding:0}
  a{color:inherit}
}
.print-hint{
  position:fixed;top:14px;right:14px;background:var(--accent);color:#fff;
  font-size:13px;padding:8px 14px;border-radius:6px;border:0;cursor:pointer;
  box-shadow:0 2px 8px rgba(0,0,0,.18);font-weight:600;
}
@media print{.print-hint{display:none}}
"""

def main() -> None:
    text = SRC.read_text(encoding="utf-8")
    html_body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists", "toc"],
    )
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Inventory Visibility Availability Platform — Interview Guide</title>
<style>{CSS}</style>
</head>
<body>
<button class="print-hint" onclick="window.print()">Save as PDF</button>
{html_body}
</body>
</html>
"""
    OUT.write_text(doc, encoding="utf-8")
    kb = len(doc.encode("utf-8")) / 1024
    print(f"{OUT.name}   {kb:.0f} KB")

if __name__ == "__main__":
    main()
