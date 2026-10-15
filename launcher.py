"""
Jackrabbit Launcher - Prototype
===============================
One app that installs, updates and launches the Jackrabbit Games.

* Looks up the latest GitHub release of each game.
* Downloads the right zip for this computer (Windows or Apple Silicon Mac).
* Unpacks it next to the old version, swaps them, then deletes the old version.
* Installed games that are out of date are updated automatically on startup
  (switch it off with the checkbox at the top).
* A game you installed yourself can be linked with "Use existing install". The
  launcher only starts it; it never updates, moves, deletes or duplicates it.

Run:        python launcher.py
Self-test:  python launcher.py --selftest      (no network needed)
Check:      python launcher.py --check         (prints the latest release of each game)

To add a game, add one entry to GAMES below. Nothing else needs to change.
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile

APP_NAME = "JackrabbitLauncher"
VERSION = "0.2.0"   # the release workflow overwrites this from the git tag
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
API_BASE = os.environ.get("JACKRABBIT_API", "https://api.github.com")   # overridden by the tests
API = API_BASE + "/repos/{repo}/releases/latest"
LAUNCHER_REPO = "Moge14/jackrabbit-launcher"   # where new launcher versions are published
HEADERS = {"User-Agent": f"{APP_NAME}/{VERSION}", "Accept": "application/vnd.github+json"}

# "win" and "mac" are the preferred release file names. If a release doesn't
# contain them exactly, the launcher falls back to guessing from the file name.
GAMES = [
    dict(id="tuffjet", name="Tuff Jet", repo="Moge14/tuffjet", accent="#f0d23c",
         tagline="Top-down jet shooter. Dodge debris, shoot down fighters, buy upgrades.",
         win="TuffJet-windows.zip", mac="TuffJet.zip"),
    dict(id="getawaychase", name="Getaway Chase", repo="Moge14/getaway-chase", accent="#e8453c",
         tagline="You're the driver. Lure the cops into roadblocks and ram anyone close.",
         win="GetawayChase-windows.zip", mac="GetawayChase-mac-mseries.zip"),
    dict(id="echo", name="Echo", repo="Moge14/echo-game", accent="#7a5cf0",
         tagline="Puzzle game: cooperate with the ghosts of your past runs.",
         win="Echo-windows.zip", mac="Echo-mac-mseries.zip"),
    dict(id="tankwars", name="Tank Wars", repo="Moge14/tank-wars", accent="#7d9b55",
         tagline="Drive a winding road, destroy everything, upgrade your tank.",
         win="TankWars-windows.zip", mac="TankWars-mac-mseries.zip"),
]


# Where each game saves its best score (the games write these themselves).
# Games with no entry here don't save a score yet, so no score is shown for them.
SCORE_FILES = {
    "getawaychase": "~/.getaway_chase_best",
    "tankwars": "~/.tank_wars_best",
}


# ---------------------------------------------------------------------------
# Files and saved state
# ---------------------------------------------------------------------------
def data_dir():
    override = os.environ.get("JACKRABBIT_HOME")
    if override:
        base = override
    elif IS_WIN:
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME)
    elif IS_MAC:
        base = os.path.join(os.path.expanduser("~/Library/Application Support"), APP_NAME)
    else:
        base = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), APP_NAME)
    os.makedirs(base, exist_ok=True)
    return base


def games_dir():
    p = os.path.join(data_dir(), "games")
    os.makedirs(p, exist_ok=True)
    return p


def game_dir(game):
    return os.path.join(games_dir(), game["id"])


def load_data():
    try:
        with open(os.path.join(data_dir(), "installed.json"), encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("auto_update", True)
        d.setdefault("games", {})
        return d
    except (OSError, ValueError):
        return {"auto_update": True, "games": {}}


def save_data(d):
    path = os.path.join(data_dir(), "installed.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, path)


def cleanup_leftovers():
    """Remove half-finished installs from a crashed or interrupted run."""
    for name in os.listdir(games_dir()):
        if name.endswith((".new", ".old")):
            shutil.rmtree(os.path.join(games_dir(), name), ignore_errors=True)
    shutil.rmtree(os.path.join(data_dir(), "tmp"), ignore_errors=True)


def installed_path(game, record):
    """Full path of the file to launch, or None if it isn't really installed."""
    if not record:
        return None
    if record.get("external"):          # the player's own copy, linked in the launcher
        p = record.get("target", "")
    else:
        p = os.path.join(game_dir(game), record.get("target", ""))
    return p if record.get("target") and os.path.exists(p) else None


