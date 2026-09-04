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

Ключи читаются из переменных окружения GH_DEPLOY_TOKEN и GH_READ_TOKEN
(или параметров --token / --read-token). Токены не сохраняются в файлах проекта.
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path

import requests

# Корень проекта: сам скрипт лежит в singbox-subscribe/
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

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
    from config.settings import SERVERS_DB_FILE, WHITELIST_EXPORT_MIN_STABLE
    from script.server_store import ServerStore

    store = ServerStore(SERVERS_DB_FILE)
    lines = store.export_lines(min_stable=WHITELIST_EXPORT_MIN_STABLE)
    if not lines:
        raise RuntimeError("В whitelist базы нет серверов (stable > порога). Нечего собирать.")
    return lines


# --------------------------------------------------------------------------- шаблон
def _resolve_template(args):
    """Возвращает dict шаблона: по имени из config/templates, по пути или по raw-JSON."""
    tpl = (args.template or "").strip()
    if not tpl:
        raise ValueError("Укажите --template <имя.json | путь | raw-JSON>")

    # 1) если это валидный JSON объект
    if tpl.startswith("{"):
        return json.loads(tpl)

    # 2) имя файла внутри известных папок шаблонов
    for folder in TEMPLATE_DIRS:
        if not folder.exists():
            continue
        # ищем по точному имени или по подстроке
        for file in sorted(folder.glob("*.json")):
            if file.name == tpl or tpl in file.name:
                return json.loads(file.read_text(encoding="utf-8"))

    # 3) путь к файлу
    p = Path(tpl)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))

    raise FileNotFoundError(
        f"Шаблон не найден: {tpl}. Доступные в config/templates: "
        + ", ".join(f.name for f in TEMPLATE_DIRS if f.exists() for f in sorted(f.glob('*.json')))
    )


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
):
    """Программный деплой собранного конфига в GitHub-репозиторий.

    Два разных ключа:
      token      — ДЕПЛОЙ-ключ (write), только для пуша, наружу не выдаётся;
      read_token — READ-ключ (read-only), вшивается в ссылку на скачивание.

    Параметры по умолчанию берутся из переменных окружения / settings:
      GH_DEPLOY_TOKEN, GH_READ_TOKEN. Авто-деплой после цикла проверки:
        from deploy_config import deploy
        deploy(silent=True)
    Возвращает dict: {ok, url, commit_sha, bytes, repo, branch, path, error, url_uses_write_token}.
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
        template_dict = _resolve_template(a)
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


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="deploy_config.py",
        description="Собрать конфиг по шаблону и задеплоить в private GitHub-репозиторий.",
    )
    parser.add_argument("--template", help="Имя шаблона из config/templates (или путь / raw-JSON)")
    parser.add_argument("--repo", default=os.getenv("GH_DEPLOY_REPO", DEFAULT_REPO), help="owner/repo (default: " + DEFAULT_REPO + ")")
    parser.add_argument("--branch", default=None, help="Ветка (по умолчанию default_branch репозитория)")
    parser.add_argument("--path", default=DEFAULT_PATH, help="Путь файла внутри репозитория (default: config.json)")
    parser.add_argument("--token", default="", help="ДЕПЛОЙ-токен write (иначе GH_DEPLOY_TOKEN/GH_TOKEN)")
    parser.add_argument("--read-token", default="", help="READ-токен read-only для скачивания (иначе GH_READ_TOKEN)")
    parser.add_argument("--create", action="store_true", help="Создать репозиторий, если его нет")
    parser.add_argument("--private", action="store_true", default=True, help="Создавать репо приватным (по умолчанию да)")
    parser.add_argument("--source", default="whitelist", help="whitelist|file:<путь>")
    parser.add_argument("--file", default=None, help="Путь к файлу URI при --source file")
    parser.add_argument("--commit-msg", default="deploy: updated sing-box config", help="Сообщение коммита")
    parser.add_argument("--list-templates", action="store_true", help="Показать доступные шаблоны и выйти")
    args = parser.parse_args(argv)

    if args.list_templates:
        print("Доступные шаблоны:")
        for t in _list_templates():
            print("  " + t)
        return 0

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
    )
    if not res.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
