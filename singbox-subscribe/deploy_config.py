# -*- coding: utf-8 -*-
"""Деплой собранного sing-box конфига в (private) GitHub-репозиторий.

Что делает:
  1. Берёт список подтверждённых серверов из центральной базы (whitelist: stable > порога).
  2. Собирает итоговый конфиг из выбранного шаблона (config/templates/*.json).
  3. Пушит собранный JSON в GitHub-репозиторий через REST API (без локального git).
  4. Печатает https-ссылку на скачивание файла из приватного репозитория
     для приложений с автоматическим обновлением конфига.

«Закрытый» доступ и разделение ключей:
  * репозиторий приватный — файл виден только обладателю токена с доступом к нему;
  * у скрипта ДВА РАЗНЫХ ключа (токена), они не должны совпадать:

       GH_DEPLOY_TOKEN — ключ ДЕПЛОЯ (rightы: Contents write). Только для пуша.
                         НИКОГДА не выдаётся наружу.
       GH_READ_TOKEN   — ключ СКАЧИВАНИЯ (rightы: Contents read, read-only).
                         Именно он вшивается в итоговую ссылку и раздаётся тем,
                         кто должен только читать конфиг. Даже при утечке этот
                         ключ не даёт права на запись/деплой.

Итоговая ссылка на скачивание (raw-файл из приватного репо, токен в ссылке):
  https://<READ_TOKEN>:x-oauth-basic@raw.githubusercontent.com/<owner>/<repo>/<branch>/<path>
  GitHub больше НЕ принимает токен в query (?access_token= -> 400), поэтому
  read-токен вшивается в URL как basic-auth: URL-only клиенты получают сырой конфиг.

Использование:
    GH_DEPLOY_TOKEN=ghp_write GH_READ_TOKEN=ghp_read python deploy_config.py --template sbc-1.14.json
    python deploy_config.py --template sbc-1.14.json --source file --file source/merge.txt --path configs/latest/config.json
    --list-templates — показывает доступные шаблоны.

Мульти-деплой (несколько конфигов за один запуск, по выбранным шаблонам):
    python deploy_config.py --templates sbc-1.14.json sing-box-config-1.13.14-ru.json
    python deploy_config.py --templates all          # все шаблоны из config/templates
    python deploy_config.py --select                 # интерактивный выбор из списка
    Итоговый файл в репозитории называется ИМЕНЕМ ШАБЛОНА: из --path берётся только
    каталог (--path configs/latest/config.json + шаблон sbc-1.14.json ->
    configs/latest/sbc-1.14.json). Список шаблонов можно задать и через env
    DEPLOY_TEMPLATES="a.json, b.json". Сбой одного шаблона не прерывает остальные
    (--stop-on-error — прерывает).

Замена текущего IP сервера в шаблоне (плейсхолдер):
    В шаблоне вместо жёсткого адреса пишется уникальная переменная:
        "address": "{{SERVER_IP}}"
    На этапе деплоя она замещается актуальным IP из источника:
        --ip-source /home/ray/ubuntu_server_ip/current_ip.txt   (или env DEPLOY_IP_SOURCE)
    Источник — путь к локальному файлу (берётся первая непустая строка) либо
    http(s)-ссылка (тело ответа). Имя переменной настраивается env
    DEPLOY_IP_PLACEHOLDER (по умолчанию {{SERVER_IP}}). Если плейсхолдера в
    шаблоне нет, IP не запрашивается и шаблон собирается как есть.

Ключи читаются из переменных окружения GH_DEPLOY_TOKEN и GH_READ_TOKEN
(или параметров --token / --read-token). Токены не сохраняются в файлах проекта.
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path, PurePosixPath

import requests

# Корень проекта: сам скрипт лежит в singbox-subscribe/
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Локальные секреты/настройки из .env в корне репозитория (как в config/env.py):
# GH_DEPLOY_TOKEN, GH_READ_TOKEN, DEPLOY_IP_SOURCE и т.д. override=False —
# переменные системного окружения имеют приоритет.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT.parent / ".env", override=False)
except Exception:  # noqa: BLE001 — пакет dotenv необязателен для CLI
    pass

DEFAULT_REPO = "LaronDambon/sing-box-config"
DEFAULT_PATH = "config.json"
TEMPLATE_DIRS = [ROOT / "config" / "templates", ROOT / "config_template", ROOT / "templates"]


# --------------------------------------------------------------------------- настройки/секреты
def _get_token(args) -> str:
    """ДЕПЛОЙ-токен (write). Из --token, либо env GH_DEPLOY_TOKEN / GH_TOKEN."""
    if getattr(args, "token", None):
        return args.token
    for key in ("GH_DEPLOY_TOKEN", "GH_TOKEN"):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return ""


def _get_read_token(args) -> str:
    """READ-токен для скачивания. Из --read-token, либо env GH_READ_TOKEN.

    Отдельный ключ с read-only правами: он вшивается в ссылку на скачивание
    и раздаётся наружу. Его утечка НЕ даёт прав записи/деплоя.
    """
    if getattr(args, "read_token", None):
        return args.read_token
    val = (os.getenv("GH_READ_TOKEN") or "").strip()
    return val


# --------------------------------------------------------------------------- сбор URI
def _load_source_uris(args) -> list[str]:
    """Возвращает список server-URI для сборки в зависимости от --source."""
    src = (args.source or "whitelist").lower()
    if src in ("file", "file:"):
        path = Path(args.file or args.source.split(":", 1)[1] if ":" in args.source else "")
        if not path.exists():
            raise FileNotFoundError(f"Файл с URI не найден: {path}")
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        return lines

    # Источник по умолчанию — whitelist центральной базы (stable > порога).
    from config.env import SERVERS_DB_FILE, WHITELIST_EXPORT_MIN_STABLE
    from script.server_store import ServerStore

    store = ServerStore(SERVERS_DB_FILE)
    # export_tagged_lines добавляет capability-тэги ([name] / [Global]) к строкам
    # на этапе экспорта для генерации итогового конфига.
    from config.env import REACHABILITY_GLOBAL_TAG
    lines = store.export_tagged_lines(
        min_stable=WHITELIST_EXPORT_MIN_STABLE,
        global_tag=REACHABILITY_GLOBAL_TAG,
    )
    if not lines:
        raise RuntimeError("В whitelist базы нет серверов (stable > порога). Нечего собирать.")
    return lines


# --------------------------------------------------------------------------- шаблон
def _resolve_template_named(tpl: str):
    """Возвращает (имя_файла_шаблона, dict шаблона).

    Имя берётся из найденного файла шаблона — оно же становится именем итогового
    файла в репозитории при мульти-деплое. Для raw-JSON имени нет (None).
    """
    tpl = (tpl or "").strip()
    if not tpl:
        raise ValueError("Укажите --template <имя.json | путь | raw-JSON>")

    # 1) если это валидный JSON объект
    if tpl.startswith("{"):
        return None, json.loads(tpl)

    # 2) имя файла внутри известных папок шаблонов
    for folder in TEMPLATE_DIRS:
        if not folder.exists():
            continue
        # ищем по точному имени или по подстроке
        for file in sorted(folder.glob("*.json")):
            if file.name == tpl or tpl in file.name:
                return file.name, json.loads(file.read_text(encoding="utf-8"))

    # 3) путь к файлу
    p = Path(tpl)
    if p.exists():
        return p.name, json.loads(p.read_text(encoding="utf-8"))

    raise FileNotFoundError(
        f"Шаблон не найден: {tpl}. Доступные в config/templates: "
        + ", ".join(f.name for f in TEMPLATE_DIRS if f.exists() for f in sorted(f.glob('*.json')))
    )


def _resolve_template(args):
    """Возвращает dict шаблона (по имени из config/templates, пути или raw-JSON)."""
    return _resolve_template_named(args.template)[1]


def _env_templates() -> list[str]:
    """Список шаблонов из env DEPLOY_TEMPLATES (имена через запятую или пробел)."""
    raw = (os.getenv("DEPLOY_TEMPLATES") or "").strip()
    if not raw:
        return []
    return [t for t in raw.replace(",", " ").split() if t.strip()]


def _expand_template_list(templates) -> list[str]:
    """Разворачивает список шаблонов: 'all' -> все доступные, с дедупликацией.

    Порядок сохраняется, пустые и дубликаты отбрасываются.
    """
    expanded: list[str] = []
    for t in templates or []:
        s = (t or "").strip()
        if not s:
            continue
        if s.lower() == "all":
            expanded += _list_templates()
        else:
            expanded.append(s)
    out: list[str] = []
    seen: set[str] = set()
    for t in expanded:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _multi_path_for(base_path: str, template_name: str) -> str:
    """Путь итогового файла при мульти-деплое: каталог из base_path + ИМЯ ШАБЛОНА.

    configs/latest/config.json + sbc-1.14.json -> configs/latest/sbc-1.14.json
    config.json (без каталога) + sbc-1.14.json -> sbc-1.14.json
    """
    p = PurePosixPath((base_path or DEFAULT_PATH).replace("\\", "/"))
    if str(p.parent) == ".":
        return template_name
    return str(p.parent / template_name)


# --------------------------------------------------------------------------- текущий IP сервера
def _resolve_server_ip(source: str) -> str:
    """Текущий IP сервера из локального файла или по http(s)-ссылке.

    source — значение DEPLOY_IP_SOURCE / --ip-source:
      * путь к файлу — берётся первая непустая строка
        (например /home/ray/ubuntu_server_ip/current_ip.txt);
      * http(s)-URL — тело ответа (первая непустая строка).
    """
    s = (source or "").strip()
    if not s:
        raise RuntimeError(
            "Источник IP не задан: укажите --ip-source или DEPLOY_IP_SOURCE "
            "(путь к файлу или http(s)-ссылка)."
        )
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
    """Замещает плейсхолдер текущего IP во всех строковых значениях шаблона.

    В шаблоне вместо реального адреса пишется уникальная переменная
    (по умолчанию {{SERVER_IP}}, см. env DEPLOY_IP_PLACEHOLDER), например:
        "address": "{{SERVER_IP}}"
    На этапе деплоя она замещается актуальным IP из DEPLOY_IP_SOURCE / --ip-source.
    Если плейсхолдера в шаблоне нет — шаблон возвращается без изменений и
    источник IP не запрашивается вовсе.
    """
    ph = placeholder if placeholder is not None else os.getenv("DEPLOY_IP_PLACEHOLDER", "{{SERVER_IP}}")
    ph = (ph or "").strip()
    if not ph:
        return template_dict
    raw = json.dumps(template_dict, ensure_ascii=False)
    if ph not in raw:
        return template_dict

    ip = _resolve_server_ip(source if source else os.getenv("DEPLOY_IP_SOURCE", ""))

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


def _list_templates() -> list[str]:
    out = []
    for folder in TEMPLATE_DIRS:
        if folder.exists():
            out += [f.name for f in sorted(folder.glob("*.json"))]
    return out


# --------------------------------------------------------------------------- сборка
def _build_config(uris, template_dict) -> dict:
    from gensub_api import build_config_from_uris

    config = build_config_from_uris(uris, template_dict)
    outbounds = config.get("outbounds") or []
    real = [o for o in outbounds if isinstance(o, dict) and o.get("type") not in ("selector", "urltest", "direct", "block", "dns")]
    if not real:
        raise RuntimeError("После сборки не осталось реальных outbound-узлов (пустой конфиг).")
    return config


# --------------------------------------------------------------------------- GitHub REST
GITHUB_API = "https://api.github.com"


class GitHubDeploy:
    def __init__(self, token: str):
        if not token:
            raise RuntimeError(
                "Токен GitHub не задан. Передайте --token или установите GH_DEPLOY_TOKEN / GH_TOKEN."
            )
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "singbox-subs-deploy",
        })

    def ensure_repo(self, repo, private: bool):
        """Создаёт private-репозиторий, если его ещё нет (owner/repo)."""
        owner, name = repo.split("/", 1)
        r = self.session.get(f"{GITHUB_API}/repos/{owner}/{name}")
        if r.status_code == 200:
            return r.json()["default_branch"]
        if r.status_code != 404:
            r.raise_for_status()
        # создать приватный репозиторий
        cr = self.session.post(f"{GITHUB_API}/user/repos", json={"name": name, "private": private})
        if cr.status_code not in (200, 201):
            raise RuntimeError(f"Не удалось создать репозиторий {repo}: {cr.status_code} {cr.text}")
        return cr.json()["default_branch"]

    def _default_branch(self, repo):
        owner, name = repo.split("/", 1)
        r = self.session.get(f"{GITHUB_API}/repos/{owner}/{name}")
        if r.status_code != 200:
            raise RuntimeError(f"Не удаётся получить информацию о репозитории {repo}: {r.status_code} {r.text}")
        return r.json()["default_branch"]

    def _existing_sha(self, repo, path):
        owner, name = repo.split("/", 1)
        r = self.session.get(f"{GITHUB_API}/repos/{owner}/{name}/contents/{path}")
        if r.status_code == 200:
            return r.json().get("sha")
        if r.status_code == 404:
            return None  # файла ещё нет — создаём
        r.raise_for_status()

    def is_empty_repo(self, repo) -> bool:
        """True, если в репозитории ещё нет ни одной ветки (репо пустой)."""
        owner, name = repo.split("/", 1)
        r = self.session.get(f"{GITHUB_API}/repos/{owner}/{name}/branches")
        if r.status_code != 200:
            r.raise_for_status()
        return len(r.json()) == 0

    def init_first_commit(self, repo, branch: str, path: str, content: str, message: str) -> str:
        """Инициализирует пустой репозиторий первым коммитом через Git Data API
        (blob -> tree -> commit -> refs/heads/<branch>).

        Contents API не может создать первый файл в совершенно пустом репо, поэтому
        дерево и коммит строим вручную. Возвращает sha коммита.
        """
        owner, name = repo.split("/", 1)

        blob = self.session.post(
            f"{GITHUB_API}/repos/{owner}/{name}/git/blobs",
            json={"content": base64.b64encode(content.encode("utf-8")).decode("ascii"), "encoding": "base64"},
        )
        if blob.status_code not in (200, 201):
            raise RuntimeError(f"Не удалось создать blob: {blob.status_code} {blob.text}")
        blob_sha = blob.json()["sha"]

        tree = self.session.post(
            f"{GITHUB_API}/repos/{owner}/{name}/git/trees",
            json={"tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob_sha}]},
        )
        if tree.status_code not in (200, 201):
            raise RuntimeError(f"Не удалось создать дерево: {tree.status_code} {tree.text}")
        tree_sha = tree.json()["sha"]

        commit = self.session.post(
            f"{GITHUB_API}/repos/{owner}/{name}/git/commits",
            json={"message": message, "tree": tree_sha},
        )
        if commit.status_code not in (200, 201):
            raise RuntimeError(f"Не удалось создать коммит: {commit.status_code} {commit.text}")
        commit_sha = commit.json()["sha"]

        ref = self.session.post(
            f"{GITHUB_API}/repos/{owner}/{name}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": commit_sha},
        )
        if ref.status_code not in (200, 201):
            raise RuntimeError(f"Не удалось создать ветку {branch}: {ref.status_code} {ref.text}")
        return commit_sha

    def push_file(self, repo, path, content: str, branch: str, message: str) -> str:
        """Создаёт или обновляет файл по API. Возвращает sha закоммиченного файла.

        Если репозиторий пустой (нет веток), файл кладём через первичную
        инициализацию Git Data API, потому что Contents PUT в пустой репо даёт 404.
        """
        owner, name = repo.split("/", 1)
        sha = self._existing_sha(repo, path)
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        url = f"{GITHUB_API}/repos/{owner}/{name}/contents/{path}"
        r = self.session.put(url, json=payload)
        if r.status_code in (200, 201):
            data = r.json()
            commit_sha = (data.get("commit") or {}).get("sha")
            return commit_sha or ""
        # 404 при попытке записать в пустой репо: инициализируем первым коммитом.
        if r.status_code == 404 and self.is_empty_repo(repo):
            print(f"[Deploy] Репозиторий {repo} пуст — создаём первый коммит (ветка {branch}).")
            return self.init_first_commit(repo, branch, path, content, message)
        raise RuntimeError(f"Не удалось записать {path}: {r.status_code} {r.text}")


def _build_download_url(token, repo, branch, path) -> str:
    """Ссылка на скачивание raw-файла из приватного репозитория (токен в ссылке).

    GitHub больше НЕ принимает токен в query (?access_token= -> 400
    "Must specify access token via Authorization header"), поэтому токен
    вшивается в URL raw.githubusercontent.com как basic-auth:
      https://<TOKEN>:x-oauth-basic@raw.githubusercontent.com/<owner>/<repo>/<branch>/<path>
    Проверено на private-репо: отдаёт сырой файл без заголовков. Наружу идёт
    только read-only GH_READ_TOKEN (Contents read), деплой-токен не вшивается.
    """
    return f"https://{token}:x-oauth-basic@raw.githubusercontent.com/{repo}/{branch}/{path}"


def _verify_url(url, token=""):
    """Проверяет, что по итоговой ссылке файл реально отдаётся (GET).

    Заголовок Authorization добавляется только если передан token: ссылка для
    URL-only клиентов проверяется ровно в том виде, в каком раздаётся.
    """
    try:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = requests.get(url, timeout=15, headers=headers)
        if r.status_code == 200 and len(r.content) > 0:
            return True, len(r.content)
        return False, f"HTTP {r.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


class _AutoArgs:
    """Заглушка параметров для программного вызова (_get_token/_resolve_template/_load_source_uris)."""
    token = ""
    read_token = ""
    template = "sbc-1.14.json"
    source = "whitelist"
    file = None


def deploy(
    *,
    token=None,
    read_token=None,
    repo=None,
    branch=None,
    path=None,
    template=None,
    source=None,
    file=None,
    create_repo=False,
    commit_msg="deploy: updated sing-box config",
    silent=False,
    uris=None,
    ip_source=None,
):
    """Программный деплой собранного конфига в GitHub-репозиторий.

    Два разных ключа:
      token      — ДЕПЛОЙ-ключ (write), только для пуша, наружу не выдаётся;
      read_token — READ-ключ (read-only), вшивается в ссылку на скачивание.

    Параметры по умолчанию берутся из переменных окружения (.env / config.env):
      GH_DEPLOY_TOKEN, GH_READ_TOKEN. Авто-деплой после цикла проверки:
        from deploy_config import deploy
        deploy(silent=True)
    Возвращает dict: {ok, url, commit_sha, bytes, repo, branch, path, error, url_uses_write_token}.
    uris — уже загруженный список URI (внутренний параметр deploy_multi); None — загрузить самому.
    ip_source — источник текущего IP для плейсхолдера в шаблоне (None — env DEPLOY_IP_SOURCE).
    """
    args_auto = _AutoArgs()
    tok = token or _get_token(args_auto)
    read_tok = read_token or _get_read_token(args_auto)
    repo = (repo or os.getenv("GH_DEPLOY_REPO", DEFAULT_REPO)).strip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    path = path or os.getenv("DEPLOY_PATH", DEFAULT_PATH)
    template = template or os.getenv("DEPLOY_TEMPLATE", "sbc-1.14.json")
    source = (source or "whitelist").lower()

    a = _AutoArgs()
    a.template = template
    a.source = source
    a.file = file

    if not tok:
        if not silent:
            print("Деплой пропущен: деплой-токен GitHub не задан (GH_DEPLOY_TOKEN/GH_TOKEN)", file=sys.stderr)
        return {"ok": False, "url": None, "commit_sha": None, "bytes": 0,
                "repo": repo, "branch": branch, "path": path, "error": "deploy token missing",
                "url_uses_write_token": False}

    try:
        gh = GitHubDeploy(tok)
        template_dict = _apply_server_ip(_resolve_template(a), ip_source)
        if uris is None:
            uris = _load_source_uris(a)
        config = _build_config(uris, template_dict)
        payload = json.dumps(config, ensure_ascii=False, indent=2)

        if branch:
            use_branch = branch
        elif create_repo:
            use_branch = gh.ensure_repo(repo, private=True)
        else:
            use_branch = gh._default_branch(repo)

        commit = gh.push_file(repo, path, payload, use_branch, commit_msg)

        # Разделение ключей: в ссылку на скачивание идёт READ-токен (read-only).
        # Если read-токена нет — предупреждаем, что ссылка содержит write-токен.
        if read_tok:
            url = _build_download_url(read_tok, repo, use_branch, path)
            ok, info = _verify_url(url)
            url_uses_write = False
        else:
            url = _build_download_url(tok, repo, use_branch, path)
            ok, info = _verify_url(url)
            url_uses_write = True
            if not silent:
                print("ВНИМАНИЕ: GH_READ_TOKEN не задан — в ссылке лежит ДЕПЛОЙ-токен "
                      "(write). Эту ссылку НЕЛЬЗЯ раздавать наружу.", file=sys.stderr)

        if not silent:
            print("Деплой: " + repo + "/" + path + " (ветка " + use_branch + ", commit " + commit + ")")
            print("Ссылка: " + url)
            print("Проверка: " + ("OK, " + str(info) + " байт" if ok else "сбой: " + str(info)))
        return {"ok": ok, "url": url, "commit_sha": commit, "bytes": len(payload),
                "repo": repo, "branch": use_branch, "path": path, "error": None,
                "url_uses_write_token": url_uses_write}
    except Exception as exc:  # noqa: BLE001
        if not silent:
            print("Ошибка деплоя: " + str(exc), file=sys.stderr)
        return {"ok": False, "url": None, "commit_sha": None, "bytes": 0,
                "repo": repo, "branch": branch, "path": path, "error": str(exc),
                "url_uses_write_token": False}


def deploy_multi(
    *,
    templates=None,
    repo=None,
    branch=None,
    path=None,
    source=None,
    file=None,
    token=None,
    read_token=None,
    create_repo=False,
    commit_msg=None,
    silent=False,
    stop_on_error=False,
    ip_source=None,
):
    """Мульти-деплой: собирает и пушит конфиг для КАЖДОГО шаблона из списка.

    Итоговое имя файла в репозитории = имя файла шаблона (_multi_path_for):
    каталог берётся из path (DEPLOY_PATH/--path), имя файла всегда от шаблона.
    URI-источник загружается один раз и переиспользуется для всех шаблонов;
    ветка репозитория определяется один раз. Сбой одного шаблона не останавливает
    остальные (stop_on_error=True — прерывает после первой ошибки).

    templates — имена шаблонов или 'all'; None -> env DEPLOY_TEMPLATES
    (имена через запятую или пробел).
    Возвращает dict: {ok, deployed, failed, repo, branch, error, results: [...]}.
    """
    repo = (repo or os.getenv("GH_DEPLOY_REPO", DEFAULT_REPO)).strip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    base_path = path or os.getenv("DEPLOY_PATH", DEFAULT_PATH)

    tpl_list = _expand_template_list(templates if templates else _env_templates())
    if not tpl_list:
        if not silent:
            print("Мульти-деплой: список шаблонов пуст — укажите --templates имя.json [...] | all | "
                  "--select или env DEPLOY_TEMPLATES", file=sys.stderr)
        return {"ok": False, "deployed": 0, "failed": 0, "repo": repo, "branch": branch,
                "error": "template list is empty", "results": []}

    a = _AutoArgs()
    a.source = (source or "whitelist").lower()
    a.file = file
    tok = token or _get_token(a)
    read_tok = read_token or _get_read_token(a)

    results: list[dict] = []
    use_branch = branch
    try:
        gh = GitHubDeploy(tok)  # бросит RuntimeError, если токена нет
        uris = _load_source_uris(a)
        if branch:
            use_branch = branch
        elif create_repo:
            use_branch = gh.ensure_repo(repo, private=True)
        else:
            use_branch = gh._default_branch(repo)
    except Exception as exc:  # noqa: BLE001
        if not silent:
            print("Ошибка мульти-деплоя: " + str(exc), file=sys.stderr)
        return {"ok": False, "deployed": 0, "failed": len(tpl_list), "repo": repo,
                "branch": branch, "error": str(exc), "results": []}

    for tpl in tpl_list:
        try:
            tpl_name, _tpl_dict = _resolve_template_named(tpl)
            if tpl_name is None:
                raise ValueError(
                    f"Шаблон '{tpl[:40]}…' — raw-JSON: в мульти-деплое нельзя определить "
                    "имя итогового файла. Используйте именованные шаблоны из config/templates."
                )
            file_path = _multi_path_for(base_path, tpl_name)
            res = deploy(
                token=tok, read_token=read_tok, repo=repo, branch=use_branch,
                path=file_path, template=tpl, source=a.source, file=a.file,
                create_repo=False, commit_msg=commit_msg or f"deploy: updated {tpl_name}",
                silent=True, uris=uris, ip_source=ip_source,
            )
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "url": None, "commit_sha": None, "bytes": 0,
                   "repo": repo, "branch": use_branch, "path": None, "error": str(exc),
                   "url_uses_write_token": False}
        res["template"] = tpl_name or tpl
        results.append(res)
        if not res.get("ok") and stop_on_error:
            break

    deployed = sum(1 for r in results if r.get("ok"))
    failed = len(results) - deployed
    ok = failed == 0 and deployed > 0
    if not silent:
        print(f"Мульти-деплой: {deployed}/{len(results)} успешно — repo {repo}, ветка {use_branch}")
        for r in results:
            mark = "OK  " if r.get("ok") else "FAIL"
            tail = "URL OK" if r.get("ok") else ("сбой: " + str(r.get("error")))
            print(f"  [{mark}] {r.get('template')} -> {r.get('path')} ({tail})")
            if r.get("url"):
                print("      " + r["url"])
        if any(r.get("url_uses_write_token") for r in results):
            print("ВНИМАНИЕ: GH_READ_TOKEN не задан — ссылки содержат ДЕПЛОЙ-токен (write). "
                  "НЕ раздавайте их наружу.", file=sys.stderr)
    return {"ok": ok, "deployed": deployed, "failed": failed, "repo": repo,
            "branch": use_branch, "error": None if ok else f"не задеплоено шаблонов: {failed}",
            "results": results}


def _select_templates_interactive() -> list[str]:
    """Интерактивный выбор шаблонов из списка для мульти-деплоя.

    Понимает: номера через пробел/запятую, диапазоны (2-5), 'all'.
    """
    tpl_list = _list_templates()
    if not tpl_list:
        print("Шаблоны не найдены. Положите *.json в " + str(TEMPLATE_DIRS[0]), file=sys.stderr)
        return []
    print("Доступные шаблоны:")
    for i, name in enumerate(tpl_list, 1):
        print(f"  {i:>2}. {name}")
    print("Выберите номера для мульти-деплоя (например: 1 3-5; 'all' — все):")
    try:
        raw = input("> ").strip()
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
                print(f"  ? пропускаю диапазон: {token}", file=sys.stderr)
        elif token.isdigit():
            n = int(token)
            if 1 <= n <= len(tpl_list):
                picked.add(n)
            else:
                print(f"  ? нет шаблона №{n}", file=sys.stderr)
        else:
            print(f"  ? не понял: {token}", file=sys.stderr)
    return [tpl_list[n - 1] for n in sorted(picked)]


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="deploy_config.py",
        description="Собрать конфиг по шаблону и задеплоить в private GitHub-репозиторий.",
    )
    parser.add_argument("--template", help="Имя шаблона из config/templates (или путь / raw-JSON)")
    parser.add_argument("--repo", default=os.getenv("GH_DEPLOY_REPO", DEFAULT_REPO), help="owner/repo (default: " + DEFAULT_REPO + ")")
    parser.add_argument("--branch", default=None, help="Ветка (по умолчанию default_branch репозитория)")
    parser.add_argument("--path", default=DEFAULT_PATH,
                        help="Путь файла внутри репозитория (default: config.json); в мульти-режиме используется только каталог из него")
    parser.add_argument("--token", default="", help="ДЕПЛОЙ-токен write (иначе GH_DEPLOY_TOKEN/GH_TOKEN)")
    parser.add_argument("--read-token", default="", help="READ-токен read-only для скачивания (иначе GH_READ_TOKEN)")
    parser.add_argument("--create", action="store_true", help="Создать репозиторий, если его нет")
    parser.add_argument("--private", action="store_true", default=True, help="Создавать репо приватным (по умолчанию да)")
    parser.add_argument("--source", default="whitelist", help="whitelist|file:<путь>")
    parser.add_argument("--file", default=None, help="Путь к файлу URI при --source file")
    parser.add_argument("--commit-msg", default=None,
                        help="Сообщение коммита (по умолчанию: 'deploy: updated <имя шаблона>' для мульти-деплоя)")
    parser.add_argument("--list-templates", action="store_true", help="Показать доступные шаблоны и выйти")
    parser.add_argument(
        "--templates", nargs="+", metavar="TPL",
        help="Мульти-деплой: несколько шаблонов через пробел ('all' — все из config/templates). "
             "Итоговый файл называется именем шаблона, каталог берётся из --path",
    )
    parser.add_argument(
        "--select", action="store_true",
        help="Интерактивно выбрать шаблоны из списка и сделать мульти-деплой",
    )
    parser.add_argument(
        "--stop-on-error", action="store_true",
        help="Мульти-деплой: прерваться после первой ошибки (по умолчанию — продолжать остальные)",
    )
    parser.add_argument(
        "--ip-source", default=None,
        help="Источник текущего IP для плейсхолдера в шаблоне: путь к файлу или http(s)-ссылка "
             "(по умолчанию env DEPLOY_IP_SOURCE, например /home/ray/ubuntu_server_ip/current_ip.txt)",
    )
    args = parser.parse_args(argv)

    if args.list_templates:
        print("Доступные шаблоны:")
        for t in _list_templates():
            print("  " + t)
        return 0

    if args.select:
        chosen = _select_templates_interactive()
        if not chosen:
            print("Шаблоны не выбраны — выход.", file=sys.stderr)
            return 1
        args.templates = chosen

    # Мульти-деплой: --templates / --select / env DEPLOY_TEMPLATES.
    multi_templates = args.templates or _env_templates()
    if multi_templates:
        res = deploy_multi(
            templates=multi_templates,
            repo=args.repo,
            branch=args.branch,
            path=args.path,
            source=args.source,
            file=args.file,
            token=args.token,
            read_token=args.read_token,
            create_repo=args.create,
            commit_msg=args.commit_msg,
            silent=False,
            stop_on_error=args.stop_on_error,
            ip_source=args.ip_source,
        )
        return 0 if res.get("ok") else 1

    # Два ключа: token (write) для пуша, read_token (read-only) для ссылки на скачивание.
    res = deploy(
        token=args.token,
        read_token=args.read_token,
        repo=args.repo,
        branch=args.branch,
        path=args.path,
        template=args.template,
        source=args.source,
        file=args.file,
        create_repo=args.create,
        commit_msg=args.commit_msg,
        silent=False,
        ip_source=args.ip_source,
    )
    if not res.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())