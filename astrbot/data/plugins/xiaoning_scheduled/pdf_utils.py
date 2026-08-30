"""HTML+CSS → Chrome headless PDF 渲染(中文铁律: 不用 Python PDF 库)。

设计标准见记忆 pdf-generation-standards:
- 字体栈: 微软雅黑正文 / 黑体标题 / 楷体引言
- 封面: 深色渐变 + 几何装饰 + 朱红点缀
- @page 页边距 + 页码 + 页眉底线
"""

from __future__ import annotations

import html
import re
import subprocess
from pathlib import Path

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

_FONT_STACK = '"Microsoft YaHei", "SimHei", "Noto Sans SC", sans-serif'

_PAGE_CSS = f"""
@page {{
  size: A4;
  margin: 22mm 20mm 24mm 20mm;
  @bottom-center {{
    content: "小柠 · 第 " counter(page) " 页 / 共 " counter(pages) " 页";
    font-family: {_FONT_STACK};
    font-size: 9pt;
    color: #9a8f7d;
  }}
  @top-center {{
    content: "· 小柠定时报送 ·";
    font-family: {_FONT_STACK};
    font-size: 8pt;
    letter-spacing: 6px;
    color: #b22222;
    border-bottom: 0.5pt solid #e8dcc8;
    padding-bottom: 4mm;
  }}
}}
body {{
  font-family: {_FONT_STACK};
  line-height: 1.75;
  color: #2b2620;
  font-size: 11pt;
  margin: 0;
}}
h1 {{
  color: #b22222;
  font-size: 17pt;
  border-bottom: 1.5pt solid #b22222;
  padding-bottom: 4pt;
  margin: 26pt 0 14pt 0;
  page-break-after: avoid;
}}
h2 {{
  color: #3d2b1f;
  font-size: 13pt;
  margin: 20pt 0 8pt 0;
  page-break-after: avoid;
}}
p {{ margin: 8pt 0; text-align: justify; }}
ul, ol {{ margin: 8pt 0; padding-left: 22pt; }}
li {{ margin: 4pt 0; break-inside: avoid; }}
a {{ color: #8b1a1a; text-decoration: none; border-bottom: 0.5pt solid #d4a84a; }}
blockquote {{
  font-family: "KaiTi", "STKaiti", "Microsoft YaHei", sans-serif;
  line-height: 2.1;
  color: #5c4632;
  border-left: 3pt solid #d4a84a;
  background: #fdf9f0;
  margin: 14pt 0;
  padding: 10pt 14pt;
  font-size: 11.5pt;
}}
hr {{ border: none; border-top: 0.5pt dashed #d8cbb4; margin: 18pt 0; }}
strong {{ color: #8b1a1a; }}
em {{ color: #6f5a45; }}
.report-signoff {{
  color: #9a8f7d;
  font-size: 9pt;
  line-height: 1.35;
  margin: 6pt 0 0 0;
  text-align: right;
  break-inside: avoid;
}}
.cover {{
  page-break-after: always;
  position: relative;
  height: 246mm;
  background: linear-gradient(150deg, #0a0e1a 0%, #1a2035 55%, #2a1f3d 100%);
  color: #f4efe4;
  overflow: hidden;
}}
.cover .deco {{
  position: absolute;
  border: 1.5pt solid rgba(212, 168, 74, 0.55);
}}
.cover .deco.d1 {{ width: 130pt; height: 130pt; top: 28mm; left: -40pt; transform: rotate(20deg); }}
.cover .deco.d2 {{ width: 90pt; height: 90pt; top: 60mm; right: -30pt; transform: rotate(60deg); }}
.cover .deco.d3 {{ width: 200pt; height: 4pt; bottom: 60mm; left: 20mm; background: #d4a84a; border: none; opacity: 0.7; }}
.cover .inner {{
  position: absolute;
  top: 78mm; left: 0; right: 0;
  text-align: center;
}}
.cover .badge {{
  display: inline-block;
  border: 1.5pt solid #d4a84a;
  color: #d4a84a;
  font-size: 13pt;
  letter-spacing: 4pt;
  padding: 6pt 18pt;
  margin-bottom: 14mm;
}}
.cover .cover-title {{
  font-size: 30pt;
  font-weight: bold;
  letter-spacing: 6pt;
  margin: 0 0 8mm 0;
  color: #f4efe4;
}}
.cover .sub {{
  font-family: "KaiTi", "STKaiti", sans-serif;
  font-size: 14pt;
  letter-spacing: 2pt;
  color: #cbbfa8;
}}
.cover .meta {{
  position: absolute;
  bottom: 30mm; left: 0; right: 0;
  text-align: center;
  font-size: 10pt;
  color: #9a8f7d;
  letter-spacing: 2pt;
}}
"""


