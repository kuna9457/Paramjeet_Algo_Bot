#!/usr/bin/env python
"""Build the Windows installer for the Nexus SMC Bot.

  python installer/build.py --bump patch            # 1.0.0 -> 1.0.1   (also: minor, major)
  python installer/build.py --version 1.2.0
  python installer/build.py --bump patch --notes-file notes.txt --tests --git-commit [--git-push]
  python installer/build.py --no-bump               # rebuild the current VERSION

Steps: (tests) -> version -> stage app files (no .env/logs/.git) -> embeddable Python + pip packages (cached)
       -> smoke test -> Inno Setup -> SHA-256 -> (git commit + tag).
Output: installer/dist/NexusBot_Setup_<ver>.exe  +  .exe.sha256  (upload BOTH to the GitHub Release).
Trading code is never modified.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

INST = Path(__file__).resolve().parent
ROOT = INST.parent
BUILD = INST / "build"
STAGE = BUILD / "stage"
CACHE = BUILD / "cache"
DIST = INST / "dist"
VERSION_FILE = ROOT / "VERSION"

def _console_python() -> str:
    """pythonw.exe (no console) breaks pytest/subprocess output - always use python.exe for child processes."""
    exe = Path(sys.executable)
    cand = exe.with_name("python.exe")
    return str(cand if exe.name.lower() == "pythonw.exe" and cand.exists() else exe)


PY_VER = "3.11.9"
EMBED_URL = f"https://www.python.org/ftp/python/{PY_VER}/python-{PY_VER}-embed-amd64.zip"
GETPIP_URL = "https://bootstrap.pypa.io/get-pip.py"

# top-level names that must never be shipped to the client
EXCLUDE_TOP = {".git", ".gitignore", ".env", "installer", "logs", "data_cache", "__pycache__", ".pytest_cache",
               "tests", "client_package", "Dockerfile", ".dockerignore", "docker-compose.yml", "docker_start.py",
               "DEPLOY_DOCKER.md", "DEPLOY_RENDER.md", "render.yaml", "run_dashboard.bat", "KILL", "STOP",
               "dashboard.pid", "image.png"}
EXCLUDE_ANY = {"__pycache__", ".pytest_cache", "settings.backup.yaml", "settings.pulled.yaml"}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    log(f"\n=== {msg}")


# ------------------------------------------------------------------ version
def read_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "1.0.0"
    except Exception:  # noqa
        return "1.0.0"


def bump(v: str, kind: str) -> str:
    a, b, c = (int(x) for x in (re.findall(r"\d+", v) + ["0", "0", "0"])[:3])
    return {"major": f"{a + 1}.0.0", "minor": f"{a}.{b + 1}.0", "patch": f"{a}.{b}.{c + 1}"}[kind]


# ------------------------------------------------------------------ helpers
def run(cmd, **kw) -> subprocess.CompletedProcess:
    log("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def download(url: str, dest: Path) -> None:
    log(f"downloading {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "nexus-build"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_iscc() -> str:
    cands = [os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
             r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", r"C:\Program Files\Inno Setup 6\ISCC.exe",
             shutil.which("ISCC") or ""]
    for c in cands:
        if c and Path(c).exists():
            return c
    sys.exit("Inno Setup 6 not found. Install it:  winget install JRSoftware.InnoSetup")


# ------------------------------------------------------------------ steps
def run_tests() -> None:
    step("Running tests")
    run([_console_python(), "-m", "pytest", "-q", "-x"], cwd=ROOT)


def stage_app(version: str) -> None:
    step("Staging app files")
    shutil.rmtree(STAGE / "app", ignore_errors=True)

    def ignore(d, names):
        top = Path(d).resolve() == ROOT
        return [n for n in names if (top and (n in EXCLUDE_TOP or n.endswith(".log"))) or n in EXCLUDE_ANY
                or n.endswith(".pyc")]

    shutil.copytree(ROOT, STAGE / "app", ignore=ignore)
    (STAGE / "app" / "VERSION").write_text(version + "\n", encoding="utf-8")
    n = sum(1 for _ in (STAGE / "app").rglob("*") if _.is_file())
    log(f"{n} files staged")
    assert not (STAGE / "app" / ".env").exists(), "refusing to ship .env"


def make_runtime(rebuild: bool) -> Path:
    """Embeddable Python + pip packages. Cached by requirements hash, so normal builds take seconds."""
    step("Python runtime")
    req = [ln.strip() for ln in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
           if ln.strip() and not ln.strip().lower().startswith(("pytest", "#"))]   # pytest is not needed on the client
    key = hashlib.sha256(("\n".join(req) + PY_VER + "v1").encode()).hexdigest()[:12]
    rt = CACHE / f"runtime-{key}"
    if rebuild:
        shutil.rmtree(rt, ignore_errors=True)
    if (rt / ".complete").exists():
        log(f"using cached runtime ({rt.name})")
        return rt
    shutil.rmtree(rt, ignore_errors=True)
    zpath = CACHE / f"python-{PY_VER}-embed-amd64.zip"
    if not zpath.exists():
        download(EMBED_URL, zpath)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(rt)
    pth = next(rt.glob("python*._pth"))
    txt = pth.read_text(encoding="utf-8").replace("#import site", "import site")
    pth.write_text(txt.rstrip() + "\n..\\app\n", encoding="utf-8")        # app code importable from the embedded Python
    gp = CACHE / "get-pip.py"
    if not gp.exists():
        download(GETPIP_URL, gp)
    py = rt / "python.exe"
    run([py, gp, "--no-warn-script-location"])
    reqfile = CACHE / "requirements.runtime.txt"
    reqfile.write_text("\n".join(req) + "\n", encoding="utf-8")
    run([py, "-m", "pip", "install", "--no-warn-script-location", "--disable-pip-version-check", "-r", reqfile])
    freeze = subprocess.run([str(py), "-m", "pip", "freeze"], capture_output=True, text=True).stdout
    (rt / "freeze.txt").write_text(freeze, encoding="utf-8")
    (rt / ".complete").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    return rt


def copy_runtime(rt: Path) -> None:
    step("Copying runtime into the bundle")
    shutil.rmtree(STAGE / "python", ignore_errors=True)
    shutil.copytree(rt, STAGE / "python", ignore=shutil.ignore_patterns(".complete", "freeze.txt", "__pycache__"))


def smoke_test() -> None:
    step("Smoke test (imports + dashboard starts)")
    py = STAGE / "python" / "python.exe"
    app = STAGE / "app"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    run([py, "-c", "import streamlit, pandas, numpy, yaml, dotenv, requests, pymongo, kiteconnect, plotly; "
                   "import bot.config, bot.live.runner, bot.ui.process, updater, render_start; "
                   "print('imports ok, version', updater.current_version())"], cwd=app, env=env)
    port = "8598"
    p = subprocess.Popen([str(py), "-m", "streamlit", "run", "dashboard.py", "--server.port", port,
                          "--server.address", "127.0.0.1", "--server.headless", "true",
                          "--browser.gatherUsageStats", "false"], cwd=app, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = False
    try:
        for _ in range(90):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=2) as r:
                    if r.status == 200:
                        ok = True
                        break
            except Exception:  # noqa
                time.sleep(1)
    finally:
        p.terminate()
        try:
            p.wait(10)
        except Exception:  # noqa
            p.kill()
    for d in ("logs", "data_cache", "__pycache__"):          # nothing the smoke test created may be shipped
        shutil.rmtree(app / d, ignore_errors=True)
    for pyc in app.rglob("__pycache__"):
        shutil.rmtree(pyc, ignore_errors=True)
    if not ok:
        sys.exit("SMOKE TEST FAILED: dashboard did not become healthy")
    log("dashboard healthy - OK")


def compile_installer(version: str) -> Path:
    step("Compiling installer (Inno Setup)")
    DIST.mkdir(exist_ok=True)
    run([find_iscc(), f"/DAppVersion={version}", "/Q", INST / "installer.iss"], cwd=INST)
    exe = DIST / f"NexusBot_Setup_{version}.exe"
    if not exe.exists():
        sys.exit("installer was not produced")
    (DIST / (exe.name + ".sha256")).write_text(f"{sha256(exe)}  {exe.name}\n", encoding="utf-8")
    return exe


def git_release(version: str, notes: str, push: bool) -> None:
    step("Git commit + tag")

    def git(*a, check=True):
        return subprocess.run(["git", *a], cwd=ROOT, check=check, capture_output=True, text=True)
    tag = f"v{version}"
    if git("tag", "--list", tag).stdout.strip():
        sys.exit(f"git tag {tag} already exists")
    git("add", "-A")
    if git("diff", "--cached", "--quiet", check=False).returncode != 0:
        msg = f"Release {tag}" + (f"\n\n{notes}" if notes else "")
        git("commit", "-m", msg)
        log("committed")
    git("tag", "-a", tag, "-m", f"Release {tag}")
    log(f"tagged {tag}")
    if push:
        r = git("push", "origin", "HEAD", "--tags", check=False)
        log("pushed" if r.returncode == 0 else f"push failed: {r.stderr.strip()}")


# ------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--bump", choices=["patch", "minor", "major"])
    g.add_argument("--version")
    g.add_argument("--no-bump", action="store_true")
    ap.add_argument("--notes-file")
    ap.add_argument("--tests", action="store_true", help="run pytest first")
    ap.add_argument("--no-smoke", action="store_true")
    ap.add_argument("--rebuild-runtime", action="store_true", help="re-download Python packages")
    ap.add_argument("--git-commit", action="store_true", help="commit + tag vX.Y.Z after a successful build")
    ap.add_argument("--git-push", action="store_true", help="also git push (branch + tags)")
    a = ap.parse_args()

    old = read_version()
    new = a.version or (bump(old, a.bump) if a.bump else old)
    if not re.fullmatch(r"\d+\.\d+\.\d+", new):
        sys.exit(f"bad version: {new}")
    notes = Path(a.notes_file).read_text(encoding="utf-8").strip() if a.notes_file else ""
    log(f"Nexus SMC Bot build: {old} -> {new}")

    try:
        if a.tests:
            run_tests()
        VERSION_FILE.write_text(new + "\n", encoding="utf-8")
        stage_app(new)
        copy_runtime(make_runtime(a.rebuild_runtime))
        if not a.no_smoke:
            smoke_test()
        exe = compile_installer(new)
        DIST.mkdir(exist_ok=True)
        (DIST / f"RELEASE_NOTES_{new}.md").write_text(notes or f"Release v{new}", encoding="utf-8")
        if a.git_commit or a.git_push:
            git_release(new, notes, a.git_push)
    except BaseException:
        VERSION_FILE.write_text(old + "\n", encoding="utf-8")        # failed build must not leave a bumped version
        raise

    mb = exe.stat().st_size / 1e6
    step("DONE")
    log(f"Installer : {exe}  ({mb:.0f} MB)")
    log(f"Checksum  : {exe}.sha256")
    log(f"Upload BOTH files to the GitHub Release tagged v{new}.")


if __name__ == "__main__":
    main()
