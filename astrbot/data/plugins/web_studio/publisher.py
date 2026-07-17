"""Fixed-target Firebase publishing and local Edge previews for Web Studio."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


FIREBASE_PROJECT = "solar-modem-496213-f5"
FIREBASE_SITE = "solar-modem-496213-f5"
PUBLIC_ORIGIN = "https://solar-modem-496213-f5.web.app"
_PAGE_ID = re.compile(r"^[a-f0-9]{10}$")
_APP_B64 = re.compile(r'const APP_B64 = "([A-Za-z0-9+/=]+)";')
_SHELL_MARKER = '<meta name="generator" content="xiaoning-web-studio-shell-v1">'
_EDGE_CANDIDATES = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)
_SHELL_CSP = (
    "default-src 'none'; img-src data: blob:; media-src data: blob:; "
    "font-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "connect-src 'none'; frame-src 'self'; child-src 'none'; worker-src blob:; "
    "object-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
)


class PublishError(RuntimeError):
    """A user-safe web publishing failure."""


@dataclass(frozen=True)
class PageSnapshot:
    document: bytes | None
    preview: bytes | None


def _sandbox_shell(page_id: str, app_html: str) -> str:
    """Wrap generated code in an opaque iframe with page-scoped persistence."""
    encoded = base64.b64encode(str(app_html).encode("utf-8")).decode("ascii")
    return f'''<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{_SHELL_MARKER}
<meta http-equiv="Content-Security-Policy" content="{_SHELL_CSP}">
<title>小柠网页工具</title>
<style>
html,body{{width:100%;height:100%;margin:0;background:#f8fafc;overflow:hidden}}
#app{{display:block;width:100%;height:100%;border:0;background:#fff}}
#xiaoning-shell-mark{{position:fixed;right:12px;bottom:10px;z-index:2;padding:8px 11px;
border:1px solid #d9e2ec;border-radius:10px;background:#f8fafcee;color:#52606d;
font:12px/1.4 system-ui,sans-serif;pointer-events:none;box-shadow:0 4px 16px #0f172a14}}
</style></head><body>
<iframe id="app" sandbox="allow-scripts allow-forms allow-downloads" allow="clipboard-write; fullscreen" allowfullscreen title="小柠生成的网页工具"></iframe>
<aside id="xiaoning-shell-mark">小柠网页工坊 · 隔离运行 · 请勿输入隐私或支付信息</aside>
<script>
(() => {{
  'use strict';
  const PAGE_ID = '{page_id}';
  const STORE_KEY = 'xn:web:' + PAGE_ID;
  const APP_B64 = "{encoded}";
  const MAX_BYTES = 65536;
  const MAX_KEYS = 64;
  const MAX_KEY = 128;
  const MAX_VALUE = 32768;
  const frame = document.getElementById('app');
  const mark = document.getElementById('xiaoning-shell-mark');
  const utf8 = value => new TextEncoder().encode(value);
  const decode64 = value => new TextDecoder().decode(Uint8Array.from(atob(value), c => c.charCodeAt(0)));
  const encode64 = value => {{
    const bytes = utf8(value); let binary = '';
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return btoa(binary);
  }};
  const normalize = payload => {{
    if (typeof payload !== 'string' || utf8(payload).length > MAX_BYTES) return null;
    let parsed; try {{ parsed = JSON.parse(payload); }} catch {{ return null; }}
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
    const keys = Object.keys(parsed);
    if (keys.length > MAX_KEYS) return null;
    const clean = Object.create(null);
    for (const key of keys) {{
      if (!key || key.length > MAX_KEY || typeof parsed[key] !== 'string' || parsed[key].length > MAX_VALUE) return null;
      clean[key] = parsed[key];
    }}
    const result = JSON.stringify(clean);
    return utf8(result).length <= MAX_BYTES ? result : null;
  }};
  let saved = '{{}}';
  try {{ saved = normalize(localStorage.getItem(STORE_KEY) || '{{}}') || '{{}}'; }} catch {{}}
  if (saved !== '{{}}') mark.textContent += ' · 已恢复本页数据';
  const seed64 = encode64(saved);
  const shim = '<scr' + 'ipt>(()=>{{' +
    "'use strict';const MAX_BYTES=65536,MAX_KEYS=64,MAX_KEY=128,MAX_VALUE=32768;" +
    "const decode64=value=>new TextDecoder().decode(Uint8Array.from(atob(value),c=>c.charCodeAt(0)));" +
    "const state=Object.assign(Object.create(null),JSON.parse(decode64('" + seed64 + "')));" +
    "const size=value=>new TextEncoder().encode(value).length;" +
    "const commit=()=>{{const payload=JSON.stringify(state);if(Object.keys(state).length>MAX_KEYS||size(payload)>MAX_BYTES)throw new DOMException('Storage quota exceeded','QuotaExceededError');parent.postMessage({{v:1,type:'xn-storage',payload}},'*')}};" +
    "const storage={{get length(){{return Object.keys(state).length}},key(i){{return Object.keys(state)[Number(i)]??null}},getItem(k){{k=String(k);return Object.prototype.hasOwnProperty.call(state,k)?state[k]:null}},setItem(k,v){{k=String(k);v=String(v);if(!k||k.length>MAX_KEY||v.length>MAX_VALUE)throw new DOMException('Storage quota exceeded','QuotaExceededError');const had=Object.prototype.hasOwnProperty.call(state,k),old=state[k];state[k]=v;try{{commit()}}catch(e){{if(had)state[k]=old;else delete state[k];throw e}}}},removeItem(k){{k=String(k);if(!Object.prototype.hasOwnProperty.call(state,k))return;const old=state[k];delete state[k];try{{commit()}}catch(e){{state[k]=old;throw e}}}},clear(){{const old=Object.assign(Object.create(null),state);for(const k of Object.keys(state))delete state[k];try{{commit()}}catch(e){{Object.assign(state,old);throw e}}}}}};" +
    "Object.defineProperty(window,'localStorage',{{value:Object.freeze(storage),writable:false,configurable:true}});" +
    "const blocked=()=>{{throw new DOMException('Network and navigation are disabled','SecurityError')}};" +
    "for(const name of ['fetch','XMLHttpRequest','WebSocket','EventSource'])try{{Object.defineProperty(window,name,{{value:blocked,writable:false,configurable:false}})}}catch{{}};" +
    "try{{Object.defineProperty(window,'open',{{value:blocked,writable:false,configurable:false}})}}catch{{}};" +
    "try{{Object.defineProperty(navigator,'sendBeacon',{{value:blocked,writable:false,configurable:false}})}}catch{{}};" +
    "addEventListener('click',e=>{{const a=e.target&&e.target.closest?e.target.closest('a'):null;if(!a)return;const href=a.getAttribute('href')||'';const active=!navigator.userActivation||navigator.userActivation.isActive;const localDownload=active&&a.hasAttribute('download')&&(href.startsWith('blob:')||href.startsWith('data:text/plain')||href.startsWith('data:text/csv')||href.startsWith('data:text/json')||href.startsWith('data:application/json'));if(href&&!href.startsWith('#')&&!localDownload)e.preventDefault()}},true);" +
    "addEventListener('submit',e=>e.preventDefault(),true);" +
    "try{{navigation.addEventListener('navigate',e=>e.preventDefault())}}catch{{}};" +
  '}})();</scr' + 'ipt>';
  let app = decode64(APP_B64);
  const doctype = app.match(/^\\s*<!doctype\\s+html[^>]*>/i);
  app = doctype ? app.slice(0, doctype[0].length) + shim + app.slice(doctype[0].length) : shim + app;
  window.addEventListener('message', event => {{
    const data = event.data;
    if (event.source !== frame.contentWindow || !data || data.v !== 1 || data.type !== 'xn-storage') return;
    const clean = normalize(data.payload);
    if (clean === null) return;
    try {{
      localStorage.setItem(STORE_KEY, clean);
      mark.textContent = '小柠网页工坊 · 隔离运行 · 本页数据已保存';
    }} catch {{
      mark.textContent = '小柠网页工坊 · 隔离运行 · 当前浏览器无法持久保存';
    }}
  }});
  frame.srcdoc = app;
}})();
</script></body></html>'''


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


class FirebasePublisher:
    """Publish only Web Studio's managed public directory to one fixed site."""

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.public_root = self.data_root / "public"
        self.config_path = self.data_root / "firebase.json"
        self._ensure_scaffold()

    @staticmethod
    def _checked_id(page_id: str) -> str:
        value = str(page_id or "").strip().lower()
        if not _PAGE_ID.fullmatch(value):
            raise ValueError("invalid page id")
        return value

    def _ensure_scaffold(self) -> None:
        self.public_root.mkdir(parents=True, exist_ok=True)
        config = {
            "hosting": {
                "site": FIREBASE_SITE,
                "public": "public",
                "ignore": ["firebase.json", "**/.*", "**/node_modules/**"],
                "cleanUrls": False,
                "headers": [
                    {
                        "source": "/x/**",
                        "headers": [
                            {
                                "key": "Content-Security-Policy",
                                "value": _SHELL_CSP,
                            },
                            {
                                "key": "Cache-Control",
                                "value": "no-cache, no-store, must-revalidate, max-age=0",
                            },
                            {"key": "X-Content-Type-Options", "value": "nosniff"},
                            {"key": "X-Frame-Options", "value": "DENY"},
                            {"key": "Referrer-Policy", "value": "no-referrer"},
                            {
                                "key": "Permissions-Policy",
                                "value": (
                                    "camera=(), microphone=(), geolocation=(), payment=(), "
                                    "usb=(), serial=(), bluetooth=()"
                                ),
                            },
                        ],
                    }
                ],
            }
        }
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        home = self.public_root / "index.html"
        if not home.exists():
            home.write_text(
                "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                "<title>小柠网页工坊</title><style>body{font:16px system-ui;"
                "max-width:720px;margin:12vh auto;padding:24px;color:#18212f}"
                "main{padding:32px;border:1px solid #dbe2ea;border-radius:18px;"
                "box-shadow:0 12px 40px #18212f12}</style><main><h1>小柠网页工坊</h1>"
                "<p>这里托管用户通过 QQ 制作的独立网页工具。页面默认离线运行，"
                "不收集账号、密码或支付信息。</p></main></html>",
                encoding="utf-8",
            )

    def page_dir(self, page_id: str) -> Path:
        return self.public_root / "x" / self._checked_id(page_id)

    def page_path(self, page_id: str) -> Path:
        return self.page_dir(page_id) / "index.html"

    def preview_path(self, page_id: str) -> Path:
        return self.data_root / "previews" / f"{self._checked_id(page_id)}.png"

    def stage(self, page_id: str, html: str) -> Path:
        key = self._checked_id(page_id)
        target = self.page_path(key)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".html.tmp")
            temporary.write_text(_sandbox_shell(key, str(html)), encoding="utf-8")
            temporary.replace(target)
            return target
        except OSError as exc:
            raise PublishError("网页文件写入失败") from exc

    def read_app(self, page_id: str) -> str:
        """Return the generated app, including migration support for old raw pages."""
        try:
            document = self.page_path(page_id).read_text(encoding="utf-8")
        except OSError as exc:
            raise PublishError("网页文件读取失败") from exc
        if _SHELL_MARKER not in document and 'id="xiaoning-shell-mark"' not in document:
            return document
        match = _APP_B64.search(document)
        if match is None:
            raise PublishError("网页文件已损坏")
        try:
            return base64.b64decode(match.group(1), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise PublishError("网页文件已损坏") from exc

    def snapshot(self, page_id: str) -> PageSnapshot:
        try:
            path = self.page_path(page_id)
            preview = self.preview_path(page_id)
            return PageSnapshot(
                document=path.read_bytes() if path.is_file() else None,
                preview=preview.read_bytes() if preview.is_file() else None,
            )
        except OSError as exc:
            raise PublishError("网页快照失败") from exc

    def restore(self, page_id: str, snapshot: PageSnapshot | None) -> None:
        directory = self.page_dir(page_id)
        preview = self.preview_path(page_id)
        try:
            if snapshot is None or snapshot.document is None:
                if directory.is_dir():
                    shutil.rmtree(directory)
            else:
                directory.mkdir(parents=True, exist_ok=True)
                target = directory / "index.html"
                temporary = target.with_suffix(".html.tmp")
                temporary.write_bytes(snapshot.document)
                temporary.replace(target)
            if snapshot is None or snapshot.preview is None:
                if preview.is_file():
                    preview.unlink()
            else:
                preview.parent.mkdir(parents=True, exist_ok=True)
                temporary_preview = preview.with_suffix(".png.tmp")
                temporary_preview.write_bytes(snapshot.preview)
                temporary_preview.replace(preview)
        except OSError as exc:
            raise PublishError("网页回滚失败") from exc

    def remove(self, page_id: str) -> PageSnapshot:
        previous = self.snapshot(page_id)
        directory = self.page_dir(page_id)
        preview = self.preview_path(page_id)
        try:
            if preview.is_file():
                preview.unlink()
            if directory.is_dir():
                shutil.rmtree(directory)
            return previous
        except OSError as exc:
            try:
                self.restore(page_id, previous)
            except PublishError:
                pass
            raise PublishError("网页删除失败") from exc

    def render_preview(self, page_id: str) -> Path:
        html_path = self.page_path(page_id).resolve(strict=True)
        output = self.preview_path(page_id)
        output.parent.mkdir(parents=True, exist_ok=True)
        edge = next((path for path in _EDGE_CANDIDATES if path.is_file()), None)
        if edge is None:
            raise PublishError("本机未找到 Edge，无法生成预览图")
        try:
            if output.is_file():
                output.unlink()
        except OSError as exc:
            raise PublishError("网页预览生成失败") from exc
        with tempfile.TemporaryDirectory(prefix="xiaoning-web-preview-") as profile:
            command = [
                str(edge),
                "--headless",
                "--disable-gpu",
                "--disable-background-networking",
                "--host-resolver-rules=MAP * ~NOTFOUND",
                "--hide-scrollbars",
                "--no-first-run",
                f"--user-data-dir={profile}",
                "--window-size=1440,900",
                "--force-device-scale-factor=1.5",
                f"--screenshot={output.resolve()}",
                html_path.as_uri(),
            ]
            try:
                result = subprocess.run(
                    command,
                    cwd=self.data_root,
                    capture_output=True,
                    text=True,
                    timeout=45,
                    creationflags=_creation_flags(),
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise PublishError("网页预览生成失败") from exc
        if result.returncode != 0 or not output.is_file() or output.stat().st_size < 100:
            raise PublishError("网页预览生成失败")
        return output

    @staticmethod
    def _firebase_command(executable: str, args: list[str]) -> list[str]:
        if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
            return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", executable, *args]
        return [executable, *args]

    def _verify_public(self, page_id: str, should_exist: bool) -> bool:
        expected = None
        if should_exist:
            try:
                expected = self.page_path(page_id).read_bytes()
            except OSError:
                return False
        for _ in range(10):
            request = urllib.request.Request(
                f"{self.page_url(page_id)}?xn_verify={time.time_ns()}",
                headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
            )
            try:
                with urllib.request.urlopen(request, timeout=12) as response:
                    body = response.read(2_000_000)
                if should_exist and response.status == 200 and body == expected:
                    return True
                marker = f"const PAGE_ID = '{page_id}';".encode("ascii")
                exists = response.status == 200 and marker in body
                if not should_exist and not exists:
                    return True
            except urllib.error.HTTPError as exc:
                if not should_exist and exc.code == 404:
                    return True
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(1)
        return False

    def deploy(self, page_id: str | None = None, should_exist: bool = True) -> str:
        executable = shutil.which("firebase.cmd" if os.name == "nt" else "firebase")
        if not executable:
            raise PublishError("Firebase 发布工具未安装")
        verify_id = self._checked_id(page_id) if page_id is not None else None
        args = [
            "deploy",
            "--only",
            "hosting",
            "--project",
            FIREBASE_PROJECT,
            "--config",
            str(self.config_path),
            "--non-interactive",
        ]
        command = self._firebase_command(executable, args)
        result = None
        try:
            result = subprocess.run(
                command,
                cwd=self.data_root,
                capture_output=True,
                text=True,
                timeout=240,
                creationflags=_creation_flags(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            if verify_id is None or not self._verify_public(verify_id, should_exist):
                raise PublishError("Firebase 发布超时且未能确认结果") from exc
        except OSError as exc:
            raise PublishError("Firebase 发布失败") from exc
        if result is not None and result.returncode != 0:
            if verify_id is not None and self._verify_public(verify_id, should_exist):
                return PUBLIC_ORIGIN
            raise PublishError("Firebase 发布失败")
        if verify_id is not None and not self._verify_public(verify_id, should_exist):
            raise PublishError("Firebase 发布后校验失败")
        return PUBLIC_ORIGIN

    def page_url(self, page_id: str) -> str:
        return f"{PUBLIC_ORIGIN}/x/{self._checked_id(page_id)}/"