def read_best(game):
    """The best score the game saved on this computer, or None."""
    f = SCORE_FILES.get(game["id"])
    if not f:
        return None
    try:
        with open(os.path.expanduser(f)) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def needs_update(record, release):
    """True when a game is installed and GitHub's latest release is a different version."""
    if not record or record.get("external"):     # the player's own copy is never updated
        return False
    return bool(release and record.get("tag") != release.get("tag"))


def newer_launcher(tag):
    """True if a release tag like 'v0.3.0' is newer than this launcher."""
    def nums(t):
        n = [int(x) for x in re.findall(r"\d+", t)]
        while n and n[-1] == 0:     # so 0.2 and 0.2.0 count as the same version
            n.pop()
        return n
    return nums(tag) > nums(VERSION)


def link_existing(game, path):
    """Register a copy the player installed themselves. The launcher only ever starts it:
    it is never updated, moved or deleted, and no second copy is downloaded."""
    path = os.path.abspath(path)
    if path.lower().endswith((".py", ".pyw")):          # a Python game, run with Python
        ok = os.path.isfile(path)
    elif IS_MAC:
        ok = os.path.isdir(path) and path.lower().endswith(".app")
    elif IS_WIN:
        ok = os.path.isfile(path) and path.lower().endswith(".exe")
    else:
        ok = os.path.isfile(path) and os.access(path, os.X_OK)
    if not ok:
        raise RuntimeError("Choose the game's .exe, .app or .py file.")
    return {"external": True, "tag": "", "target": path}


def remove_game(game, record):
    """Delete what the launcher installed. A linked copy of the player's own is never deleted."""
    if not (record and record.get("external")):
        uninstall(game)


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------
def pick_asset(game, assets, windows=IS_WIN):
    want = (game["win"] if windows else game["mac"]).lower()
    for a in assets:
        if a["name"].lower() == want:
            return a
    zips = [a for a in assets if a["name"].lower().endswith(".zip")]
    if windows:
        pool = [a for a in zips if "win" in a["name"].lower()]
    else:
        pool = [a for a in zips if "win" not in a["name"].lower()]
        pool.sort(key=lambda a: "mac" not in a["name"].lower())
    return pool[0] if pool else None