def _md_to_html(text: str) -> str:
    """受控 Markdown→HTML: 保留标题、列表、强调、引用与可点击来源链接。"""
    out: list[str] = []
    list_kind: str | None = None

    def close_list():
        nonlocal list_kind
        if list_kind is not None:
            out.append(f"</{list_kind}>")
            list_kind = None

    def open_list(kind: str):
        nonlocal list_kind
        if list_kind != kind:
            close_list()
            out.append(f"<{kind}>")
            list_kind = kind

    lines = text.splitlines()
    nonempty_indexes = [i for i, value in enumerate(lines) if value.strip()]
    trailing_rule_index: int | None = None
    if len(nonempty_indexes) >= 2:
        last_index, previous_index = nonempty_indexes[-1], nonempty_indexes[-2]
        if (
            lines[last_index].strip().startswith("由小柠自动生成")
            and re.match(r"^\s*(?:-{3,}|—{3,}|–{3,})\s*$", lines[previous_index])
        ):
            trailing_rule_index = previous_index

    for index, raw in enumerate(lines):
        line = raw.rstrip()
        if index == trailing_rule_index:
            close_list()
            continue
        if not line:
            close_list()
            continue
        if line.startswith("### "):
            close_list()
            out.append(f"<h2>{_inline_md(line[4:])}</h2>")
        elif line.startswith("## "):
            close_list()
            out.append(f"<h1>{_inline_md(line[3:])}</h1>")
        elif line.startswith("# "):
            close_list()
            out.append(f"<h1>{_inline_md(line[2:])}</h1>")
        elif line.startswith("> "):
            close_list()
            out.append(f"<blockquote>{_inline_md(line[2:])}</blockquote>")
        elif line.startswith(("- ", "* ")):
            open_list("ul")
            out.append(f"<li>{_inline_md(line[2:])}</li>")
        elif re.match(r"^\s*(?:-{3,}|—{3,}|–{3,})\s*$", line):
            close_list()
            out.append("<hr/>")
        elif re.match(r"^\d+[.、)]\s", line):
            open_list("ol")
            stripped = re.sub(r"^\d+[.、)]\s*", "", line)
            out.append(f"<li>{_inline_md(stripped)}</li>")
        elif line.startswith("由小柠自动生成"):
            close_list()
            out.append(f'<p class="report-signoff">{_inline_md(line)}</p>')
        else:
            close_list()
            out.append(f"<p>{_inline_md(line)}</p>")
    close_list()
    return "\n".join(out)


def _inline_md(text: str) -> str:
    urls: list[str] = []
    markdown_links: list[tuple[str, str]] = []

    def stash_markdown_link(match: re.Match[str]) -> str:
        token = f"@@XIAONING_LINK_{len(markdown_links)}@@"
        markdown_links.append((match.group(1), match.group(2)))
        return token

    def stash_url(match: re.Match[str]) -> str:
        value = match.group(0)
        url = value.rstrip(".,;:!?，。；！？、）》】”’")
        suffix = value[len(url):]
        token = f"@@XIAONING_URL_{len(urls)}@@"
        urls.append(url)
        return token + suffix

    protected = re.sub(
        r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", stash_markdown_link, text
    )
    escaped = _esc(re.sub(r"https?://\S+", stash_url, protected))
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", escaped)
    for index, (label, url) in enumerate(markdown_links):
        anchor = f'<a href="{html.escape(url, quote=True)}">{_esc(label)}</a>'
        escaped = escaped.replace(f"@@XIAONING_LINK_{index}@@", anchor)
    for index, url in enumerate(urls):
        anchor = (
            f'<a href="{html.escape(url, quote=True)}">{_esc(url)}</a>'
        )
        escaped = escaped.replace(f"@@XIAONING_URL_{index}@@", anchor)
    return escaped


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def render_document(title: str, subtitle: str, badge: str | None,
                    sections: list[tuple[str, str]], note: str | None = None) -> str:
    """组装完整 HTML. sections = [(标题, markdown正文)], note = 落款."""
    body_parts = []
    for heading, body in sections:
        body_parts.append(f"<h1>{_esc(heading)}</h1>{_md_to_html(body)}")
    body_html = "\n".join(body_parts)
    if note:
        body_html += f"\n<blockquote>{_esc(note)}</blockquote>"
    badge_html = f'<div class="badge">{_esc(badge)}</div>' if badge else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{_esc(title)}</title>
