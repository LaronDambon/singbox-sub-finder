import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

UTILS = ROOT / "utils"

from flask import Flask, jsonify, request, render_template_string

from config.settings import URLTEST_TEMPLATE, WHITELIST_FILE, FLASK_HOST, FLASK_PORT
from script import core as core_mod

app = Flask(__name__)

HTML_PAGE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>sing-box generator</title>
  <style>
    :root {
      --bg: #f4f7fb;
      --panel: #ffffff;
      --panel-alt: #eef4ff;
      --border: #dfe9f7;
      --text: #1e293b;
      --muted: #5b6b82;
      --primary: #2563eb;
      --primary-strong: #1d4ed8;
      --success: #0f766e;
      --danger: #dc2626;
      --shadow: 0 14px 35px rgba(15, 23, 42, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, "Segoe UI", Arial, sans-serif;
      background: linear-gradient(180deg, #f8fbff 0%, #eef4ff 100%);
      color: var(--text);
    }
    .app {
      max-width: 1200px;
      margin: 0 auto;
      padding: 24px 16px 48px;
    }
    .header {
      margin-bottom: 20px;
    }
    h1 {
      margin: 0 0 8px;
      font-size: clamp(1.8rem, 3vw, 2.6rem);
    }
    .subtitle {
      margin: 0;
      color: var(--muted);
      font-size: 0.98rem;
    }
    .grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 20px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: var(--shadow);
      padding: 18px;
    }
    .panel h2 {
      margin: 0 0 14px;
      font-size: 1.1rem;
    }
    label {
      display: block;
      font-size: 0.9rem;
      margin-bottom: 8px;
      color: var(--muted);
      font-weight: 600;
    }
    textarea, select, button, input[type="file"] {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px 14px;
      font: inherit;
      background: #fff;
      color: var(--text);
    }
    textarea {
      min-height: 220px;
      resize: vertical;
      line-height: 1.45;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-bottom: 12px;
      align-items: center;
    }
    .file-box {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
      margin-bottom: 12px;
    }
    input[type="file"] {
      padding: 8px 10px;
      background: var(--panel-alt);
    }
    button {
      border: none;
      border-radius: 12px;
      cursor: pointer;
      transition: 0.2s ease;
      font-weight: 700;
    }
    .primary {
      background: var(--primary);
      color: white;
      padding: 12px 18px;
    }
    .primary:hover { background: var(--primary-strong); }
    .secondary {
      background: #eef2ff;
      color: var(--primary-strong);
      padding: 10px 14px;
    }
    .download {
      background: var(--success);
      color: white;
      padding: 12px 18px;
      display: none;
    }
    .download.visible { display: inline-block; }
    .status {
      min-height: 22px;
      margin-top: 12px;
      font-size: 0.92rem;
      color: var(--muted);
    }
    .status.error { color: var(--danger); }
    .status.success { color: var(--success); }
    @media (max-width: 840px) {
      .grid { grid-template-columns: 1fr; }
      .panel { padding: 16px; }
      textarea { min-height: 180px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <header class="header">
      <h1>sing-box generator</h1>
      <p class="subtitle">Соберите config из ссылок и шаблона с помощью API</p>
    </header>

    <div class="grid">
      <section class="panel">
        <h2>1. Ссылки</h2>
        <div class="file-box">
          <input id="urlFile" type="file" accept=".txt,text/plain" />
        </div>
        <label for="urlsInput">URI / ссылки / одна ссылка на строку</label>
        <textarea id="urlsInput" placeholder="vless://...&#10;vmess://...&#10;https://..."></textarea>
      </section>

      <section class="panel">
        <h2>2. JSON шаблон</h2>
        <label for="templateSelect">Встроенный шаблон</label>
        <select id="templateSelect">
          {% for item in templates %}
            <option value="{{ item.name }}">{{ item.name }}</option>
          {% endfor %}
        </select>
        <div class="toolbar" style="margin-top: 12px;">
          <button class="secondary" id="loadTemplateBtn" type="button">Загрузить шаблон</button>
        </div>
        <label for="templateInput">Редактирование JSON</label>
        <textarea id="templateInput" placeholder="{ ... }"></textarea>
      </section>
    </div>
    <div class="panel">
      <h2>3. Whitelist</h2>
      <div class="toolbar">
        <button class="secondary" id="loadWhitelistBtn" type="button">Загрузить whitelist</button>
      </div>
      <label for="whitelistPreview">Whitelist (для вставки)</label>
      <textarea id="whitelistPreview" placeholder="Whitelist будет загружен сюда" readonly></textarea>
    </div>

    <div class="panel" style="margin-top: 20px;">
      <div class="toolbar">
        <button class="primary" id="generateBtn" type="button">Generate</button>
        <button class="download" id="downloadBtn" type="button">Download JSON</button>
      </div>
      <div id="status" class="status"></div>
    </div>
  </div>

  <script>
    const urlFile = document.getElementById('urlFile');
    const urlsInput = document.getElementById('urlsInput');
    const templateSelect = document.getElementById('templateSelect');
    const templateInput = document.getElementById('templateInput');
    const whitelistPreview = document.getElementById('whitelistPreview');
    const loadWhitelistBtn = document.getElementById('loadWhitelistBtn');
    const generateBtn = document.getElementById('generateBtn');
    const downloadBtn = document.getElementById('downloadBtn');
    const statusBox = document.getElementById('status');
    let generatedJson = null;

    function setStatus(message, type = '') {
      statusBox.textContent = message;
      statusBox.className = 'status';
      if (type) statusBox.classList.add(type);
    }

    urlFile.addEventListener('change', async (event) => {
      const file = event.target.files[0];
      if (!file) return;
      try {
        const text = await file.text();
        urlsInput.value = text.trim();
        setStatus('Файл TXT загружен');
      } catch (error) {
        setStatus('Ошибка чтения файла: ' + error.message, 'error');
      }
    });

    async function loadTemplate(name) {
      if (!name) return;
      try {
        const response = await fetch('/api/template/' + encodeURIComponent(name));
        if (!response.ok) {
          throw new Error('Template load failed');
        }
        const data = await response.json();
        templateInput.value = JSON.stringify(data, null, 2);
        setStatus('Шаблон загружен');
      } catch (error) {
        setStatus('Не удалось загрузить шаблон: ' + error.message, 'error');
      }
    }

    document.getElementById('loadTemplateBtn').addEventListener('click', () => {
      loadTemplate(templateSelect.value);
    });

    document.getElementById('loadWhitelistBtn').addEventListener('click', async () => {
      try {
        setStatus('Загрузка whitelist...');
        const response = await fetch('/api/whitelist');
        if (!response.ok) {
          throw new Error('Whitelist load failed');
        }
        const data = await response.text();
        whitelistPreview.value = data;
        setStatus('Whitelist загружен');
      } catch (error) {
        setStatus('Не удалось загрузить whitelist: ' + error.message, 'error');
      }
    });

    generateBtn.addEventListener('click', async () => {
      const urls = urlsInput.value
        .split(/\\r?\\n|,/) 
        .map((s) => s.trim())
        .filter(Boolean);

      if (!urls.length) {
        setStatus('Добавьте хотя бы одну ссылку', 'error');
        return;
      }

      let parsedTemplate = null;
      try {
        parsedTemplate = JSON.parse(templateInput.value || '{}');
      } catch (error) {
        setStatus('JSON шаблона невалиден: ' + error.message, 'error');
        return;
      }

      try {
        setStatus('Генерация...');
        const response = await fetch('/gensub', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ uris: urls, template: parsedTemplate })
        });

        const data = await response.json();
        if (!response.ok) {
          throw new Error(data.error || 'Generation failed');
        }

        generatedJson = data;
        templateInput.value = JSON.stringify(parsedTemplate, null, 2);
        const pretty = JSON.stringify(data, null, 2);
        const output = document.createElement('textarea');
        output.value = pretty;
        output.style.width = '100%';
        output.style.minHeight = '240px';
        output.style.marginTop = '12px';
        output.style.borderRadius = '12px';
        output.style.border = '1px solid var(--border)';
        output.style.padding = '12px';
        output.style.resize = 'vertical';
        output.style.fontFamily = 'ui-monospace, SFMono-Regular, monospace';

        const existing = document.getElementById('generatedOutput');
        if (existing) existing.remove();
        output.id = 'generatedOutput';
        generateBtn.parentNode.parentNode.appendChild(output);
        downloadBtn.classList.add('visible');
        setStatus('Готово. Конфиг сгенерирован.', 'success');
      } catch (error) {
        setStatus(error.message, 'error');
      }
    });

    downloadBtn.addEventListener('click', () => {
      if (!generatedJson) {
        setStatus('Сначала сгенерируйте config', 'error');
        return;
      }
      const blob = new Blob([JSON.stringify(generatedJson, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'config.json';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setStatus('Файл загружен', 'success');
    });

    if (templateSelect.value) {
      loadTemplate(templateSelect.value);
    }
  </script>
</body>
</html>
"""


def _load_template_from_payload(payload_template):
    if isinstance(payload_template, dict):
        return payload_template
    if isinstance(payload_template, str):
        if not payload_template.strip():
            raise ValueError("template is empty")
        return json.loads(payload_template)
    if payload_template is None:
        template_path = Path(URLTEST_TEMPLATE)
        if not template_path.exists():
            raise FileNotFoundError(f"Default template not found: {template_path}")
        return json.loads(template_path.read_text(encoding="utf-8"))
    raise ValueError("template must be a JSON object or a JSON string")


def build_config_from_uris(uris, template):
    template_data = _load_template_from_payload(template)
    if not hasattr(core_mod, "providers") or core_mod.providers is None:
        core_mod.providers = {"exclude_protocol": "", "subscribes": []}
    if hasattr(core_mod, "init_parsers"):
        core_mod.init_parsers()

    nodes = []

    for raw_uri in uris:
        if not raw_uri or not str(raw_uri).strip():
            continue
        uri = str(raw_uri).strip()
        parsed_nodes = core_mod.get_nodes(uri)
        if not parsed_nodes:
            continue
        for node in parsed_nodes:
            if isinstance(node, dict):
                nodes.append(node)

    if not nodes:
        return {
            "outbounds": [],
            "warning": "No valid nodes found for the provided uris",
        }

    config = core_mod.build_singbox_config_from_nodes(template_data, nodes)
    return config


def list_builtin_templates():
    candidates = [ROOT / "config" / "templates", ROOT / "config_template", ROOT / "templates"]
    items = []
    for folder in candidates:
        if not folder.exists():
            continue
        for file in sorted(folder.glob("*.json")):
            items.append({"name": file.name})
    return items


@app.route("/")
def index():
    return render_template_string(HTML_PAGE, templates=list_builtin_templates())


@app.route("/api/template/<path:name>")
def api_template(name):
    for folder in [ROOT / "config" / "templates", ROOT / "config_template", ROOT / "templates"]:
        path = folder / name
        if path.exists() and path.is_file():
            return jsonify(json.loads(path.read_text(encoding="utf-8")))
    return jsonify({"error": "template not found"}), 404


@app.route("/api/whitelist")
def api_whitelist():
    whitelist_path = Path(WHITELIST_FILE)
    if not whitelist_path.exists():
        return jsonify({"error": "Whitelist not found"}), 404
    return whitelist_path.read_text(encoding="utf-8")


@app.route("/gensub", methods=["GET", "POST"])
def gensub():
    try:
        if request.method == "GET":
            return jsonify({
                "status": "ok",
                "message": "Use POST with JSON body: {\"uris\": [...], \"template\": {...}}",
                "fields": ["uris", "template"],
            })

        payload = request.get_json(silent=True)
        if payload is None:
            payload = {}
            if request.form:
                payload = request.form.to_dict()
            elif request.args:
                payload = request.args.to_dict()

        uris = payload.get("uris") or payload.get("uri") or payload.get("urls")
        if isinstance(uris, str):
            uris = [line.strip() for line in uris.splitlines() if line.strip()]
        elif uris is None:
            raw = payload.get("data") or payload.get("value")
            if isinstance(raw, str):
                uris = [line.strip() for line in raw.splitlines() if line.strip()]

        if not isinstance(uris, list):
            return jsonify({"error": "JSON/form must contain 'uris' list of subscription links"}), 400

        if not uris:
            return jsonify({"error": "'uris' list is empty"}), 400

        template = payload.get("template")
        config = build_config_from_uris(uris, template)
        return jsonify(config)

    except Exception as exc:  # pragma: no cover - API safety net
        return jsonify({"error": str(exc)}), 400


if __name__ == "__main__":
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False)
