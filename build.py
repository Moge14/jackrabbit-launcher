"""Build the launcher into a standalone app for the computer you run this on.

    pip install pyinstaller
    python build.py

Run it once on Windows (gives dist/JackrabbitLauncher.exe) and once on an Apple
Silicon Mac (gives dist/JackrabbitLauncher.app). PyInstaller can't cross-build.
On Mac use the python.org installer so Tk is included.
"""
import subprocess
import sys

args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", "JackrabbitLauncher"]
if sys.platform.startswith("win"):
    args.append("--onefile")
args.append("launcher.py")
subprocess.run(args, check=True)
print("\nDone. Look in the dist folder.")
