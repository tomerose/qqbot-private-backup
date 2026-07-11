"""
UI 开发模式 - 本地预览服务器
提供实时预览和热重载功能，专注于 UI 开发
"""

import json
import os
import sys
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# 添加父目录到路径
CURRENT_DIR = Path(__file__).parent
PROJECT_ROOT = CURRENT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from jinja2 import Template

from core.constant import CARD_TEMPLATES, DEFAULT_TEMPLATE, get_template_path
from dev.mock_data import get_scenario_by_name, get_scenarios_by_category


def get_template(style: str = DEFAULT_TEMPLATE) -> str:
    """从注册表加载指定样式的模板"""
    path = get_template_path(style)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def get_template_options() -> list:
    """获取模板选项列表，用于前端下拉框"""
    options = []
    for tid, info in CARD_TEMPLATES.items():
        options.append(
            {
                "id": tid,
                "name": info["name"],
                "description": info["description"],
            }
        )
    return options


# 开发服务器端口
DEV_PORT = 8765


# ==================== 控制面板 HTML ====================

CONTROL_PANEL_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bilibili Plugin UI Dev Mode</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            min-height: 100vh;
            color: #e8e8e8;
        }

        .container {
            display: flex;
            height: 100vh;
        }

        /* 左侧控制面板 */
        .sidebar {
            width: 320px;
            background: rgba(255, 255, 255, 0.05);
            backdrop-filter: blur(10px);
            border-right: 1px solid rgba(255, 255, 255, 0.1);
            overflow-y: auto;
            padding: 20px;
        }

        .logo {
            text-align: center;
            padding: 20px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
            margin-bottom: 20px;
        }

        .logo h1 {
            font-size: 1.5rem;
            background: linear-gradient(90deg, #fb7299, #ffc0cb);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }

        .logo p {
            font-size: 0.85rem;
            color: #888;
            margin-top: 5px;
        }

        .category {
            margin-bottom: 20px;
        }

        .category-title {
            font-size: 0.9rem;
            color: #fb7299;
            padding: 8px 12px;
            background: rgba(251, 114, 153, 0.1);
            border-radius: 8px;
            margin-bottom: 10px;
            font-weight: 600;
        }

        .scenario-btn {
            display: block;
            width: 100%;
            padding: 10px 15px;
            margin-bottom: 6px;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 8px;
            color: #e8e8e8;
            cursor: pointer;
            text-align: left;
            font-size: 0.85rem;
            transition: all 0.2s ease;
        }

        .scenario-btn:hover {
            background: rgba(251, 114, 153, 0.2);
            border-color: rgba(251, 114, 153, 0.5);
            transform: translateX(5px);
        }

        .scenario-btn.active {
            background: rgba(251, 114, 153, 0.3);
            border-color: #fb7299;
        }

        /* 右侧预览区 */
        .preview-area {
            flex: 1;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        .toolbar {
            display: flex;
            align-items: center;
            gap: 15px;
            padding: 15px 25px;
            background: rgba(0, 0, 0, 0.3);
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        }

        .toolbar-btn {
            padding: 8px 16px;
            background: rgba(251, 114, 153, 0.2);
            border: 1px solid rgba(251, 114, 153, 0.5);
            border-radius: 6px;
            color: #fb7299;
            cursor: pointer;
            font-size: 0.85rem;
            transition: all 0.2s ease;
        }

        .toolbar-btn:hover {
            background: rgba(251, 114, 153, 0.4);
        }

        .style-selector {
            background: rgba(0, 0, 0, 0.3);
            border: 1px solid rgba(255, 255, 255, 0.1);
            color: #e8e8e8;
            padding: 8px 12px;
            border-radius: 6px;
            outline: none;
            font-size: 0.85rem;
        }

        .current-scenario {
            flex: 1;
            font-size: 0.9rem;
            color: #888;
        }

        .current-scenario span {
            color: #ffc0cb;
            font-weight: 600;
        }

        .preview-container {
            flex: 1;
            overflow: auto;
            padding: 30px;
            display: flex;
            justify-content: center;
            align-items: flex-start;
        }

        .preview-frame {
            background: transparent;
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.4);
        }

        .preview-frame iframe {
            border: none;
            display: block;
        }

        /* 数据面板 */
        .data-panel {
            position: fixed;
            right: 0;
            top: 0;
            width: 400px;
            height: 100vh;
            background: rgba(0, 0, 0, 0.9);
            backdrop-filter: blur(10px);
            transform: translateX(100%);
            transition: transform 0.3s ease;
            z-index: 1000;
            display: flex;
            flex-direction: column;
        }

        .data-panel.open {
            transform: translateX(0);
        }

        .data-panel-header {
            padding: 20px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .data-panel-header h3 {
            color: #fb7299;
        }

        .close-btn {
            background: none;
            border: none;
            color: #888;
            font-size: 1.5rem;
            cursor: pointer;
        }

        .close-btn:hover {
            color: #fb7299;
        }

        .data-panel-content {
            flex: 1;
            overflow: auto;
            padding: 20px;
        }

        .data-panel-content pre {
            font-family: 'Consolas', 'Monaco', monospace;
            font-size: 0.8rem;
            line-height: 1.5;
            color: #a8d8a8;
            white-space: pre-wrap;
            word-break: break-all;
        }

        /* 快捷键提示 */
        .shortcuts {
            padding: 15px;
            background: rgba(0, 0, 0, 0.2);
            border-top: 1px solid rgba(255, 255, 255, 0.1);
            font-size: 0.75rem;
            color: #666;
        }

        .shortcuts kbd {
            background: rgba(255, 255, 255, 0.1);
            padding: 2px 6px;
            border-radius: 4px;
            margin: 0 2px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="sidebar">
            <div class="logo">
                <h1>🎬 Bilibili Plugin</h1>
                <p>UI Development Mode</p>
            </div>

            <div id="scenario-list"></div>

            <div class="shortcuts">
                <p><kbd>R</kbd> 刷新预览 | <kbd>D</kbd> 查看数据 | <kbd>↑</kbd><kbd>↓</kbd> 切换场景</p>
            </div>
        </div>

        <div class="preview-area">
            <div class="toolbar">
                <button class="toolbar-btn" onclick="refreshPreview()">🔄 刷新</button>
                <button class="toolbar-btn" onclick="toggleDataPanel()">📊 查看数据</button>
                <button class="toolbar-btn" onclick="openInNewTab()">🔗 新标签打开</button>

                <select id="style-selector" class="style-selector" onchange="refreshPreview()">
                </select>

                <div class="current-scenario">
                    当前场景: <span id="current-name">-</span>
                </div>
            </div>

            <div class="preview-container">
                <div class="preview-frame">
                    <iframe id="preview-iframe" width="720" height="900"></iframe>
                </div>
            </div>
        </div>
    </div>

    <div class="data-panel" id="data-panel">
        <div class="data-panel-header">
            <h3>渲染数据</h3>
            <button class="close-btn" onclick="toggleDataPanel()">&times;</button>
        </div>
        <div class="data-panel-content">
            <pre id="data-content"></pre>
        </div>
    </div>

    <script>
        const scenarios = SCENARIOS_DATA;
        const templateOptions = TEMPLATE_OPTIONS;
        let currentScenario = null;
        let scenarioKeys = [];

        // 初始化样式选择器
        function initStyleSelector() {
            const selector = document.getElementById('style-selector');
            selector.innerHTML = templateOptions.map(opt =>
                `<option value="${opt.id}">${opt.name}</option>`
            ).join('');
        }

        // 初始化场景列表
        function initScenarioList() {
            const container = document.getElementById('scenario-list');
            let html = '';

            for (const [category, names] of Object.entries(scenarios)) {
                html += `<div class="category">`;
                html += `<div class="category-title">${category}</div>`;
                for (const name of names) {
                    scenarioKeys.push(name);
                    html += `<button class="scenario-btn" data-name="${name}" onclick="loadScenario('${name}')">${name.split('_')[1] || name}</button>`;
                }
                html += `</div>`;
            }

            container.innerHTML = html;
        }

        // 加载场景
        function loadScenario(name) {
            currentScenario = name;

            // 更新按钮状态
            document.querySelectorAll('.scenario-btn').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.name === name);
            });

            // 更新当前场景名称
            document.getElementById('current-name').textContent = name;

            // 获取当前样式
            const style = document.getElementById('style-selector').value;

            // 加载预览
            const iframe = document.getElementById('preview-iframe');
            iframe.src = `/render?scenario=${encodeURIComponent(name)}&style=${style}&t=${Date.now()}`;

            // 加载数据
            fetch(`/data?scenario=${encodeURIComponent(name)}`)
                .then(res => res.json())
                .then(data => {
                    document.getElementById('data-content').textContent = JSON.stringify(data, null, 2);
                });
        }

        // 刷新预览
        function refreshPreview() {
            if (currentScenario) {
                loadScenario(currentScenario);
            }
        }

        // 切换数据面板
        function toggleDataPanel() {
            document.getElementById('data-panel').classList.toggle('open');
        }

        // 新标签打开
        function openInNewTab() {
            if (currentScenario) {
                const style = document.getElementById('style-selector').value;
                window.open(`/render?scenario=${encodeURIComponent(currentScenario)}&style=${style}`, '_blank');
            }
        }

        // 键盘快捷键
        document.addEventListener('keydown', (e) => {
            if (e.key === 'r' || e.key === 'R') {
                refreshPreview();
            } else if (e.key === 'd' || e.key === 'D') {
                toggleDataPanel();
            } else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
                e.preventDefault();
                const currentIndex = scenarioKeys.indexOf(currentScenario);
                let newIndex;
                if (e.key === 'ArrowUp') {
                    newIndex = currentIndex > 0 ? currentIndex - 1 : scenarioKeys.length - 1;
                } else {
                    newIndex = currentIndex < scenarioKeys.length - 1 ? currentIndex + 1 : 0;
                }
                loadScenario(scenarioKeys[newIndex]);
            }
        });

        // 初始化
        initStyleSelector();
        initScenarioList();
        if (scenarioKeys.length > 0) {
            loadScenario(scenarioKeys[0]);
        }
    </script>
