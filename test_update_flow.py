"""End-to-end test of the update flow, with a fake GitHub running on localhost.

    python test_update_flow.py

It plays out what happens to a real player: install each game, publish a new
release, run the launcher's startup update pass, and check that the old version
is gone, the new one runs, saved scores survive, and a new launcher is noticed.
No network or display needed.
"""
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import urllib.request
import zipfile

HOME = tempfile.mkdtemp()                     # the launcher's own data folder
FAKE_USER = tempfile.mkdtemp()                # stands in for the player's home folder (scores live here)
os.environ.update(JACKRABBIT_HOME=HOME, HOME=FAKE_USER, USERPROFILE=FAKE_USER)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

state = {"tag": "v1", "launcher": "v0.2.0", "missing": set()}


def make_zip(tag):
    """A fake game download for this operating system, with the version written inside."""
    import launcher as L
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if L.IS_WIN:
            z.writestr("Game/Game.exe", "exe")
            z.writestr(f"Game/version-{tag}.txt", tag)
        elif L.IS_MAC:
            z.writestr(zipfile.ZipInfo("Game.app/Contents/MacOS/game"), "x")
            z.writestr(f"Game.app/version-{tag}.txt", tag)
        else:
            info = zipfile.ZipInfo("Game/run.sh")
            info.external_attr = 0o755 << 16
            z.writestr(info, "#!/bin/sh\n")
            z.writestr(f"Game/version-{tag}.txt", tag)
    return buf.getvalue()


class FakeGitHub(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, kind="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        import launcher as L
        base = f"http://127.0.0.1:{self.server.server_port}"
        parts = self.path.strip("/").split("/")
        if parts[0] == "dl":                                   # /dl/<tag>/<file>
            return self.reply(200, make_zip(parts[1]), "application/zip")
        if parts[0] == "repos" and parts[-2:] == ["releases", "latest"]:
            repo = "/".join(parts[1:3])
            if repo in state["missing"]:
                return self.reply(404, b"{}")
            if repo == L.LAUNCHER_REPO:
                tag = state["launcher"]
                return self.reply(200, json.dumps(dict(tag_name=tag, html_url=f"{base}/launcher/{tag}", assets=[])).encode())
            game = next(g for g in L.GAMES if g["repo"] == repo)
            tag = state["tag"]
            names = [game["win"], game["mac"], "Source code.zip"]
            assets = [dict(name=n, size=1234, browser_download_url=f"{base}/dl/{tag}/{n.replace(' ', '_')}") for n in names]
            return self.reply(200, json.dumps(dict(tag_name=tag, html_url=f"{base}/r/{tag}", assets=assets)).encode())
        self.reply(404, b"{}")


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["JACKRABBIT_API"] = f"http://127.0.0.1:{server.server_port}"
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))   # never use a proxy for localhost

import launcher as L  # noqa: E402  (must come after the environment is set)


def files_of(game):
    return sorted(f for _, _, fs in os.walk(L.game_dir(game)) for f in fs)


def startup_pass(record):
    """What the launcher does on startup: check every installed game and update the old ones."""
    updated = []
    for g in L.GAMES:
        if g["id"] not in record:
            continue
        rel = L.fetch_latest(g)
        if L.needs_update(record[g["id"]], rel):
            target = L.install(g, rel)
            record[g["id"]] = {"tag": rel["tag"], "target": target}
            updated.append(g["id"])
    return updated


