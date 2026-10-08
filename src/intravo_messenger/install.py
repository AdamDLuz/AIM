"""Copy the program into the user profile, register agent skills, and autostart."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from intravo_messenger.config import create_config, home_dir

SKILL_NAME = "intravo-messenger"
USER_SKILL_ROOTS = (
    ".claude/skills",
    ".codex/skills",
    ".grok/skills",
    ".agents/skills",
)
PROJECT_SKILL_ROOTS = (
    ".claude/skills",
    ".codex/skills",
    ".grok/skills",
    ".agents/skills",
)


def source_root() -> Path:
    here = Path(__file__).resolve()
    root = here.parents[2]
    package = root / "src" / "intravo_messenger" / "__init__.py"
    if package.is_file():
        return root
    raise FileNotFoundError(f"cannot find the messenger source next to {here}")


def install_machine(
    *,
    name: str | None = None,
    github_root: str | None = None,
    secret: str | None = None,
    autostart: bool = True,
    start: bool = True,
    edit_path: bool = True,
    user_home: Path | None = None,
) -> dict:
    root = source_root()
    user_home = user_home or Path.home()
    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    _copy_runtime(root, home)
    launcher = _write_launchers(home)
    path_note = _edit_path(home / "bin", user_home) if edit_path else "path left unchanged"
    cfg = create_config(name=name, github_root=github_root, secret=secret)
    skills = install_skills(root, user_home)
    auto_note = "autostart skipped"
    if autostart:
        auto_note = install_autostart(home, launcher)
    started = False
    if start:
        started = start_daemon(home)
    notes = [
        f"config {home / 'config.json'}",
        f"launcher {launcher}",
        path_note,
        auto_note,
        "started" if started else "not started",
        firewall_hint(),
        "Pair the other computers with: ivm pair-export",
        "Then on each other computer: ivm install --secret-file <that file> --name <machine>",
    ]
    return {
        "ok": True,
        "name": cfg.name,
        "github_root": cfg.github_root,
        "home": str(home),
        "launcher": str(launcher),
        "skills": [str(path) for path in skills],
        "started": started,
        "notes": notes,
    }


def install_skills(root: Path | None = None, user_home: Path | None = None) -> list[Path]:
    root = root or source_root()
    user_home = user_home or Path.home()
    canonical = root / "skills" / SKILL_NAME / "SKILL.md"
    if not canonical.is_file():
        raise FileNotFoundError(f"missing skill: {canonical}")
    written: list[Path] = []
    destinations = [user_home / Path(item) / SKILL_NAME for item in USER_SKILL_ROOTS]
    if (root / "pyproject.toml").is_file() and root.resolve() != home_dir().resolve():
        destinations.extend(root / Path(item) / SKILL_NAME for item in PROJECT_SKILL_ROOTS)
    for dest in destinations:
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / "SKILL.md"
        if target.resolve() == canonical.resolve():
            written.append(target)
            continue
        shutil.copy2(canonical, target)
        written.append(target)
    return written


def install_autostart(home: Path, launcher: Path) -> str:
    if os.name == "nt":
        return _windows_task(home, launcher)
    if sys.platform == "darwin":
        return _launchd(home, launcher)
    return _systemd(home, launcher)


def start_daemon(home: Path) -> bool:
    from intravo_messenger.cli import daemon_running

    if daemon_running(home):
        return True
    log_path = home / "daemon.log"
    if os.name == "nt":
        vbs = home / "bin" / "serve-hidden.vbs"
        subprocess.Popen(
            ["wscript.exe", str(vbs)],
            cwd=str(home),
            close_fds=True,
        )
    else:
        with log_path.open("ab") as handle:
            subprocess.Popen(
                [str(home / "bin" / "ivm"), "serve"],
                cwd=str(home),
                stdout=handle,
                stderr=handle,
                start_new_session=True,
            )
    return True


def firewall_hint() -> str:
    if os.name == "nt":
        return (
            "If Windows asks to allow Python on private networks, allow it. "
            "In an elevated PowerShell, you can also run: "
            "New-NetFirewallRule -DisplayName 'Intravo Messenger' -Direction Inbound "
            "-Protocol TCP -LocalPort 4777 -Action Allow -Profile Private; "
            "New-NetFirewallRule -DisplayName 'Intravo Messenger Discovery' -Direction Inbound "
            "-Protocol UDP -LocalPort 4778 -Action Allow -Profile Private"
        )
    if sys.platform == "darwin":
        return "If macOS asks for local network access, allow it for the terminal or Python."
    return (
        "If ufw is enabled: sudo ufw allow from 192.168.0.0/16 to any port 4777 proto tcp; "
        "sudo ufw allow from 192.168.0.0/16 to any port 4778 proto udp; "
        "sudo ufw allow from 10.0.0.0/8 to any port 4777 proto tcp; "
        "sudo ufw allow from 10.0.0.0/8 to any port 4778 proto udp"
    )


def read_secret_file(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("secret"), str):
        secret = data["secret"]
    elif isinstance(data, str):
        secret = data
    else:
        raise ValueError("secret file must be JSON with a secret field")
    if len(secret) < 32:
        raise ValueError("secret file is too short")
    return secret


def write_pair_file(path: Path, secret: str, http_port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"secret": secret, "http_port": http_port}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _copy_runtime(root: Path, home: Path) -> None:
    package = root / "src" / "intravo_messenger"
    dest = home / "src" / "intravo_messenger"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        package,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    skill_src = root / "skills" / SKILL_NAME
    skill_dest = home / "skills" / SKILL_NAME
    if skill_dest.exists():
        shutil.rmtree(skill_dest)
    shutil.copytree(skill_src, skill_dest)


def _write_launchers(home: Path) -> Path:
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    src = (home / "src").resolve()
    if os.name == "nt":
        launcher = bin_dir / "ivm.cmd"
        launcher.write_text(
            "\r\n".join(
                [
                    "@echo off",
                    f'set "PYTHONPATH={src}"',
                    "where py >nul 2>&1",
                    "if %ERRORLEVEL%==0 (",
                    "  py -3 -m intravo_messenger %*",
                    "  exit /b %ERRORLEVEL%",
                    ")",
                    "python -m intravo_messenger %*",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        vbs = bin_dir / "serve-hidden.vbs"
        vbs.write_text(
            "\r\n".join(
                [
                    "Set shell = CreateObject(\"Wscript.Shell\")",
                    "Set files = CreateObject(\"Scripting.FileSystemObject\")",
                    "folder = files.GetParentFolderName(WScript.ScriptFullName)",
                    "shell.Run \"\"\"\" & folder & \"\\ivm.cmd\"\" serve\", 0, False",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return launcher
    launcher = bin_dir / "ivm"
    launcher.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                f'export PYTHONPATH="{src}${{PYTHONPATH:+:$PYTHONPATH}}"',
                "if command -v python3 >/dev/null 2>&1; then",
                '  exec python3 -m intravo_messenger "$@"',
                "fi",
                'exec python -m intravo_messenger "$@"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher


def _edit_path(bin_dir: Path, user_home: Path) -> str:
    if os.name == "nt":
        return _windows_path(bin_dir)
    return _unix_path(bin_dir, user_home)


def _windows_path(bin_dir: Path) -> str:
    import winreg

    norm = str(bin_dir)
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
        ) as key:
            try:
                current, reg_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current, reg_type = "", winreg.REG_EXPAND_SZ
            parts = [part for part in str(current).split(";") if part]
            if any(part.rstrip("\\").lower() == norm.rstrip("\\").lower() for part in parts):
                return "user PATH already contains the launcher"
            updated = norm if not current else norm + ";" + str(current)
            winreg.SetValueEx(key, "Path", 0, reg_type, updated)
    except OSError as exc:
        return f"could not edit user PATH: {exc}"
    try:
        import ctypes

        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 2, 5000, None)
    except OSError:
        pass
    return "added the launcher to the user PATH (new terminals pick it up)"


def _unix_path(bin_dir: Path, user_home: Path) -> str:
    rc = user_home / (".zshrc" if sys.platform == "darwin" else ".bashrc")
    start = "# >>> intravo-messenger >>>"
    end = "# <<< intravo-messenger <<<"
    block = f'{start}\nexport PATH="{bin_dir}:$PATH"\n{end}\n'
    existing = rc.read_text(encoding="utf-8") if rc.exists() else ""
    if start in existing:
        return f"PATH block already in {rc}"
    with rc.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(block)
    return f"added the launcher to {rc}"


def _windows_task(home: Path, launcher: Path) -> str:
    del launcher  # logon starts the hidden script, which calls ivm.cmd
    vbs = home / "bin" / "serve-hidden.vbs"
    # wscript has no console window at logon.
    command = f'wscript.exe "{vbs}"'
    try:
        completed = subprocess.run(
            [
                "schtasks",
                "/Create",
                "/F",
                "/TN",
                "IntravoMessenger",
                "/SC",
                "ONLOGON",
                "/RL",
                "LIMITED",
                "/TR",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"autostart was not registered: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        fallback = _startup_folder(vbs)
        return f"{fallback} (Task Scheduler refused the logon task: {detail})"
    _remove_startup_folder()
    return "registered IntravoMessenger to start at logon"


def _startup_dir() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _remove_startup_folder() -> None:
    folder = _startup_dir()
    if folder is None:
        return
    script = folder / "IntravoMessenger.vbs"
    try:
        script.unlink(missing_ok=True)
    except OSError:
        pass


def _startup_folder(vbs: Path) -> str:
    """Per-user logon start when Task Scheduler will not create the task."""
    folder = _startup_dir()
    if folder is None:
        return "autostart was not registered: APPDATA is unset"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        script = folder / "IntravoMessenger.vbs"
        script.write_text(
            "\r\n".join(
                [
                    'Set shell = CreateObject("Wscript.Shell")',
                    f'shell.Run "wscript.exe ""{vbs}""", 0, False',
                    "",
                ]
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        return f"autostart was not registered: {exc}"
    return f"registered {script} to start at logon"


