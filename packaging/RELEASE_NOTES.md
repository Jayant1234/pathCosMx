## Install PathQC

**1. Download the file for your computer** (under *Assets* below):

| Computer | File |
|---|---|
| Mac with Apple chip (M1–M4) | `PathQC-macos-arm64.zip` |
| Mac with Intel chip | `PathQC-macos-x64.zip` |
| Windows 10 / 11 | `PathQC-windows-x64.zip` |
| Linux (x86-64) | `PathQC-linux-x64.tar.gz` |

Not sure which Mac? Apple menu → **About This Mac** → "Chip" (Apple M… = arm64, Intel = x64).

**2. First open** (the app is not signed yet, so the computer asks once):

- **Mac:** double-click the zip, drag `PathQC.app` into **Applications**. Right-click it → **Open** → **Open**.
  If macOS only offers "Move to Bin": System Settings → Privacy & Security → scroll down → **Open Anyway**.
  (Or in Terminal: `xattr -dr com.apple.quarantine /Applications/PathQC.app`.) After that, a normal double-click.
- **Windows:** right-click the zip → **Extract All**, open the `PathQC` folder, double-click `PathQC.exe`.
  If SmartScreen says "Windows protected your PC": **More info → Run anyway**. Keep the exe inside its folder.
- **Linux:** `tar -xzf PathQC-linux-x64.tar.gz && ./PathQC/PathQC` — the app opens in your web browser
  (keep the terminal open; Ctrl+C stops it). Over SSH: `./PathQC/PathQC --no-browser --port 8011`, then on your
  laptop `ssh -L 8011:127.0.0.1:8011 <server>` and open http://localhost:8011.

**3. Start screen:** type your name → in **Real slide data (password protected)** type the password you were
sent separately → **Unlock** (once per computer, a few seconds) → **Continue with stand-in**, or locate your H&E
file and register it. Later starts open the viewer directly.

**4. Send results back:** **Save results** (top right) writes
`Documents/PathQC/packs/<pack>/results/<YourName>_<date-time>/`. Zip that folder and send it to us.
Problems? Send `Documents/PathQC/pathqc.log` too.

Full guide: [README](https://github.com/Jayant1234/pathCosMx#get-the-app-pathologists).