def main():
    record = {}

    # 1. first install of every game at v1
    for g in L.GAMES:
        rel = L.fetch_latest(g)
        assert rel["tag"] == "v1" and rel["name"] == (g["win"] if L.IS_WIN else g["mac"]), (g["id"], rel)
        record[g["id"]] = {"tag": rel["tag"], "target": L.install(g, rel)}
        assert L.installed_path(g, record[g["id"]]), g["id"]
        assert "version-v1.txt" in files_of(g)
    assert startup_pass(record) == [], "nothing should update when everything is current"
    print("ok: fresh install of all 4 games at v1, and a startup check finds nothing to update")

    # 2. high scores: read from the files the games write, never touched by updates
    tank = next(g for g in L.GAMES if g["id"] == "tankwars")
    chase = next(g for g in L.GAMES if g["id"] == "getawaychase")
    jet = next(g for g in L.GAMES if g["id"] == "tuffjet")
    assert L.read_best(tank) is None
    with open(os.path.join(FAKE_USER, ".tank_wars_best"), "w") as f:
        f.write("4210")
    with open(os.path.join(FAKE_USER, ".getaway_chase_best"), "w") as f:
        f.write("987")
    assert (L.read_best(tank), L.read_best(chase), L.read_best(jet)) == (4210, 987, None)
    print("ok: best scores read for Tank Wars and Getaway Chase; Tuff Jet has none to show")

    # 3. publish v2 -> startup pass updates everything and deletes v1
    state["tag"] = "v2"
    assert sorted(startup_pass(record)) == sorted(g["id"] for g in L.GAMES)
    for g in L.GAMES:
        names = files_of(g)
        assert "version-v2.txt" in names and "version-v1.txt" not in names, (g["id"], names)
        assert record[g["id"]]["tag"] == "v2" and L.installed_path(g, record[g["id"]])
        assert not os.path.exists(L.game_dir(g) + ".old") and not os.path.exists(L.game_dir(g) + ".new")
    assert (L.read_best(tank), L.read_best(chase)) == (4210, 987)
    print("ok: v2 published -> all games auto-updated, v1 files deleted, scores untouched")

    # 4. a release that disappears mid-update leaves the installed game alone
    state["tag"] = "v3"
    real = L.download

    def broken(url, dest, progress):
        raise OSError("connection dropped")
    L.download = broken
    try:
        L.install(tank, L.fetch_latest(tank))
        raise AssertionError("install should have failed")
    except OSError:
        pass
    L.download = real
    assert "version-v2.txt" in files_of(tank) and L.installed_path(tank, record["tankwars"])
    print("ok: dropped connection during an update keeps the working v2 install")

    # 5. a repo with no release yet gives a clear message
    state["missing"].add(jet["repo"])
    try:
        L.fetch_latest(jet)
        raise AssertionError("should have failed")
    except RuntimeError as e:
        assert "No release" in str(e), e
    state["missing"].clear()
    print("ok: repo with no release reports 'No release published yet'")

    # 6. a game the player installed themselves: linked, launched, never updated, duplicated or deleted
    echo = next(g for g in L.GAMES if g["id"] == "echo")
    L.remove_game(echo, record.pop("echo"))                     # drop the managed copy first
    assert not os.path.exists(L.game_dir(echo))
    own = tempfile.mkdtemp()
    notgame = os.path.join(own, "readme.txt")
    with open(notgame, "w") as f:
        f.write("not a game")
    if L.IS_MAC:
        mine = os.path.join(own, "Echo.app")
        os.makedirs(os.path.join(mine, "Contents"))
        mine_file = os.path.join(mine, "Contents", "marker.txt")
    else:
        mine = os.path.join(own, "Echo.exe" if L.IS_WIN else "echo-run")
        mine_file = mine
    with open(mine_file, "w") as f:
        f.write("mine")
    if not L.IS_MAC:
        os.chmod(mine, 0o755)
    record["echo"] = L.link_existing(echo, mine)
    assert L.installed_path(echo, record["echo"]) == os.path.abspath(mine)
    for bad in (notgame, os.path.join(own, "missing.exe")):
        try:
            L.link_existing(echo, bad)
            raise AssertionError(f"{bad} should have been refused")
        except RuntimeError:
            pass
    state["tag"] = "v9"                                         # a new release comes out
    updated = startup_pass(record)
    assert sorted(updated) == sorted(g["id"] for g in L.GAMES if g["id"] != "echo"), updated
    assert not os.path.exists(L.game_dir(echo)), "a duplicate copy was installed"
    assert open(mine_file).read() == "mine", "the player's own copy was modified"
    assert L.needs_update(record["echo"], L.fetch_latest(echo)) is False
    L.remove_game(echo, record["echo"])                         # "Unlink"
    assert os.path.exists(mine) and open(mine_file).read() == "mine", "unlinking deleted the player's files"
    L.remove_game(tank, record["tankwars"])                     # "Remove" on a managed copy does delete it
    assert not os.path.exists(L.game_dir(tank))
    print("ok: linked own copy launches, is never updated or duplicated, unlink keeps its files; managed Remove deletes")

    # 6b. a Python game (.py) can be linked and really is launched with Python
    script = os.path.join(own, "echo.py")
    with open(script, "w") as f:
        f.write("import os\nopen(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ran.txt'), 'w').write('ok')\n")
    rec = L.link_existing(echo, script)
    assert rec["external"] and L.installed_path(echo, rec) == os.path.abspath(script)
    assert L.needs_update(rec, L.fetch_latest(echo)) is False
    assert L.launch(echo, rec).wait(timeout=30) == 0
    assert open(os.path.join(own, "ran.txt")).read() == "ok", "the .py game did not run"
    assert not os.path.exists(L.game_dir(echo))
    print("ok: a .py game links, launches with Python, and is never updated or duplicated")

    # 7. the launcher notices a newer version of itself (and ignores the same one)
    assert L.check_launcher_update() is None                       # fake repo says v0.2.0, we are 0.2.0
    state["launcher"] = "v0.3.0"
    tag, url = L.check_launcher_update()
    assert tag == "v0.3.0" and url.endswith("/launcher/v0.3.0")
    assert not L.newer_launcher("v0.2") and L.newer_launcher("v0.10.0") and not L.newer_launcher("v0.1.9")
    print("ok: newer launcher release is detected, same or older version is ignored")

    print("\nALL UPDATE-FLOW CHECKS PASSED")


main()
