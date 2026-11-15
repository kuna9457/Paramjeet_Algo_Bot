"""Nexus Bot - Release Admin (your side only, never shipped to the client).

Pick a version bump, write release notes, press Build. It runs build.py, which (optionally) runs the tests,
bundles everything and compiles the Inno Setup installer into installer/dist/.
Then press "Open GitHub release page" and upload the .exe + .exe.sha256 files.

Run with:  python installer/release_admin.py   (or double-click Release_Admin.bat)
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

INST = Path(__file__).resolve().parent
ROOT = INST.parent
sys.path.insert(0, str(INST))
import build  # noqa: E402  (version helpers only)


def git(*args) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception as e:  # noqa
        return f"(git error: {e})"


def repo_slug() -> str:
    try:
        return json.loads((ROOT / "release_config.json").read_text(encoding="utf-8")).get("github_repo", "")
    except Exception:  # noqa
        return ""


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Nexus Bot - Release Admin")
        self.geometry("820x720")
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.last_version = ""

        pad = {"padx": 12, "pady": 6}
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        self.cur = tk.StringVar()
        self.gitinfo = tk.StringVar()
        ttk.Label(top, textvariable=self.cur, font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(top, textvariable=self.gitinfo, foreground="#555").pack(anchor="w")

        bf = ttk.LabelFrame(self, text="Version")
        bf.pack(fill="x", **pad)
        self.kind = tk.StringVar(value="patch")
        self.custom = tk.StringVar()
        for text, val in (("Patch (bug fix)", "patch"), ("Minor (new feature)", "minor"), ("Major", "major"),
                          ("Rebuild same version", "same"), ("Custom:", "custom")):
            ttk.Radiobutton(bf, text=text, value=val, variable=self.kind, command=self.refresh_preview).pack(
                side="left", padx=6, pady=6)
        ce = ttk.Entry(bf, textvariable=self.custom, width=10)
        ce.pack(side="left")
        ce.bind("<KeyRelease>", lambda e: self.refresh_preview())
        self.preview = tk.StringVar()
        ttk.Label(bf, textvariable=self.preview, font=("Segoe UI", 10, "bold")).pack(side="right", padx=10)

        nf = ttk.LabelFrame(self, text="Release notes (shown to the client when an update is offered)")
        nf.pack(fill="x", **pad)
        self.notes = tk.Text(nf, height=5, wrap="word")
        self.notes.pack(fill="x", padx=6, pady=6)

        of = ttk.LabelFrame(self, text="Options")
        of.pack(fill="x", **pad)
        self.o_tests = tk.BooleanVar(value=True)
        self.o_smoke = tk.BooleanVar(value=True)
        self.o_rt = tk.BooleanVar(value=False)
        self.o_commit = tk.BooleanVar(value=True)
        self.o_push = tk.BooleanVar(value=False)
        for text, var in (("Run tests first", self.o_tests), ("Smoke-test the bundle", self.o_smoke),
                          ("Re-download Python packages", self.o_rt), ("Git commit + tag after build", self.o_commit),
                          ("Also git push (branch + tags)", self.o_push)):
            ttk.Checkbutton(of, text=text, variable=var).pack(side="left", padx=6, pady=6)

        bt = ttk.Frame(self)
        bt.pack(fill="x", **pad)
        self.btn = ttk.Button(bt, text="Build installer", command=self.start_build)
        self.btn.pack(side="left")
        ttk.Button(bt, text="Open dist folder", command=self.open_dist).pack(side="left", padx=8)
        ttk.Button(bt, text="Open GitHub release page", command=self.open_release).pack(side="left")
        ttk.Button(bt, text="Refresh", command=self.refresh).pack(side="right")

        self.log = scrolledtext.ScrolledText(self, height=18, font=("Consolas", 9), state="disabled")
        self.log.pack(fill="both", expand=True, **pad)
        self.refresh()
        self.after(100, self.pump)

    # ---------------------------------------------------------------- state
    def target_version(self) -> str:
        cur, k = build.read_version(), self.kind.get()
        if k == "same":
            return cur
        if k == "custom":
            return self.custom.get().strip()
        return build.bump(cur, k)

    def refresh(self) -> None:
        self.cur.set(f"Current version: {build.read_version()}")
        changed = [ln for ln in git("status", "--short").splitlines() if ln.strip()]
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        self.gitinfo.set(f"git: branch {branch} | {len(changed)} uncommitted change(s) | repo {repo_slug() or '(not set)'}")
        self.refresh_preview()

    def refresh_preview(self) -> None:
        v = self.target_version()
        self.preview.set(f"-> new version: {v}" if v else "-> enter a version like 1.2.0")

    # ---------------------------------------------------------------- build
    def append(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def start_build(self) -> None:
        if self.busy:
            return
        v = self.target_version()
        if not v or not all(p.isdigit() for p in v.split(".")) or v.count(".") != 2:
            messagebox.showerror("Version", "Version must look like 1.2.3")
            return
        if self.o_push.get() and not messagebox.askyesno(
                "git push", "This will push your commits and the tag to 'origin'.\nContinue?"):
            return
        cmd = [build._console_python(), "-u", str(INST / "build.py")]
        cmd += ["--no-bump"] if self.kind.get() == "same" else ["--version", v]
        if self.o_tests.get():
            cmd.append("--tests")
        if not self.o_smoke.get():
            cmd.append("--no-smoke")
        if self.o_rt.get():
            cmd.append("--rebuild-runtime")
        if self.o_push.get():
            cmd.append("--git-push")
        elif self.o_commit.get():
            cmd.append("--git-commit")
        notes = self.notes.get("1.0", "end").strip()
        if notes:
            nf = Path(tempfile.gettempdir()) / "nexus_release_notes.txt"
            nf.write_text(notes, encoding="utf-8")
            cmd += ["--notes-file", str(nf)]
        self.last_version = v
        self.busy = True
        self.btn.configure(state="disabled", text="Building...")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        threading.Thread(target=self.worker, args=(cmd,), daemon=True).start()

    def worker(self, cmd) -> None:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", env=env, creationflags=flags)
        for line in p.stdout:
            self.q.put(("line", line))
        self.q.put(("done", p.wait()))

    def pump(self) -> None:
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "line":
                    self.append(val)
                else:
                    self.busy = False
                    self.btn.configure(state="normal", text="Build installer")
                    self.refresh()
                    if val == 0:
                        messagebox.showinfo("Build finished",
                                            f"Installer ready in installer/dist:\nNexusBot_Setup_{self.last_version}.exe\n"
                                            "and its .sha256 file.\n\nNext: Open GitHub release page and upload BOTH files.")
                    else:
                        messagebox.showerror("Build failed", "See the log. The version number was left unchanged.")
        except queue.Empty:
            pass
        self.after(100, self.pump)

    # ---------------------------------------------------------------- links
    def open_dist(self) -> None:
        build.DIST.mkdir(exist_ok=True)
        os.startfile(build.DIST)

    def open_release(self) -> None:
        slug = repo_slug()
        if not slug:
            messagebox.showerror("Repo", "Set github_repo in release_config.json")
            return
        v = self.last_version or build.read_version()
        webbrowser.open(f"https://github.com/{slug}/releases/new?tag=v{v}&title=v{v}")


if __name__ == "__main__":
    App().mainloop()