</body>
</html>
"""


class DevServerHandler(SimpleHTTPRequestHandler):
    """开发服务器请求处理器"""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self.serve_control_panel()
        elif path == "/render":
            scenario_name = query.get("scenario", [""])[0]
            style = query.get("style", [DEFAULT_TEMPLATE])[0]
            self.serve_rendered_template(scenario_name, style)
        elif path == "/data":
            scenario_name = query.get("scenario", [""])[0]
            self.serve_scenario_data(scenario_name)
        else:
            super().do_GET()

    def serve_control_panel(self):
        """提供控制面板页面"""
        categories = get_scenarios_by_category()
        template_options = get_template_options()

        html = CONTROL_PANEL_HTML.replace(
            "SCENARIOS_DATA", json.dumps(categories, ensure_ascii=False)
        ).replace("TEMPLATE_OPTIONS", json.dumps(template_options, ensure_ascii=False))

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def serve_rendered_template(self, scenario_name: str, style: str):
        """提供渲染后的模板"""
        data = get_scenario_by_name(scenario_name)
        if not data:
            self.send_error(404, f"Scenario not found: {scenario_name}")
            return

        template_content = get_template(style)
        template = Template(template_content)
        rendered = template.render(**data)

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(rendered.encode("utf-8"))

    def serve_scenario_data(self, scenario_name: str):
        """提供场景的原始数据"""
        data = get_scenario_by_name(scenario_name)
        if not data:
            self.send_error(404, f"Scenario not found: {scenario_name}")
            return

        # 移除 base64 数据以便查看
        display_data = {
            k: (
                v[:100] + "..."
                if isinstance(v, str) and len(v) > 100 and v.startswith("data:")
                else v
            )
            for k, v in data.items()
        }

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            json.dumps(display_data, ensure_ascii=False, indent=2).encode("utf-8")
        )

    def log_message(self, format, *args):
        """自定义日志格式"""
        print(f"[DevServer] {args[0]}")


def run_dev_server(port: int = DEV_PORT, open_browser: bool = True):
    """启动开发服务器"""
    os.chdir(PROJECT_ROOT)

    server = HTTPServer(("localhost", port), DevServerHandler)
    url = f"http://localhost:{port}"

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║          🎬 Bilibili Plugin UI Dev Server                    ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  服务器已启动: {url:<42} ║
║                                                              ║
║  快捷键:                                                     ║
║    R - 刷新预览                                              ║
║    D - 查看渲染数据                                          ║
║    ↑/↓ - 切换场景                                            ║
║                                                              ║
║  按 Ctrl+C 停止服务器                                        ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
    """)

    if open_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止")
        server.shutdown()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Bilibili Plugin UI Dev Server")
    parser.add_argument("--port", "-p", type=int, default=DEV_PORT, help="服务器端口")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    run_dev_server(port=args.port, open_browser=not args.no_browser)
