"""Native file / folder chooser for the start screen.

A browser cannot hand a file *path* to the server, so the server opens the dialog itself.
`pick_file()` / `pick_folder()` return {"available": bool, "path": str | None}:
  available False  -> no dialog on this machine (headless, remote over SSH): the page shows a paste-a-path field
  path None        -> the user cancelled

Order: a registered provider (the desktop shell, e.g. pywebview's create_file_dialog, sets
`picker.provider = fn(kind, title, extensions) -> path | None`), then the OS tool (macOS osascript,
Linux zenity / kdialog when a display is set, Windows PowerShell). PATHQC_NO_DIALOG=1 disables the OS tools.
"""
import os
import shutil
import subprocess
import sys

provider = None


def _run(cmd, timeout=600):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = r.stdout.strip()
    return out or None              # cancel -> non-zero exit and empty output


def _os_dialog(kind, title, exts):
    if sys.platform == "darwin":
        what = "choose folder" if kind == "folder" else "choose file"
        return _run(["osascript", "-e", f'POSIX path of ({what} with prompt "{title}")'])
    if sys.platform == "win32":
        if kind == "folder":
            ps = ("Add-Type -AssemblyName System.Windows.Forms;$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                  f"$d.Description='{title}';if($d.ShowDialog() -eq 'OK'){{$d.SelectedPath}}")
        else:
            flt = ";".join("*" + e for e in exts) or "*.*"
            ps = ("Add-Type -AssemblyName System.Windows.Forms;$d=New-Object System.Windows.Forms.OpenFileDialog;"
                  f"$d.Title='{title}';$d.Filter='Slides|{flt}|All files|*.*';if($d.ShowDialog() -eq 'OK'){{$d.FileName}}")
        return _run(["powershell", "-NoProfile", "-STA", "-Command", ps])
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    if shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", f"--title={title}"]
        if kind == "folder":
            cmd.append("--directory")
        elif exts:
            cmd.append("--file-filter=Slides | " + " ".join("*" + e for e in exts))
        return _run(cmd)
    if shutil.which("kdialog"):
        if kind == "folder":
            return _run(["kdialog", "--getexistingdirectory", os.path.expanduser("~"), "--title", title])
        return _run(["kdialog", "--getopenfilename", os.path.expanduser("~"), " ".join("*" + e for e in exts), "--title", title])
    return False


def pick(kind="file", title="Choose a file", exts=()):
    if provider is not None:
        return {"available": True, "path": provider(kind, title, exts)}
    if os.environ.get("PATHQC_NO_DIALOG"):
        return {"available": False, "path": None}
    try:
        p = _os_dialog(kind, title, exts)
    except (OSError, subprocess.SubprocessError):
        p = False
    if p is False:
        return {"available": False, "path": None}
    return {"available": True, "path": p}


def pick_file(title="Locate the H&E slide", exts=()):
    return pick("file", title, exts)


def pick_folder(title="Choose a slide pack folder"):
    return pick("folder", title)
