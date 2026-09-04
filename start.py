import os
import sys
import time
import subprocess
from pathlib import Path
from dotenv import load_dotenv

# Локальные секреты/настройки из .env (в .gitignore). Загружаем ДО запуска
# дочерних процессов, чтобы main.py и gensub_api.py унаследовали переменные
# (GH_DEPLOY_TOKEN, GH_READ_TOKEN, DEPLOY_ENABLED, ...) из окружения.
# override=False: уже заданные в окружении переменные не перезаписываются.
ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)
API_SCRIPT = ROOT / "singbox-subscribe" / "gensub_api.py"
MAIN_SCRIPT = ROOT / "singbox-subscribe" / "main.py"
CHECK_INTERVAL_SECONDS = 30 * 60


def is_main_running() -> bool:
    if sys.platform.startswith("linux") and Path("/proc").exists():
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == os.getpid():
                continue
            cmdline_path = Path("/proc") / entry / "cmdline"
            try:
                raw = cmdline_path.read_bytes()
            except (FileNotFoundError, PermissionError, OSError):
                continue
            if not raw:
                continue
            if b"main.py" in raw:
                return True
        return False

    try:
        output = subprocess.check_output(["ps", "-eo", "pid,args"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return False

    for line in output.splitlines():
        if "main.py" in line and str(os.getpid()) not in line:
            return True
    return False


def start_process(script_path: Path, name: str) -> subprocess.Popen:
    print(f"[{name}] starting: {script_path}")
    return subprocess.Popen(
        [sys.executable, "-u", str(script_path)],
        cwd=str(ROOT),
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def main() -> int:
    if not API_SCRIPT.exists() or not MAIN_SCRIPT.exists():
        print("Required script files are missing.")
        return 1

    api_process = start_process(API_SCRIPT, "API")
    main_process = None

    if not is_main_running():
        main_process = start_process(MAIN_SCRIPT, "main.py")
    else:
        print("[Scheduler] main.py is already active at startup; initial start skipped.")

    last_check = time.monotonic()
    try:
        while True:
            time.sleep(1)
            if api_process.poll() is not None:
                print("[API] process exited unexpectedly; restarting API.")
                api_process = start_process(API_SCRIPT, "API")

            if time.monotonic() - last_check < CHECK_INTERVAL_SECONDS:
                continue

            last_check = time.monotonic()
            if main_process is not None and main_process.poll() is None:
                print("[Scheduler] main.py is still running; skipping scheduled start.")
                continue

            if is_main_running():
                print("[Scheduler] detected active main.py process; skipping start.")
                main_process = None
                continue

            main_process = start_process(MAIN_SCRIPT, "main.py")
    except KeyboardInterrupt:
        print("[Scheduler] received shutdown signal; terminating child processes.")
    finally:
        for proc, name in ((api_process, "API"), (main_process, "main.py")):
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
