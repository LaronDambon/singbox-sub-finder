# -*- coding: utf-8 -*-
"""Деплой собранных sing-box конфигов.

Модуль переписан на СПИСОК блоков. Каждый блок — независимый способ
публикации; нет блока — нет деплоя.

    deploy:
      - type: github
        name: main
        repo: "owner/repo"
        token: env:GH_DEPLOY_TOKEN
        read_token: env:GH_READ_TOKEN
        templates: sbc-1.14.json
        path: configs/latest/config.json
      - type: github
        name: second
        repo: "owner/other"
        token: "ghp_..."
        templates: [config/templates/sbc-1.14.json, other.json]
      - type: local
        name: local
        path: output
        templates: config/templates

Ключи блока:
  type        github | local
  name        имя блока (для логов)
  repo        owner/repo (github)
  branch      ветка (github; по умолчанию default_branch)
  token       деплой-токен write; литерал или env:VAR
  read_token  read-only токен для ссылки; литерал или env:VAR
  templates   папка | файл | список файлов | "all"
  path        github: путь файла в репозитории; local: папка/файл
  ip_source   источник текущего IP (файл или http(s))
  ip_placeholder  имя плейсхолдера в шаблоне
  commit_msg  сообщение коммита

Токены можно писать литералом или через env:VAR (или env:VAR:fallback).
Если токен не задан — блок пропускается, а не падает.
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path, PurePosixPath

import requests

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
except Exception:  # noqa: BLE001
    pass

DEFAULT_PATH = "config.json"
TEMPLATE_DIRS = [
    PROJECT_ROOT / "config" / "templates",
    ROOT / "config" / "templates",
    ROOT / "config_template",
    ROOT / "templates",
]


# --------------------------------------------------------------------------- настройки
def _setting(path: str, default: str = "") -> str:
    from config.settings import get_settings
    value = get_settings().setting(path, default)
    return str(value if value is not None else default)


def _env_value(value, default: str = "") -> str:
    """Значение из блока: литерал, env:VAR или env:VAR:fallback."""
    if value is None:
        return default
    s = str(value).strip()
    if s.startswith("env:"):
        rest = s[4:]
        name, _, fallback = rest.partition(":")
        v = os.getenv(name)
        if v is None or v == "":
            v = fallback
        return str(v or "")
    return s


def _deploy_blocks() -> list[dict]:
    """Блоки деплоя из config.yaml. Пустой список — деплой выключен."""
    try:
        from config.settings import get_settings
        raw = get_settings().deploy.blocks
    except Exception:  # noqa: BLE001
        return []
    if isinstance(raw, list):
        return [b for b in raw if isinstance(b, dict)]
    return []


# --------------------------------------------------------------------------- шаблоны
def _resolve_template_named(tpl: str):
    """(имя_файла_шаблона, dict шаблона)."""
    tpl = (tpl or "").strip()
    if not tpl:
        raise ValueError("Укажите шаблон: имя.json, путь или raw-JSON")
    if tpl.startswith("{"):
        return None, json.loads(tpl)
    for folder in TEMPLATE_DIRS:
        if not folder.exists():
            continue
        for file in sorted(folder.glob("*.json")):
            if file.name == tpl or tpl in file.name:
                return file.name, json.loads(file.read_text(encoding="utf-8"))
    p = Path(tpl)
    if p.exists():
        return p.name, json.loads(p.read_text(encoding="utf-8"))
    raise FileNotFoundError(
        f"Шаблон не найден: {tpl}. Доступные: "
        + ", ".join(f.name for folder in TEMPLATE_DIRS if folder.exists()
                    for f in sorted(folder.glob("*.json")))
    )


def _list_templates() -> list[str]:
    out = []
    for folder in TEMPLATE_DIRS:
        if folder.exists():
            out += [f.name for f in sorted(folder.glob("*.json"))]
    return out


def _resolve_templates(templates) -> list[str]:
    """Разворачивает templates: строка/список/папка/файл/all -> имена."""
    if templates is None:
        return []
    if isinstance(templates, str):
        items = [templates]
    else:
        items = list(templates)
    expanded: list[str] = []
    for item in items:
        s = str(item or "").strip()
        if not s:
            continue
        if s.lower() == "all":
            expanded += _list_templates()
            continue
        p = Path(s)
        if p.is_dir():
            expanded += [f.name for f in sorted(p.glob("*.json"))]
            continue
        if p.is_file():
            expanded.append(str(p))
            continue
        # имя шаблона из config/templates
        expanded.append(s)
    out: list[str] = []
    seen: set[str] = set()
    for t in expanded:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _multi_path_for(base_path: str, template_name: str) -> str:
    p = PurePosixPath((base_path or DEFAULT_PATH).replace("\\", "/"))
    if str(p.parent) == ".":
        return template_name
    return str(p.parent / template_name)


# --------------------------------------------------------------------------- IP
def _resolve_server_ip(source: str) -> str:
    s = (source or "").strip()
    if not s:
        raise RuntimeError("Источник IP не задан (ip_source)")
    if s.lower().startswith(("http://", "https://")):
        r = requests.get(s, timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"Не удалось скачать текущий IP: {s} -> HTTP {r.status_code}")
        text = r.text or ""
    else:
        p = Path(s)
        if not p.exists():
            raise FileNotFoundError(f"Файл с текущим IP не найден: {p}")
        text = p.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        ip = line.strip()
        if ip:
            return ip
    raise RuntimeError(f"Источник IP пуст: {s}")


def _apply_server_ip(template_dict, source=None, placeholder=None):
    ph = placeholder if placeholder is not None else "{{SERVER_IP}}"
    ph = (ph or "").strip()
    if not ph:
        return template_dict
    raw = json.dumps(template_dict, ensure_ascii=False)
    if ph not in raw:
        return template_dict
    ip = _resolve_server_ip(source or "")

    def _walk(value):
        if isinstance(value, str):
            return value.replace(ph, ip)
        if isinstance(value, list):
            return [_walk(item) for item in value]
        if isinstance(value, dict):
            return {key: _walk(item) for key, item in value.items()}
        return value

    replaced = _walk(template_dict)
    if ph in json.dumps(replaced, ensure_ascii=False):
        raise RuntimeError(f"Плейсхолдер {ph} не полностью заменён IP в шаблоне")
    return replaced


# --------------------------------------------------------------------------- URI
def _apply_server_filter(uris: list[str], template_dict) -> list[str]:
    """Старый server_filter из шаблона (fallback; SQL-блок его заменяет)."""
    if not isinstance(template_dict, dict):
        return uris
    spec = template_dict.get("server_filter")
    if not spec:
        return uris
    from script.node_filters import build_filter

    flt = build_filter(spec)
    if flt.is_empty:
        return uris
    from config.settings import get_settings
    from script.downloader import normalize_proxy_key
    from script.server_store import ServerStore

    store = ServerStore(get_settings().paths.servers_db_file)
    records = store.server_records_by_key(uris)
    kept = []
    for uri in uris:
        rec = records.get(normalize_proxy_key(uri))
        if rec is not None and flt.matches(rec):
            kept.append(uri)
    if not kept:
        raise RuntimeError(
            "Фильтр server_filter отсёк все серверы. "
            f"Условие: {flt.describe()}."
        )
    return kept


def _load_source_uris(block: dict | None = None) -> list[str]:
    """URI серверов из базы (whitelist) или из файла, если задан file."""
    block = block or {}
    file_path = block.get("file") or block.get("source_file")
    if file_path:
        p = Path(str(file_path))
        if not p.exists():
            raise FileNotFoundError(f"Файл с URI не найден: {p}")
        return [ln.strip() for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    from config.settings import get_settings
    from script.server_store import ServerStore

    store = ServerStore(get_settings().paths.servers_db_file)
    lines = store.export_whitelist_lines(
        global_tag=get_settings().reach.global_tag,
    )
    if not lines:
        raise RuntimeError("В whitelist базы нет серверов. Нечего собирать.")
    return lines


# --------------------------------------------------------------------------- сборка
def _build_config(uris, template_dict, db_path=None) -> dict:
    from gensub_api import build_config_from_uris

    config = build_config_from_uris(uris, template_dict, db_path=db_path)
    outbounds = config.get("outbounds") or []
    real = [o for o in outbounds if isinstance(o, dict) and o.get("type") not in ("selector", "urltest", "direct", "block", "dns")]
    if not real:
        raise RuntimeError("После сборки не осталось реальных outbound-узлов (пустой конфиг).")
    from script.core import validate_config_groups

    validate_config_groups(config, strict=True)
    return config


# --------------------------------------------------------------------------- GitHub
GITHUB_API = "https://api.github.com"


class GitHubDeploy:
    def __init__(self, token: str):
        if not token:
            raise RuntimeError("Токен GitHub не задан.")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "singbox-subs-deploy",
        })

    def _default_branch(self, repo):
        owner, name = repo.split("/", 1)
        r = self.session.get(f"{GITHUB_API}/repos/{owner}/{name}")
        if r.status_code != 200:
            raise RuntimeError(f"Не удаётся получить репозиторий {repo}: {r.status_code} {r.text}")
        return r.json()["default_branch"]

    def _existing_sha(self, repo, path):
        owner, name = repo.split("/", 1)
        r = self.session.get(f"{GITHUB_API}/repos/{owner}/{name}/contents/{path}")
        if r.status_code == 200:
            return r.json().get("sha")
        if r.status_code == 404:
            return None
        r.raise_for_status()

    def is_empty_repo(self, repo) -> bool:
        owner, name = repo.split("/", 1)
        r = self.session.get(f"{GITHUB_API}/repos/{owner}/{name}/branches")
        if r.status_code != 200:
            r.raise_for_status()
        return len(r.json()) == 0

    def init_first_commit(self, repo, branch, path, content, message):
        owner, name = repo.split("/", 1)
        blob = self.session.post(
            f"{GITHUB_API}/repos/{owner}/{name}/git/blobs",
            json={"content": base64.b64encode(content.encode("utf-8")).decode("ascii"), "encoding": "base64"},
        )
        if blob.status_code not in (200, 201):
            raise RuntimeError(f"Не удалось создать blob: {blob.status_code} {blob.text}")
        tree = self.session.post(
            f"{GITHUB_API}/repos/{owner}/{name}/git/trees",
            json={"tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob.json()["sha"]}]},
        )
        if tree.status_code not in (200, 201):
            raise RuntimeError(f"Не удалось создать дерево: {tree.status_code} {tree.text}")
        commit = self.session.post(
            f"{GITHUB_API}/repos/{owner}/{name}/git/commits",
            json={"message": message, "tree": tree.json()["sha"]},
        )
        if commit.status_code not in (200, 201):
            raise RuntimeError(f"Не удалось создать коммит: {commit.status_code} {commit.text}")
        ref = self.session.post(
            f"{GITHUB_API}/repos/{owner}/{name}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": commit.json()["sha"]},
        )
        if ref.status_code not in (200, 201):
            raise RuntimeError(f"Не удалось создать ветку {branch}: {ref.status_code} {ref.text}")
        return commit.json()["sha"]

    def push_file(self, repo, path, content, branch, message):
        owner, name = repo.split("/", 1)
        sha = self._existing_sha(repo, path)
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        r = self.session.put(f"{GITHUB_API}/repos/{owner}/{name}/contents/{path}", json=payload)
        if r.status_code in (200, 201):
            return (r.json().get("commit") or {}).get("sha") or ""
        if r.status_code == 404 and self.is_empty_repo(repo):
            return self.init_first_commit(repo, branch, path, content, message)
        raise RuntimeError(f"Не удалось записать {path}: {r.status_code} {r.text}")


def _build_download_url(token, repo, branch, path) -> str:
    return f"https://{token}:x-oauth-basic@raw.githubusercontent.com/{repo}/{branch}/{path}"


def _verify_url(url, token=""):
    try:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = requests.get(url, timeout=15, headers=headers)
        if r.status_code == 200 and len(r.content) > 0:
            return True, len(r.content)
        return False, f"HTTP {r.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# --------------------------------------------------------------------------- блоки
def _github_block(block: dict, *, silent=False) -> dict:
    name = str(block.get("name") or "github")
    token = _env_value(block.get("token")) or _env_value(block.get("token_env")) or os.getenv("GH_DEPLOY_TOKEN", "")
    read_token = _env_value(block.get("read_token")) or _env_value(block.get("read_token_env")) or os.getenv("GH_READ_TOKEN", "")
    repo = _env_value(block.get("repo")).strip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    branch = _env_value(block.get("branch"))
    path = _env_value(block.get("path"), "config.json")
    commit_msg = _env_value(block.get("commit_msg"), "deploy: updated sing-box config")
    ip_source = _env_value(block.get("ip_source"))
    ip_placeholder = _env_value(block.get("ip_placeholder"), "{{SERVER_IP}}")
    templates = _resolve_templates(block.get("templates"))

    if not token:
        return {"ok": False, "name": name, "error": "deploy token missing", "results": []}
    if not repo:
        return {"ok": False, "name": name, "error": "repo missing", "results": []}
    if not templates:
        return {"ok": False, "name": name, "error": "templates missing", "results": []}

    try:
        gh = GitHubDeploy(token)
        use_branch = branch or gh._default_branch(repo)
        uris = _load_source_uris(block)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "name": name, "error": str(exc), "results": []}

    results = []
    for tpl in templates:
        try:
            tpl_name, tpl_dict = _resolve_template_named(tpl)
            if tpl_name is None:
                raise ValueError("raw-JSON шаблон: нельзя определить имя файла")
            tpl_dict = _apply_server_ip(tpl_dict, ip_source, ip_placeholder)
            tpl_dict = _apply_server_filter(tpl_dict, tpl_dict) if False else tpl_dict
            uris_f = _apply_server_filter(uris, tpl_dict)
            file_path = _multi_path_for(path, tpl_name)
            config = _build_config(uris_f, tpl_dict, db_path=_setting("paths.servers_db_file", ""))
            payload = json.dumps(config, ensure_ascii=False, indent=2)
            commit = gh.push_file(repo, file_path, payload, use_branch, commit_msg or f"deploy: {tpl_name}")
            if read_token:
                url = _build_download_url(read_token, repo, use_branch, file_path)
                ok, info = _verify_url(url)
                url_uses_write = False
            else:
                url = _build_download_url(token, repo, use_branch, file_path)
                ok, info = _verify_url(url)
                url_uses_write = True
            results.append({
                "ok": ok, "name": name, "template": tpl_name, "path": file_path,
                "url": url, "commit_sha": commit, "bytes": len(payload),
                "error": None, "url_uses_write_token": url_uses_write,
            })
        except Exception as exc:  # noqa: BLE001
            results.append({"ok": False, "name": name, "template": tpl, "path": None,
                            "error": str(exc), "url_uses_write_token": False})

    deployed = sum(1 for r in results if r.get("ok"))
    ok = deployed == len(results) and deployed > 0
    return {"ok": ok, "name": name, "repo": repo, "branch": use_branch,
            "deployed": deployed, "failed": len(results) - deployed, "results": results}


def _local_block(block: dict, *, silent=False) -> dict:
    name = str(block.get("name") or "local")
    base = _env_value(block.get("path"), "output")
    ip_source = _env_value(block.get("ip_source"))
    ip_placeholder = _env_value(block.get("ip_placeholder"), "{{SERVER_IP}}")
    templates = _resolve_templates(block.get("templates"))
    if not templates:
        return {"ok": False, "name": name, "error": "templates missing", "results": []}

    try:
        uris = _load_source_uris(block)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "name": name, "error": str(exc), "results": []}

    target = Path(base)
    if target.suffix.lower() != ".json":
        target.mkdir(parents=True, exist_ok=True)

    results = []
    for tpl in templates:
        try:
            tpl_name, tpl_dict = _resolve_template_named(tpl)
            if tpl_name is None:
                raise ValueError("raw-JSON шаблон: нельзя определить имя файла")
            tpl_dict = _apply_server_ip(tpl_dict, ip_source, ip_placeholder)
            uris_f = _apply_server_filter(uris, tpl_dict)
            config = _build_config(uris_f, tpl_dict, db_path=_setting("paths.servers_db_file", ""))
            payload = json.dumps(config, ensure_ascii=False, indent=2)
            out = target / tpl_name if target.suffix.lower() != ".json" else target
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(payload, encoding="utf-8")
            results.append({"ok": True, "name": name, "template": tpl_name,
                            "path": str(out), "bytes": len(payload.encode("utf-8")), "error": None})
        except Exception as exc:  # noqa: BLE001
            results.append({"ok": False, "name": name, "template": tpl,
                            "path": None, "error": str(exc)})

    deployed = sum(1 for r in results if r.get("ok"))
    ok = deployed == len(results) and deployed > 0
    return {"ok": ok, "name": name, "deployed": deployed, "failed": len(results) - deployed,
            "dir": str(target), "results": results}


def _whitelist_block(block: dict, *, silent=False) -> dict:
    """Экспорт живых серверов из базы в файл-список (whitelist).

    Параметры блока:
      path            куда писать (файл; каталог -> whitelist.txt)
      only_available  брать только пинговавшиеся в последней проверке (да)
      min_stable      нижний порог stable (включительно, по умолчанию 0)
      limit           максимум строк (0 = без ограничения)
      tags            дописывать к строкам метки [tag] из server_tags

    Правило простое: сервер жив (available=1), не исключён (excluded=0),
    stable не ниже порога. Никакого отбора по сервисам здесь нет.
    """
    name = str(block.get("name") or "whitelist")
    raw_path = _env_value(block.get("path"), "output/whitelist.txt")
    target = Path(raw_path)
    if target.suffix.lower() not in (".txt", ".list", ""):
        target = target / "whitelist.txt"
    elif target.is_dir() or target.suffix == "":
        target = target / "whitelist.txt"

    only_available = block.get("only_available", True)
    if isinstance(only_available, str):
        only_available = only_available.strip().lower() not in ("0", "false", "no", "off")
    try:
        min_stable = int(block.get("min_stable", 0) or 0)
    except (TypeError, ValueError):
        min_stable = 0
    try:
        limit = int(block.get("limit", 0) or 0)
    except (TypeError, ValueError):
        limit = 0
    with_tags = bool(block.get("tags", False))

    try:
        from config.settings import get_settings
        from script.server_store import ServerStore

        store = ServerStore(get_settings().paths.servers_db_file)
        if with_tags:
            lines = store.export_whitelist_lines(
                global_tag=get_settings().reach.global_tag,
                only_available=bool(only_available),
                min_stable=min_stable,
                limit=limit or None,
            )
        else:
            lines = store.export_lines(
                only_available=bool(only_available),
                min_stable=min_stable - 1 if min_stable else None,
                limit=limit or None,
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "name": name, "path": str(target), "error": str(exc)}

    return {
        "ok": True, "name": name, "path": str(target), "bytes": target.stat().st_size,
        "lines": len(lines), "error": None,
        "results": [{"ok": True, "name": name, "template": "whitelist",
                     "path": str(target), "error": None}],
    }


def run_deploy(blocks=None, *, silent=False, stop_on_error=False) -> dict:
    """Выполняет все блоки деплоя. Возвращает сводку."""
    blocks = _deploy_blocks() if blocks is None else blocks
    if not blocks:
        return {"ok": False, "deployed": 0, "failed": 0, "results": [], "error": "deploy blocks empty"}
    results: list[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "").strip().lower()
        if btype == "github":
            res = _github_block(block, silent=silent)
        elif btype == "local":
            res = _local_block(block, silent=silent)
        elif btype == "whitelist":
            res = _whitelist_block(block, silent=silent)
        else:
            res = {"ok": False, "name": str(block.get("name") or "?"), "error": f"unknown deploy type: {btype}"}
        results.append(res)
        if not silent:
            print(f"Деплой [{res.get('name')}]: " + ("OK" if res.get("ok") else "FAIL: " + str(res.get("error"))))
        if not res.get("ok") and stop_on_error:
            break
    deployed = sum(1 for r in results if r.get("ok"))
    failed = len(results) - deployed
    return {"ok": failed == 0 and deployed > 0, "deployed": deployed, "failed": failed,
            "results": results, "error": None if failed == 0 else "some blocks failed"}


# --------------------------------------------------------------------------- совместимость
def deploy(**kwargs):
    """Совместимость: один github-блок."""
    block = {"type": "github", "name": kwargs.get("name", "github")}
    block.update({k: v for k, v in kwargs.items() if v is not None})
    return _github_block(block)


def local_deploy(**kwargs):
    block = {"type": "local", "name": kwargs.get("name", "local")}
    block.update({k: v for k, v in kwargs.items() if v is not None})
    return _local_block(block)


def deploy_multi(**kwargs):
    return _github_block(kwargs)


def local_deploy_multi(**kwargs):
    return _local_block(kwargs)


def _select_templates_interactive() -> list[str]:
    tpl_list = _list_templates()
    if not tpl_list:
        print("Шаблоны не найдены.", file=sys.stderr)
        return []
    print("Доступные шаблоны:")
    for i, name in enumerate(tpl_list, 1):
        print(f"  {i:>2}. {name}")
    try:
        raw = input("Выберите номера (например: 1 3-5; all): ").strip()
    except (EOFError, KeyboardInterrupt):
        return []
    picked: set[int] = set()
    for token in raw.replace(",", " ").split():
        if token.lower() == "all":
            picked.update(range(1, len(tpl_list) + 1))
        elif "-" in token:
            try:
                lo, hi = token.split("-", 1)
                picked.update(range(int(lo), int(hi) + 1))
            except ValueError:
                pass
        elif token.isdigit():
            n = int(token)
            if 1 <= n <= len(tpl_list):
                picked.add(n)
    return [tpl_list[n - 1] for n in sorted(picked)]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="deploy_config.py", description="Собрать и задеплоить конфиги.")
    parser.add_argument("--list-templates", action="store_true", help="Показать шаблоны")
    parser.add_argument("--select", action="store_true", help="Интерактивный выбор шаблонов")
    parser.add_argument("--templates", nargs="+", help="Шаблоны для локального деплоя")
    parser.add_argument("--local", action="store_true", help="Локальный деплой")
    parser.add_argument("--local-path", default="output", help="Папка локального деплоя")
    parser.add_argument("--token", default="", help="Деплой-токен")
    parser.add_argument("--read-token", default="", help="Read-токен")
    parser.add_argument("--repo", default="", help="owner/repo")
    parser.add_argument("--branch", default="", help="Ветка")
    parser.add_argument("--path", default="config.json", help="Путь файла")
    parser.add_argument("--ip-source", default="", help="Источник IP")
    parser.add_argument("--commit-msg", default="", help="Сообщение коммита")
    args = parser.parse_args(argv)

    if args.list_templates:
        for t in _list_templates():
            print(t)
        return 0
    if args.select:
        chosen = _select_templates_interactive()
        if not chosen:
            return 1
        args.templates = chosen
    if args.local:
        block = {"type": "local", "name": "local", "path": args.local_path,
                 "templates": args.templates or "all", "ip_source": args.ip_source}
        res = _local_block(block)
        return 0 if res.get("ok") else 1
    block = {"type": "github", "name": "github", "repo": args.repo, "branch": args.branch,
             "path": args.path, "token": args.token, "read_token": args.read_token,
             "templates": args.templates or "all", "commit_msg": args.commit_msg,
             "ip_source": args.ip_source}
    res = _github_block(block)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
