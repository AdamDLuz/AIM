"""Run a task on this machine and snapshot any files the caller asked to keep.

The cwd has to sit inside the GitHub folder. The command itself runs as the
user who started the daemon. That is the point of the messenger: the other
machine's agent can git pull and build here. The shared secret is the lock.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import signal
import subprocess
import threading
import uuid
import zipfile
from pathlib import Path

from intravo_messenger.config import Config, home_dir
from intravo_messenger.jail import PathJailError, assert_outside_home, resolve_inside
from intravo_messenger.store import Store, utc_now

log = logging.getLogger("ivm.tasks")

MAX_OUTPUT_CHARS = 200_000
MAX_ARGV = 64
MAX_ARG_CHARS = 4000
MAX_SHELL_CHARS = 8000


class TaskError(ValueError):
    pass


def build_command(shell: str | None, argv: list[str] | None) -> list[str]:
    if shell and argv:
        raise TaskError("pass shell or argv, not both")
    if shell is not None:
        if not isinstance(shell, str) or not shell.strip():
            raise TaskError("shell command is empty")
        if len(shell) > MAX_SHELL_CHARS or "\x00" in shell:
            raise TaskError("shell command is invalid")
        if os.name == "nt":
            # cmd.exe understands && on Windows PowerShell 5.1, which does not.
            return ["cmd.exe", "/d", "/s", "/c", shell]
        return ["/bin/sh", "-lc", shell]
    if not argv:
        raise TaskError("missing command")
    if len(argv) > MAX_ARGV:
        raise TaskError("too many arguments")
    cleaned: list[str] = []
    for arg in argv:
        if not isinstance(arg, str) or not arg or len(arg) > MAX_ARG_CHARS or "\x00" in arg:
            raise TaskError("invalid argument")
        cleaned.append(arg)
    return cleaned


def command_display(command: list[str]) -> str:
    if os.name == "nt" and len(command) >= 4 and command[0].lower() == "cmd.exe":
        return command[-1]
    if command[:2] == ["/bin/sh", "-lc"] and len(command) == 3:
        return command[-1]
    return " ".join(command)


def kill_tree(pid: int) -> None:
    if pid <= 0:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
                check=False,
            )
            return
        os.killpg(pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _tail(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    notice = "\n[ivm] output truncated; showing the last 200000 characters\n"
    keep = MAX_OUTPUT_CHARS - len(notice)
    return notice + text[-keep:], True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _safe_filename(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
    cleaned = cleaned.strip("._") or "file"
    return cleaned[:120]


class TaskRunner:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self._slots = threading.BoundedSemaphore(max(1, int(cfg.max_concurrent_tasks)))
        self._home = home_dir()

    def submit(self, spec: dict) -> dict:
        if not self.cfg.exec_enabled:
            raise TaskError("command execution is disabled on this machine")
        active = self.store.count_tasks(("queued", "running"))
        if active >= int(self.cfg.max_queued_tasks):
            raise TaskError("too many tasks are already queued")

        shell = spec.get("shell")
        argv = spec.get("argv")
        command = build_command(shell if isinstance(shell, str) else None, argv if isinstance(argv, list) else None)
        cwd_text = spec.get("cwd") or "."
        try:
            cwd = resolve_inside(Path(self.cfg.github_root), cwd_text)
            assert_outside_home(cwd, self._home)
        except PathJailError as exc:
            raise TaskError(str(exc)) from exc
        if not cwd.is_dir():
            raise TaskError(f"cwd is not a directory: {cwd}")

        timeout = spec.get("timeout_s") or self.cfg.task_timeout_s
        try:
            timeout_s = int(timeout)
        except (TypeError, ValueError) as exc:
            raise TaskError("timeout_s must be an integer") from exc
        timeout_s = max(1, min(timeout_s, 3600))

        return_files = spec.get("return_files") or []
        if not isinstance(return_files, list) or len(return_files) > 20:
            raise TaskError("return_files must be a list of at most 20 paths")

        task_id = uuid.uuid4().hex
        record = {
            "id": task_id,
            "from_node": str(spec.get("from_node") or "unknown")[:64],
            "from_agent": str(spec.get("from_agent") or "unknown")[:32],
            "to_agent": str(spec.get("to_agent") or "*")[:32],
            "reply_host": spec.get("reply_host"),
            "reply_port": spec.get("reply_port"),
            "cwd": str(cwd),
            "command": command_display(command),
            "status": "queued",
        }
        self.store.create_task(record)
        self._audit(f"queued task {task_id} from {record['from_node']}/{record['from_agent']} cwd={cwd} cmd={record['command']}")
        thread = threading.Thread(
            target=self._run,
            name=f"ivm-task-{task_id[:8]}",
            args=(task_id, command, cwd, timeout_s, [str(item) for item in return_files]),
            daemon=True,
        )
        thread.start()
        stored = self.store.get_task(task_id)
        return stored or {**record, "exit_code": None, "output": "", "error": "", "result_files": []}

    def _run(
        self,
        task_id: str,
        command: list[str],
        cwd: Path,
        timeout_s: int,
        return_files: list[str],
    ) -> None:
        with self._slots:
            self.store.update_task(task_id, status="running")
            popen_kwargs: dict = {
                "cwd": str(cwd),
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "stdin": subprocess.DEVNULL,
                "env": _child_env(),
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000
            else:
                popen_kwargs["start_new_session"] = True
            try:
                proc = subprocess.Popen(command, **popen_kwargs)
            except OSError as exc:
                self.store.update_task(
                    task_id,
                    status="error",
                    exit_code=127,
                    error=str(exc),
                    finished_at=utc_now(),
                )
                self._audit(f"error task {task_id} failed to start: {exc}")
                self._notify(task_id)
                return
            try:
                raw, _ = proc.communicate(timeout=timeout_s)
                code = int(proc.returncode)
                status = "ok" if code == 0 else "error"
                error = ""
            except subprocess.TimeoutExpired:
                kill_tree(proc.pid)
                raw, _ = proc.communicate()
                code = int(proc.returncode if proc.returncode is not None else 124)
                status = "timeout"
                error = f"timed out after {timeout_s}s"
            text = (raw or b"").decode("utf-8", errors="replace")
            text, truncated = _tail(text)
            files: list[dict] = []
            file_error = ""
            if status == "ok" and return_files:
                try:
                    files = self._collect(cwd, return_files, task_id)
                except (PathJailError, OSError, TaskError) as exc:
                    status = "error"
                    code = code or 1
                    file_error = str(exc)
            self.store.update_task(
                task_id,
                status=status,
                exit_code=code,
                output=text,
                error=file_error or error,
                truncated=1 if truncated else 0,
                result_files=files,
                finished_at=utc_now(),
            )
            self._audit(f"{status} task {task_id} exit={code}")
            self._notify(task_id)

    def _collect(self, cwd: Path, paths: list[str], task_id: str) -> list[dict]:
        github = Path(self.cfg.github_root)
        collected = []
        for rel in paths:
            target = _resolve_return(github, cwd, rel)
            assert_outside_home(target, self._home)
            if not target.exists():
                raise TaskError(f"return file not found: {rel}")
            collected.append(self._snapshot(target, task_id))
        return collected

    def _snapshot(self, source: Path, task_id: str) -> dict:
        file_id = uuid.uuid4().hex
        filename = _safe_filename(source.name + (".zip" if source.is_dir() else ""))
        blob_dir = self._home / "blobs" / file_id
        blob_dir.mkdir(parents=True, exist_ok=True)
        dest = blob_dir / filename
        if source.is_dir():
            _zip_tree(source, dest, int(self.cfg.max_file_bytes))
        elif source.is_file():
            size = source.stat().st_size
            if size > int(self.cfg.max_file_bytes):
                raise TaskError(f"{source.name} is larger than max_file_bytes")
            _link_or_copy(source, dest)
        else:
            raise TaskError(f"not a file or directory: {source}")
        digest = _sha256(dest)
        record = {
            "id": file_id,
            "from_node": self.cfg.name,
            "from_agent": "daemon",
            "filename": filename,
            "stored_path": str(dest),
            "dest_path": str(source),
            "size": dest.stat().st_size,
            "sha256": digest,
        }
        self.store.add_file(record)
        return {
            "id": file_id,
            "filename": filename,
            "size": record["size"],
            "sha256": digest,
            "source": str(source),
            "task_id": task_id,
        }

    def _notify(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if not task:
            return
        host = task.get("reply_host")
        port = task.get("reply_port") or 4777
        if not host:
            return
        # Local self-test and a caller that is already polling do not need a
        # second copy delivered to this same daemon.
        if host in {"127.0.0.1", "::1"} and int(port) == int(self.cfg.http_port):
            self._drop_local_notice(task)
            return
        body = (
            f"task {task_id} {task['status']} exit={task['exit_code']} "
            f"on {self.cfg.name}: {task['command']}"
        )
        try:
            from intravo_messenger.client import Client

            client = Client(host, int(port), self.cfg.secret, timeout=5)
            client.send_message(
                from_node=self.cfg.name,
                from_agent="daemon",
                to_agent=task["from_agent"] or "*",
                body=body,
                kind="task_result",
                payload={"task_id": task_id, "status": task["status"], "exit_code": task["exit_code"]},
            )
        except Exception as exc:  # noqa: BLE001 — delivery is best-effort
            log.info("could not deliver task result to %s:%s: %s", host, port, exc)
            self._drop_local_notice(task)

    def _drop_local_notice(self, task: dict) -> None:
        self.store.add_message(
            message_id=uuid.uuid4().hex,
            from_node=self.cfg.name,
            from_agent="daemon",
            to_agent=task.get("to_agent") or "*",
            kind="task_notice",
            body=(
                f"task {task['id']} {task['status']} exit={task['exit_code']}: {task['command']}"
            ),
            payload={"task_id": task["id"], "status": task["status"], "exit_code": task["exit_code"]},
        )

    def _audit(self, line: str) -> None:
        path = self._home / "audit.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{utc_now()} {line}\n")
        except OSError as exc:
            log.warning("audit log failed: %s", exc)


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _resolve_return(github: Path, cwd: Path, rel: str) -> Path:
    """A return path is cwd-relative, or github-root-relative if that misses.

    The path that exists wins. Both candidates still have to stay inside the
    GitHub folder.
    """
    if Path(rel).is_absolute():
        return resolve_inside(github, rel)
    ordered: list[Path] = []
    for candidate_root in (Path(cwd) / rel, rel):
        try:
            ordered.append(resolve_inside(github, candidate_root))
        except PathJailError:
            continue
    if not ordered:
        raise PathJailError(f"path escapes {github}")
    for candidate in ordered:
        if candidate.exists():
            return candidate
    return ordered[0]


def _link_or_copy(source: Path, dest: Path) -> None:
    try:
        os.link(source, dest)
    except OSError:
        shutil.copy2(source, dest)


def _zip_tree(source: Path, dest: Path, max_bytes: int) -> None:
    total = 0
    files: list[Path] = []
    root = source.resolve()
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if not _inside_tree(resolved, root):
            continue
        total += resolved.stat().st_size
        if total > max_bytes:
            raise TaskError(f"directory {source.name} is larger than max_file_bytes")
        files.append(resolved)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in files:
            archive.write(path, path.relative_to(root).as_posix())


def _inside_tree(child: Path, parent: Path) -> bool:
    from intravo_messenger.jail import is_inside

    return is_inside(child, parent)