def get_release(repo):
    req = urllib.request.Request(API.format(repo=repo), headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("No release published yet")
        if e.code in (403, 429):
            raise RuntimeError("GitHub rate limit reached, try again later")
        raise RuntimeError(f"GitHub error {e.code}")
    except (OSError, ValueError):
        raise RuntimeError("Can't reach GitHub")
    return data


def fetch_latest(game):
    data = get_release(game["repo"])
    asset = pick_asset(game, data.get("assets", []))
    if not asset:
        raise RuntimeError("This release has no download for your system")
    return dict(tag=data["tag_name"], url=asset["browser_download_url"],
                size=asset.get("size", 0), name=asset["name"])


def check_launcher_update():
    """(tag, page_url) if a newer launcher has been published, else None. Never raises."""
    try:
        data = get_release(LAUNCHER_REPO)
    except RuntimeError:
        return None
    tag = data.get("tag_name", "")
    return (tag, data.get("html_url", "")) if tag and newer_launcher(tag) else None


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------
def download(url, dest, progress):
    req = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(64 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            progress(done, total)


def extract(zip_path, dest):
    if IS_MAC and shutil.which("ditto"):
        # ditto keeps the permissions and symlinks a Mac .app needs
        subprocess.run(["ditto", "-x", "-k", zip_path, dest], check=True)
        return
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            out = z.extract(info, dest)
            mode = info.external_attr >> 16
            if mode & 0o111 and not info.is_dir():
                os.chmod(out, os.stat(out).st_mode | 0o111)


def find_target(root):
    """The thing to launch: an .app on Mac, an .exe on Windows. Shallowest wins."""
    found = []
    for dp, dn, fn in os.walk(root):
        depth = 0 if dp == root else os.path.relpath(dp, root).count(os.sep) + 1
        if IS_MAC:
            apps = [d for d in dn if d.lower().endswith(".app")]
            found += [(depth, d.lower(), os.path.relpath(os.path.join(dp, d), root)) for d in apps]
            dn[:] = [d for d in dn if d not in apps]
        else:
            for f in fn:
                p = os.path.join(dp, f)
                if IS_WIN:
                    low = f.lower()
                    ok = low.endswith(".exe") and not any(w in low for w in ("unins", "crash", "redist", "setup"))
                else:
                    ok = os.access(p, os.X_OK)
                if ok:
                    found.append((depth, f.lower(), os.path.relpath(p, root)))
    if not found:
        raise RuntimeError("Couldn't find the game inside the download")
    return sorted(found)[0][2]


def install(game, release, progress=lambda d, t: None, status=lambda s: None):
    """Download and unpack into a staging folder, then swap it in and delete the old
    version. If anything fails before the swap, the installed game is untouched."""
    final = game_dir(game)
    staging, old = final + ".new", final + ".old"
    tmp = os.path.join(data_dir(), "tmp")
    os.makedirs(tmp, exist_ok=True)
    zip_path = os.path.join(tmp, f"{game['id']}.zip")
    for p in (staging, old):
        shutil.rmtree(p, ignore_errors=True)
    try:
        status("Downloading...")
        download(release["url"], zip_path, progress)
        status("Unpacking...")
        os.makedirs(staging)
        extract(zip_path, staging)
        target = find_target(staging)
        if IS_MAC:  # not set by our own download, but harmless if it ever is
            subprocess.run(["xattr", "-dr", "com.apple.quarantine", staging], capture_output=True)
        status("Replacing old version...")
        had_old = os.path.exists(final)
        if had_old:
            os.replace(final, old)
        try:
            os.replace(staging, final)
        except OSError:
            if had_old:
                os.replace(old, final)   # put the old version back
            raise
        shutil.rmtree(old, ignore_errors=True)
        return target
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            os.remove(zip_path)
        except OSError:
            pass


def uninstall(game):
    shutil.rmtree(game_dir(game), ignore_errors=True)


def python_for_scripts():
    """A Python to run linked .py games with. Inside the packaged launcher, sys.executable
    is the launcher itself, so there we look for a real Python on the PATH instead."""
    if not getattr(sys, "frozen", False):
        exe = sys.executable
        if IS_WIN:      # pythonw runs the game without a black console window
            w = os.path.join(os.path.dirname(exe), "pythonw.exe")
            return w if os.path.exists(w) else exe
        return exe
    for name in (("pythonw", "python", "py") if IS_WIN else ("python3", "python")):
        found = shutil.which(name)
        if found:
            return found
    return None


def launch(game, record):
    path = installed_path(game, record)
    if not path:
        raise RuntimeError("Game files are missing, reinstall it")
    if path.lower().endswith((".py", ".pyw")):
        py = python_for_scripts()
        if not py:
            raise RuntimeError("Python wasn't found on this computer, and it's needed to run .py games.")
        return subprocess.Popen([py, path], cwd=os.path.dirname(path))
    if IS_MAC:
        return subprocess.Popen(["open", path])
    flags = 0x00000008 if IS_WIN else 0   # DETACHED_PROCESS
    return subprocess.Popen([path], cwd=os.path.dirname(path), creationflags=flags)


# ---------------------------------------------------------------------------
# Self-test (no network, no display)
# ---------------------------------------------------------------------------
def selftest():
    import pathlib
    os.environ["JACKRABBIT_HOME"] = tempfile.mkdtemp()
    cleanup_leftovers()

    # release file name matching, for every game on both platforms
    for g in GAMES:
        names = [g["win"], g["mac"], "source.zip", "notes.txt"]
        assets = [dict(name=n) for n in names]
        assert pick_asset(g, assets, windows=True)["name"] == g["win"], g["id"]
        assert pick_asset(g, assets, windows=False)["name"] == g["mac"], g["id"]
        odd = [dict(name="Thing-win64.zip"), dict(name="Thing-macos-arm.zip")]
        assert pick_asset(g, odd, windows=True)["name"] == "Thing-win64.zip"
        assert pick_asset(g, odd, windows=False)["name"] == "Thing-macos-arm.zip"

    # install v1, update to v2, make sure the old files are gone
    def make_zip(path, marker):
        with zipfile.ZipFile(path, "w") as z:
            if IS_WIN:
                z.writestr("Game/Game.exe", "exe")
                z.writestr(f"Game/{marker}.txt", marker)
            elif IS_MAC:
                z.writestr(zipfile.ZipInfo("Game.app/Contents/MacOS/game"), "x")
                z.writestr(f"Game.app/{marker}.txt", marker)
            else:
                info = zipfile.ZipInfo("Game/run.sh")
                info.external_attr = 0o755 << 16
                z.writestr(info, "#!/bin/sh\n")
                z.writestr(f"Game/{marker}.txt", marker)

    g = GAMES[0]
    work = tempfile.mkdtemp()
    for n, marker in enumerate(("v1", "v2"), 1):
        zp = os.path.join(work, f"{n}.zip")
        make_zip(zp, marker)
        seen = []
        target = install(g, dict(url=pathlib.Path(zp).as_uri()), progress=lambda d, t: seen.append(d))
        assert seen and os.path.exists(os.path.join(game_dir(g), target)), target
        files = [f for _, _, fs in os.walk(game_dir(g)) for f in fs]
        assert f"{marker}.txt" in files, files
        other = "v1" if marker == "v2" else "v2"
        assert f"{other}.txt" not in files, "old version was not deleted"
        assert not os.path.exists(game_dir(g) + ".old") and not os.path.exists(game_dir(g) + ".new")

    # a broken download must leave the installed version alone
    bad = os.path.join(work, "bad.zip")
    with open(bad, "w") as f:
        f.write("not a zip")
    try:
        install(g, dict(url=pathlib.Path(bad).as_uri()))
        raise AssertionError("bad zip should have failed")
    except Exception as e:
        assert not isinstance(e, AssertionError), e
    files = [f for _, _, fs in os.walk(game_dir(g)) for f in fs]
    assert "v2.txt" in files, "failed update damaged the installed game"
    assert not os.path.exists(game_dir(g) + ".new")

    # saved state round trip
    save_data({"auto_update": False, "games": {g["id"]: {"tag": "v2", "target": target}}})
    assert load_data()["games"][g["id"]]["tag"] == "v2"
    uninstall(g)
    assert not os.path.exists(game_dir(g))
    print("ok: asset matching, install, update (old version deleted), failed update is safe, state saved")


def check():
    for g in GAMES:
        try:
            r = fetch_latest(g)
            print(f"{g['name']:14} {r['tag']:10} {r['name']} ({r['size'] // 1024} KB)")
        except RuntimeError as e:
            print(f"{g['name']:14} {e}")


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------
BG, CARD, INK, MUTED = "#0e0f12", "#171a21", "#e8ebf2", "#98a0b5"


def run_app():
    import tkinter as tk
    import webbrowser
    from tkinter import filedialog, messagebox, ttk

    FONT = "Segoe UI" if IS_WIN else "Helvetica"

    class Btn(tk.Label):
        """A flat button made from a label, so colours work the same on Windows and Mac."""
        def __init__(self, master, color, command):
            super().__init__(master, font=(FONT, 12, "bold"), padx=20, pady=8, width=9)
            self.color, self.command, self.enabled = color, command, True
            self.bind("<Button-1>", lambda e: self.enabled and self.command())

        def set(self, text, enabled=True):
            self.enabled = enabled
            self.config(text=text, bg=self.color if enabled else "#2a2e38",
                        fg="#101216" if enabled else "#6c7386", cursor="hand2" if enabled else "arrow")

    class Row:
        def __init__(self, app, parent, game):
            self.game = game
            self.frame = tk.Frame(parent, bg=CARD)
            self.frame.pack(fill="x", pady=6)
            tk.Frame(self.frame, bg=game["accent"], width=6).pack(side="left", fill="y")
            body = tk.Frame(self.frame, bg=CARD)
            body.pack(side="left", fill="both", expand=True, padx=16, pady=12)
            right = tk.Frame(body, bg=CARD)
            right.pack(side="right", padx=(12, 0))
            left = tk.Frame(body, bg=CARD)
            left.pack(side="left", fill="x", expand=True)
            tk.Label(left, text=game["name"], bg=CARD, fg=INK, font=(FONT, 17, "bold")).pack(anchor="w")
            tk.Label(left, text=game["tagline"], bg=CARD, fg=MUTED, font=(FONT, 10),
                     wraplength=360, justify="left").pack(anchor="w")
            self.status = tk.Label(left, bg=CARD, fg=INK, font=(FONT, 10), anchor="w")
            self.status.pack(anchor="w", pady=(6, 0))
            self.best = tk.Label(left, bg=CARD, fg=game["accent"], font=(FONT, 10, "bold"), anchor="w")
            self.bar = ttk.Progressbar(left, style="J.Horizontal.TProgressbar", maximum=100)
            self.btn = Btn(right, game["accent"], lambda: app.on_click(game["id"]))
            self.btn.pack()
            self.remove = tk.Label(right, text="Remove", bg=CARD, fg=MUTED, font=(FONT, 9, "underline"), cursor="hand2")
            self.remove.bind("<Button-1>", lambda e: app.on_remove(game["id"]))
            self.link = tk.Label(right, text="Use existing install", bg=CARD, fg="#50a0f0",
                                 font=(FONT, 9, "underline"), cursor="hand2")
            self.link.bind("<Button-1>", lambda e: app.on_link(game["id"]))

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(f"Jackrabbit Launcher {VERSION}")
            self.configure(bg=BG)
            self.geometry("700x640")
            self.minsize(620, 560)
            cleanup_leftovers()
            self.data = load_data()
            self.latest, self.errors = {}, {}
            self.checking, self.busy = set(), set()
            self.procs = {}
            self.q = queue.Queue()

            style = ttk.Style(self)
            style.theme_use("clam")
            style.configure("J.Horizontal.TProgressbar", troughcolor="#0e0f12", background="#50a0f0",
                            bordercolor=CARD, lightcolor="#50a0f0", darkcolor="#50a0f0")

            top = tk.Frame(self, bg=BG)
            top.pack(fill="x", padx=20, pady=(18, 6))
            tk.Label(top, text="JACKRABBIT GAMES", bg=BG, fg=MUTED, font=("Courier", 13, "bold")).pack(side="left")
            self.auto = tk.BooleanVar(value=self.data["auto_update"])
            tk.Checkbutton(top, text="Update automatically", variable=self.auto, command=self.save_auto,
                           bg=BG, fg=INK, selectcolor=CARD, activebackground=BG, activeforeground=INK,
                           font=(FONT, 10), highlightthickness=0, bd=0).pack(side="right")
            check = tk.Label(top, text="Check for updates", bg=BG, fg="#50a0f0", font=(FONT, 10, "underline"), cursor="hand2")
            check.pack(side="right", padx=18)
            check.bind("<Button-1>", lambda e: self.check_all())

            # Shown only when a newer launcher has been published
            self.banner = tk.Label(self, bg="#2a2410", fg="#f0d23c", font=(FONT, 11, "bold"), pady=7, cursor="hand2")

            self.holder = tk.Frame(self, bg=BG)
            self.holder.pack(fill="both", expand=True, padx=20)
            self.rows = {g["id"]: Row(self, self.holder, g) for g in GAMES}
            tk.Label(self, text=f"Games are stored in {games_dir()}", bg=BG, fg="#5d6475",
                     font=(FONT, 9)).pack(pady=(4, 12))

            for gid in self.rows:
                self.refresh(gid)
            self.after(100, self.pump)
            self.after(2000, self.tick_scores)
            self.check_all()
            threading.Thread(target=lambda: self.q.put(("launcher", "", check_launcher_update(), None)),
                             daemon=True).start()

        # -- helpers ----------------------------------------------------------
        def game(self, gid):
            return next(g for g in GAMES if g["id"] == gid)

        def save_auto(self):
            self.data["auto_update"] = self.auto.get()
            save_data(self.data)
            if self.auto.get():
                self.check_all()

        def running(self, gid):
            p = self.procs.get(gid)
            return p is not None and p.poll() is None

        def is_outdated(self, gid):
            return needs_update(self.data["games"].get(gid), self.latest.get(gid))

        # -- showing state ----------------------------------------------------
        def show_best(self, gid):
            row, best = self.rows[gid], read_best(self.game(gid))
            if best is None:
                row.best.pack_forget()
            else:
                row.best.config(text=f"Best score: {best:,}")
                row.best.pack(anchor="w", after=row.status)

        def tick_scores(self):
            """A game saves its score while it runs, so keep the cards current."""
            for gid in self.rows:
                self.show_best(gid)
            self.after(2000, self.tick_scores)

        def show_launcher_update(self, tag, url):
            self.banner.config(text=f"Launcher {tag} is available. Click here to download it.")
            self.banner.bind("<Button-1>", lambda e: webbrowser.open(url))
            self.banner.pack(fill="x", padx=20, before=self.holder)

        def refresh(self, gid):
            g, row = self.game(gid), self.rows[gid]
            rec = self.data["games"].get(gid)
            rel = self.latest.get(gid)
            installed = installed_path(g, rec) is not None
            self.show_best(gid)
            row.remove.pack_forget()
            row.link.pack_forget()
            if gid in self.busy:
                row.btn.set("Working...", False)
                row.bar.pack(fill="x", pady=(6, 0))
                return
            row.bar.pack_forget()
            if not installed:
                row.link.pack(pady=(6, 0))
            if installed:
                row.remove.pack(pady=(6, 0))
                row.remove.config(text="Unlink" if rec.get("external") else "Remove")
                if rec.get("external"):
                    row.status.config(text=f"Using your own copy ({os.path.basename(rec['target'])}). "
                                           "The launcher won't change it.", fg=INK)
                    row.btn.set("Play")
                elif self.is_outdated(gid):
                    row.status.config(text=f"Installed {rec['tag']}, update to {rel['tag']} available", fg="#f0d23c")
                    row.btn.set("Update")
                else:
                    note = "up to date" if rel else (self.errors.get(gid) or "checking...") if gid in self.errors or gid in self.checking else "not checked yet"
                    row.status.config(text=f"Installed {rec['tag']}, {note}", fg=INK)
                    row.btn.set("Play")
            elif rel:
                mb = f" ({rel['size'] / 1048576:.0f} MB)" if rel["size"] else ""
                row.status.config(text=f"Not installed. Latest is {rel['tag']}{mb}", fg=INK)
                row.btn.set("Install")
            elif gid in self.checking:
                row.status.config(text="Checking for the latest version...", fg=MUTED)
                row.btn.set("Install", False)
            else:
                row.status.config(text=self.errors.get(gid, "Not installed"), fg="#e8453c")
                row.btn.set("Retry")

        # -- actions ----------------------------------------------------------
        def check_all(self):
            for gid in self.rows:
                if gid not in self.checking and gid not in self.busy:
                    self.check_one(gid)

        def check_one(self, gid):
            self.checking.add(gid)
            self.errors.pop(gid, None)
            self.refresh(gid)

            def work():
                try:
                    self.q.put(("latest", gid, fetch_latest(self.game(gid)), None))
                except RuntimeError as e:
                    self.q.put(("latest", gid, None, str(e)))
            threading.Thread(target=work, daemon=True).start()

        def on_click(self, gid):
            g = self.game(gid)
            rec = self.data["games"].get(gid)
            if installed_path(g, rec) and not self.is_outdated(gid):
                try:
                    self.procs[gid] = launch(g, rec)
                except Exception as e:
                    messagebox.showerror("Couldn't start the game", str(e))
            elif self.latest.get(gid):
                self.start_install(gid)
            else:
                self.check_one(gid)

        def start_install(self, gid):
            if gid in self.busy:
                return
            if self.running(gid):
                messagebox.showinfo("Game is running", "Close the game first, then update it.")
                return
            g, rel = self.game(gid), self.latest[gid]
            self.busy.add(gid)
            self.rows[gid].status.config(text="Starting...", fg=INK)
            self.rows[gid].bar.config(value=0)
            self.refresh(gid)

            def work():
                try:
                    target = install(g, rel,
                                     progress=lambda d, t: self.q.put(("progress", gid, d, t)),
                                     status=lambda s: self.q.put(("status", gid, s, None)))
                    self.q.put(("done", gid, target, None))
                except Exception as e:
                    self.q.put(("done", gid, None, str(e) or e.__class__.__name__))
            threading.Thread(target=work, daemon=True).start()

        def on_remove(self, gid):
            g = self.game(gid)
            if self.running(gid):
                messagebox.showinfo("Game is running", "Close the game first.")
            elif self.data["games"].get(gid, {}).get("external"):
                if messagebox.askyesno("Unlink game", f"Stop using your own copy of {g['name']}?\nYour files are not deleted."):
                    self.data["games"].pop(gid, None)
                    save_data(self.data)
                    self.refresh(gid)
            elif messagebox.askyesno("Remove game", f"Delete {g['name']} from this computer?\n(Your best scores are kept.)"):
                remove_game(g, self.data["games"].get(gid))
                self.data["games"].pop(gid, None)
                save_data(self.data)
                self.refresh(gid)

        def on_link(self, gid):
            g = self.game(gid)
            kind = [("Game", ("*.exe", "*.app", "*.py", "*.pyw")), ("All files", "*")]
            path = filedialog.askopenfilename(title=f"Choose your copy of {g['name']} (.exe, .app or .py)",
                                              filetypes=kind)
            if not path:
                return
            try:
                rec = link_existing(g, path)
            except RuntimeError as e:
                messagebox.showerror("Can't use that file", str(e))
                return
            self.data["games"][gid] = rec
            save_data(self.data)
            self.refresh(gid)

        # -- results from worker threads ---------------------------------------
        def pump(self):
            try:
                while True:
                    kind, gid, a, b = self.q.get_nowait()
                    if kind == "launcher":
                        if a:
                            self.show_launcher_update(*a)
                        continue
                    row = self.rows[gid]
                    if kind == "latest":
                        self.checking.discard(gid)
                        if a:
                            self.latest[gid] = a
                        else:
                            self.errors[gid] = b
                        self.refresh(gid)
                        if a and self.auto.get() and self.is_outdated(gid) and gid not in self.busy and not self.running(gid):
                            self.start_install(gid)
                    elif kind == "progress":
                        if b:
                            row.bar.config(value=a * 100 / b)
                            row.status.config(text=f"Downloading... {a / 1048576:.1f} of {b / 1048576:.1f} MB")
                    elif kind == "status":
                        row.status.config(text=a)
                        if a != "Downloading...":
                            row.bar.config(value=100)
                    elif kind == "done":
                        self.busy.discard(gid)
                        if b:
                            self.errors[gid] = f"Install failed: {b}"
                            self.refresh(gid)
                            row.status.config(text=self.errors[gid], fg="#e8453c")
                        else:
                            self.data["games"][gid] = {"tag": self.latest[gid]["tag"], "target": a}
                            save_data(self.data)
                            self.refresh(gid)
            except queue.Empty:
                pass
            self.after(100, self.pump)

    App().mainloop()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--check" in sys.argv:
        check()
    else:
        run_app()