def _launchd(home: Path, launcher: Path) -> str:
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    plist_path = agents / "com.intravo.messenger.plist"
    log_path = home / "daemon.log"
    program = launcher
    plist_path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.intravo.messenger</string>
  <key>ProgramArguments</key>
  <array>
    <string>{program}</string>
    <string>serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log_path}</string>
  <key>StandardErrorPath</key><string>{log_path}</string>
</dict>
</plist>
""",
        encoding="utf-8",
    )
    uid = os.getuid()
    domain = f"gui/{uid}"
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], capture_output=True, check=False)
    completed = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        fallback = subprocess.run(["launchctl", "load", "-w", str(plist_path)], capture_output=True, text=True, check=False)
        if fallback.returncode != 0:
            return f"wrote {plist_path} but launchctl did not load it"
    return f"launchd job com.intravo.messenger ({plist_path})"


def _systemd(home: Path, launcher: Path) -> str:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "intravo-messenger.service"
    program = launcher
    unit.write_text(
        "\n".join(
            [
                "[Unit]",
                "Description=Intravo Messenger",
                "After=network-online.target",
                "",
                "[Service]",
                f"ExecStart={program} serve",
                "Restart=on-failure",
                "RestartSec=3",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        ),
        encoding="utf-8",
    )
    if shutil.which("systemctl") is None:
        return f"wrote {unit}; systemctl is not on PATH, so it was not enabled"
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)
    completed = subprocess.run(
        ["systemctl", "--user", "enable", "--now", "intravo-messenger.service"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return f"wrote {unit} but systemctl did not enable it: {detail}"
    return "systemd user service intravo-messenger is enabled"