<style>{_PAGE_CSS}</style></head>
<body>
<div class="cover">
  <div class="deco d1"></div><div class="deco d2"></div><div class="deco d3"></div>
  <div class="inner">
    {badge_html}
    <div class="cover-title">{_esc(title)}</div>
    <div class="sub">{_esc(subtitle)}</div>
  </div>
  <div class="meta">小柠 · 早报 · 午报 · 晚报</div>
</div>
{body_html}
</body></html>"""


def render_pdf(html_text: str, pdf_path: Path) -> Path:
    """HTML 字符串 → Chrome headless PDF."""
    pdf_path = Path(pdf_path).resolve()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    html_path = pdf_path.with_suffix(".html")
    html_path.write_text(html_text, encoding="utf-8")
    subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={pdf_path}", html_path.as_uri()],
        check=True, capture_output=True, timeout=120,
    )
    if not pdf_path.is_file() or pdf_path.stat().st_size < 100:
        raise RuntimeError(f"Chrome did not create a valid PDF: {pdf_path}")
    return pdf_path


# ── 书信风格模板(告别信等): 楷体信纸 + 首行缩进 + 落款 ──────────

_LETTER_CSS = f"""
@page {{
  size: A4;
  margin: 24mm 22mm 26mm 22mm;
  @bottom-right {{
    content: counter(page) " / " counter(pages);
    font-family: {_FONT_STACK};
    font-size: 8.5pt;
    color: #b8ad9a;
  }}
}}
body {{
  font-family: "KaiTi", "STKaiti", "Microsoft YaHei", sans-serif;
  line-height: 2.05;
  color: #3a3227;
  font-size: 12pt;
  margin: 0;
  background:
    repeating-linear-gradient(transparent 0 31.9pt, rgba(180,170,150,0.15) 31.9pt 32pt),
    #fdfbf7;
}}
.sheet {{ padding: 10mm 8mm; }}
.letterhead {{
  text-align: center;
  margin-bottom: 18pt;
  font-family: "SimHei", "Microsoft YaHei", sans-serif;
  font-size: 15pt;
  letter-spacing: 8pt;
  color: #3d2b1f;
  border-bottom: 0.8pt solid #cbbfa8;
  padding-bottom: 8pt;
}}
.salutation {{
  font-family: "SimHei", "Microsoft YaHei", sans-serif;
  font-size: 13pt;
  margin-bottom: 12pt;
}}
p {{ margin: 0 0 10pt 0; text-indent: 2em; text-align: justify; }}
p.noindent {{ text-indent: 0; }}
li {{ margin: 2pt 0; text-indent: 0; list-style: none; }}
li::before {{ content: "· "; color: #9a8f7d; }}
blockquote {{
  border-left: 2pt solid #d4c6ad;
  background: #f8f3e8;
  margin: 12pt 0;
  padding: 8pt 14pt;
}}
.signoff {{
  text-align: right;
  margin-top: 24pt;
  font-size: 13pt;
  color: #5c4632;
}}
"""


def _split_salutation(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip():
            head = ln.strip()
            if head.endswith((":", "：")) and len(head) < 20:
                return head, "\n".join(lines[i + 1:]).strip()
            return "", text
    return "", text


def render_letter(letter_title: str, body_md: str,
                  signature: str = "小柠", date_str: str = "") -> str:
    """书信风格 HTML: 楷体信纸、首行缩进、落款右对齐, 无封面无装饰色块."""
    salutation, body = _split_salutation(body_md)
    body_html = _md_to_html(body)
    # 书信化: 标题→黑体段落, 列表/引用保持克制
    body_html = re.sub(
        r"<h1[^>]*>",
        '<p style="font-family:SimHei,sans-serif;font-size:13pt;'
        'margin:16pt 0 8pt 0;text-indent:0;color:#3d2b1f;">',
        body_html)
    body_html = body_html.replace("</h1>", "</p>")
    salutation_html = f'<p class="salutation">{_esc(salutation)}</p>' if salutation else ""
    date_html = f'<p class="noindent" style="text-align:right;">{_esc(date_str)}</p>' if date_str else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{_esc(letter_title)}</title>
<style>{_LETTER_CSS}</style></head>
<body>
<div class="sheet">
  <div class="letterhead">{_esc(letter_title)}</div>
  {salutation_html}
  {body_html}
  {date_html}
  <p class="signoff">—— {_esc(signature)}</p>
</div>
</body></html>"""
