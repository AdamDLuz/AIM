"""Command line for humans and for Claude, Grok, and Codex."""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import signal
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aim import __version__
from aim.client import Client, AimError
from aim.config import (
    agent_name,
    home_dir,
    load_config,
    os_label,
)
from aim.discovery import Discovery
from aim.install import (
    install_machine,
    read_secret_file,
    write_pair_file,
)
from aim.peers import (
    PeerError,
    annotate,
    learn_peer,
    remember_address,
    resolve_peer,
)
from aim.server import App, AimHTTPServer
from aim.store import Store, utc_now

log = logging.getLogger("aim")


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    _configure_stdio()
    cleaned, json_mode, home, command = _extract(list(argv))
    if home:
        os.environ["AIM_HOME"] = home
    parser = _parser()
    if not cleaned:
        parser.print_help()
        return 2
    args = parser.parse_args(cleaned)
    args.json = json_mode
    args.command = command
    try:
        return int(_COMMANDS[args.cmd](args))
    except (AimError, PeerError, FileNotFoundError, ValueError) as exc:
        return _fail(args, str(exc))
    except KeyboardInterrupt:
        return 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aim",
        description="AIM (AI Messenger): local network messenger for Claude, Grok, and Codex.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    install = sub.add_parser("install", help="install the daemon, launcher, and agent skills")
    install.add_argument("--name", help="stable name for this machine, such as adam-win")
    install.add_argument("--github-root", help="this machine's GitHub folder")
    install.add_argument("--secret-file", help="pair file from the first machine")
    install.add_argument("--no-autostart", action="store_true")
    install.add_argument("--no-start", action="store_true")

    sub.add_parser("serve", help="run the daemon in the foreground")
    sub.add_parser("stop", help="stop the local daemon")
    sub.add_parser("status", help="check the local daemon")
    sub.add_parser("whoami", help="show this machine, without the secret")
    sub.add_parser("version", help="print the version")
    sub.add_parser("self-test", help="message this machine and run a local echo task")

    peers = sub.add_parser("peers", help="list machines, or add/remove one")
    peers.add_argument("action", nargs="?", choices=("add", "remove"))
    peers.add_argument("host", nargs="?")
    peers.add_argument("--port", type=int)

    repos = sub.add_parser("repos", help="list repo folders in a GitHub directory")
    repos.add_argument("--peer", help="machine name; omit for this machine")

    send = sub.add_parser("send", help="leave a message in another agent's inbox")
    send.add_argument("peer")
    send.add_argument("--from", dest="from_agent")
    send.add_argument("--to", dest="to_agent", default="*")
    send.add_argument("--message", required=True)

    inbox = sub.add_parser("inbox", help="read unread messages for an agent")
    inbox.add_argument("--agent", help="claude, grok, or codex")
    inbox.add_argument("--wait", type=int, default=0)
    inbox.add_argument("--limit", type=int, default=50)

    ack = sub.add_parser("ack", help="mark inbox messages read")
    ack.add_argument("ids", nargs="+")

    task = sub.add_parser("task", help="run a command on another machine")
    task.add_argument("peer")
    task.add_argument("--cwd", default=".", help="path inside that machine's GitHub folder")
    task.add_argument("--from", dest="from_agent")
    task.add_argument("--to", dest="to_agent", default="*")
    task.add_argument("--shell", help="one shell command; on Windows this is cmd.exe")
    task.add_argument("--wait", action="store_true")
    task.add_argument("--timeout", type=int)
    task.add_argument("--return-file", action="append", default=[], help="file to snapshot when the command exits 0")
    task.add_argument("--fetch-to", help="local directory for --return-file snapshots")

    tasks = sub.add_parser("tasks", help="list recent tasks")
    tasks.add_argument("--peer")

    status = sub.add_parser("task-status", help="read one task")
    status.add_argument("task_id")
    status.add_argument("--peer", help="omit to read a task this machine ran")

    send_file = sub.add_parser("send-file", help="copy a file into another machine's GitHub folder")
    send_file.add_argument("peer")
    send_file.add_argument("path")
    send_file.add_argument("--dest", required=True, help="path relative to their GitHub folder")
    send_file.add_argument("--from", dest="from_agent")

    pull = sub.add_parser("pull", help="copy a file or folder out of another machine's GitHub folder")
    pull.add_argument("peer")
    pull.add_argument("--path", required=True)
    pull.add_argument("--output", required=True)

    fetch = sub.add_parser("fetch-file", help="download a file id returned by a task")
    fetch.add_argument("peer")
    fetch.add_argument("file_id")
    fetch.add_argument("--output", required=True)

    export = sub.add_parser("pair-export", help="write a pair file for the other computers")
    export.add_argument("--output")
    return parser


def _cmd_install(args: argparse.Namespace) -> int:
    secret = read_secret_file(Path(args.secret_file)) if args.secret_file else None
    result = install_machine(
        name=args.name,
        github_root=args.github_root,
        secret=secret,
        autostart=not args.no_autostart,
        start=not args.no_start,
    )
    if result.get("started"):
        result["healthy"] = _wait_healthy()
        if not result["healthy"]:
            result["notes"].append("the daemon was started but is not answering yet; run aim status")
    human = "\n".join(result["notes"])
    return _emit(args, result, human)


def _cmd_serve(args: argparse.Namespace) -> int:
    cfg = load_config()
    home = home_dir()
    if daemon_running(home):
        return _fail(args, "messenger is already running")
    _setup_logging(home)
    lock = home / "daemon.lock"
    lock.write_text(str(os.getpid()), encoding="utf-8")
    store = Store(home / "messenger.db")
    app = App(cfg, store)
    try:
        httpd = AimHTTPServer((cfg.bind_host, int(cfg.http_port)), app)
    except OSError as exc:
        _release_lock(lock)
        return _fail(args, f"cannot listen on {cfg.bind_host}:{cfg.http_port}: {exc}")
    discovery = Discovery(cfg, store)
    discovery.start()
    log.info("listening on %s:%s as %s", cfg.bind_host, cfg.http_port, cfg.name)
    print(
        f"AIM listening on {cfg.bind_host}:{cfg.http_port} as {cfg.name}",
        flush=True,
    )
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        discovery.stop()
        httpd.server_close()
        _release_lock(lock)
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    cfg = load_config()
    client = Client("127.0.0.1", cfg.http_port, cfg.secret, timeout=5)
    try:
        client.request_json("POST", "/v1/stop", {})
        return _emit(args, {"ok": True, "stopped": True}, "stopped")
    except AimError:
        home = home_dir()
        lock = home / "daemon.lock"
        if not lock.is_file():
            return _fail(args, "messenger is not running")
        try:
            pid = int(lock.read_text(encoding="utf-8").strip())
        except ValueError:
            return _fail(args, "messenger lock file is unreadable")
        if not pid_alive(pid):
            _release_lock(lock)
            return _emit(args, {"ok": True, "stopped": False}, "messenger was not running")
        _kill_pid(pid)
        return _emit(args, {"ok": True, "stopped": True, "pid": pid}, f"stopped pid {pid}")


def _cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    client = Client("127.0.0.1", cfg.http_port, cfg.secret, timeout=3)
    try:
        health = client.health()
        identity = client.identity()
    except AimError as exc:
        return _fail(args, f"not running ({exc})")
    payload = {"ok": True, "running": True, "health": health, "identity": identity}
    human = (
        f"running as {identity.get('name')} ({identity.get('os')}) "
        f"on port {identity.get('http_port')}\n"
        f"github  {identity.get('github_root')}"
    )
    return _emit(args, payload, human)


def _cmd_whoami(args: argparse.Namespace) -> int:
    cfg = load_config()
    payload = {
        "ok": True,
        "name": cfg.name,
        "node_id": cfg.node_id,
        "os": os_label(),
        "github_root": cfg.github_root,
        "agents": list(cfg.agents),
        "exec_enabled": cfg.exec_enabled,
        "http_port": cfg.http_port,
        "home": str(home_dir()),
        "version": __version__,
    }
    human = (
        f"{cfg.name}  {payload['os']}  port {cfg.http_port}\n"
        f"github  {cfg.github_root}\n"
        f"agents  {', '.join(cfg.agents)}"
    )
    return _emit(args, payload, human)


def _cmd_version(args: argparse.Namespace) -> int:
    return _emit(args, {"ok": True, "version": __version__}, __version__)


def _cmd_self_test(args: argparse.Namespace) -> int:
    cfg = load_config()
    client = _local_client(cfg, timeout=30)
    client.health()
    sent = client.send_message(
        from_node=cfg.name,
        from_agent="claude",
        to_agent="grok",
        body="aim-self-test",
    )
    created = client.create_task(
        {
            "from_node": cfg.name,
            "from_agent": "claude",
            "to_agent": "grok",
            "cwd": ".",
            "shell": "echo aim-ok",
            "timeout_s": 30,
            "reply_port": cfg.http_port,
        }
    )
    task = _poll_task(client, created["task"]["id"], 30)
    inbox = client.inbox("grok")
    ok = task.get("exit_code") == 0 and "aim-ok" in (task.get("output") or "")
    found = any(item.get("body") == "aim-self-test" for item in inbox.get("messages") or [])
    payload = {"ok": ok and found, "message_id": sent.get("id"), "task": task, "inbox_has_message": found}
    if not payload["ok"]:
        return _emit(args, payload, "self-test failed", 1)
    return _emit(args, payload, "self-test passed")


def _cmd_peers(args: argparse.Namespace) -> int:
    cfg = load_config()
    store = _store()
    if args.action == "add":
        if not args.host:
            return _fail(args, "usage: aim peers add HOST [--port N]")
        host, port = _split_host(args.host, args.port or cfg.http_port)
        remember_address(store, host, port, pinned=True)
        try:
            learn_peer(store, cfg, host, port, pinned=True)
        except AimError as exc:
            return _emit(
                args,
                {"ok": True, "pinned": f"{host}:{port}", "warning": str(exc)},
                f"pinned {host}:{port}; it did not answer yet ({exc})",
            )
        return _cmd_peers_list(args, cfg, store)
    if args.action == "remove":
        if not args.host:
            return _fail(args, "usage: aim peers remove HOST")
        return _remove_peer(args, store)
    return _cmd_peers_list(args, cfg, store)


def _cmd_peers_list(args: argparse.Namespace, cfg, store: Store) -> int:
    peers = _visible_peers(cfg, store)
    lines = []
    for peer in peers:
        if peer.get("self"):
            state = "this machine"
        elif peer.get("online"):
            state = "online"
        else:
            state = "offline"
        agents = ", ".join(peer.get("agents") or []) or "-"
        lines.append(
            f"{peer['name']}  {peer.get('os')}  {state}  {peer['host']}:{peer['port']}\n"
            f"  github  {peer.get('github_root') or '-'}\n"
            f"  agents  {agents}"
        )
    payload = {"ok": True, "daemon": "up" if _daemon_answers(cfg) else "down", "peers": peers}
    return _emit(args, payload, "\n".join(lines) if lines else "no peers")


def _cmd_repos(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.peer:
        peer = _connect(cfg, args.peer)
        data = _client_for(cfg, peer).repos()
    else:
        root = Path(cfg.github_root)
        names = []
        for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if child.name.startswith(".") or not child.is_dir():
                continue
            names.append(child.name)
            if len(names) >= 500:
                break
        data = {"ok": True, "github_root": str(root), "repos": names}
    human = "\n".join(data.get("repos") or []) or "(no repos)"
    return _emit(args, data, human)


def _cmd_send(args: argparse.Namespace) -> int:
    cfg = load_config()
    peer = _connect(cfg, args.peer)
    result = _client_for(cfg, peer).send_message(
        from_node=cfg.name,
        from_agent=agent_name(args.from_agent),
        to_agent=_to_agent(args.to_agent),
        body=args.message,
    )
    human = f"sent {result.get('id')} to {peer['name']}"
    result["peer"] = peer["name"]
    return _emit(args, result, human)


def _cmd_inbox(args: argparse.Namespace) -> int:
    cfg = load_config()
    agent = agent_name(args.agent) if args.agent else None
    data = _local_client(cfg, timeout=max(10, int(args.wait) + 10)).inbox(
        agent, wait=int(args.wait), limit=int(args.limit)
    )
    lines = []
    for item in data.get("messages") or []:
        lines.append(
            f"{item['id']}  {item['created_at']}  {item['from_node']}/{item['from_agent']} "
            f"-> {item['to_agent']}  {item['kind']}\n  {item['body']}"
        )
    return _emit(args, data, "\n".join(lines) if lines else "(inbox empty)")


def _cmd_ack(args: argparse.Namespace) -> int:
    cfg = load_config()
    data = _local_client(cfg).ack(args.ids)
    return _emit(args, data, f"acked {data.get('acked', 0)}")


def _cmd_task(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.shell and args.command:
        return _fail(args, "pass --shell or a command after --, not both")
    if not args.shell and not args.command:
        return _fail(args, "missing command; use --shell \"...\" or -- git status")
    peer = _connect(cfg, args.peer)
    timeout_s = int(args.timeout or cfg.task_timeout_s)
    spec = {
        "from_node": cfg.name,
        "from_agent": agent_name(args.from_agent),
        "to_agent": _to_agent(args.to_agent),
        "cwd": args.cwd,
        "timeout_s": timeout_s,
        "reply_port": cfg.http_port,
        "return_files": list(args.return_file or []),
    }
    if args.shell:
        spec["shell"] = args.shell
    else:
        spec["argv"] = list(args.command)
    client = _client_for(cfg, peer, timeout=60)
    created = client.create_task(spec)
    task = created["task"]
    if args.wait:
        task = _poll_task(client, task["id"], timeout_s)
        fetched = _fetch_results(client, task, args.fetch_to)
        if fetched:
            task["fetched"] = fetched
    payload = {"ok": True, "peer": peer["name"], "task": task}
    if task.get("status") in {"error", "timeout"} or (
        task.get("exit_code") not in (None, 0) and args.wait
    ):
        payload["ok"] = False
    human = _format_task(task)
    code = 0 if payload["ok"] else 1
    return _emit(args, payload, human, code)


def _cmd_tasks(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.peer:
        peer = _connect(cfg, args.peer)
        data = _client_for(cfg, peer).request_json("GET", "/v1/tasks")
    else:
        data = {"ok": True, "tasks": _store().list_tasks()}
        data["tasks"] = [_without_output(item) for item in data["tasks"]]
    lines = [
        f"{item['id']}  {item['status']}  exit={item.get('exit_code')}  {item['command']}"
        for item in data.get("tasks") or []
    ]
    return _emit(args, data, "\n".join(lines) if lines else "(no tasks)")


def _cmd_task_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.peer:
        peer = _connect(cfg, args.peer)
        data = _client_for(cfg, peer).get_task(args.task_id)
        task = data["task"]
    else:
        task = _store().get_task(args.task_id)
        if not task:
            return _fail(args, "no such task on this machine")
        data = {"ok": True, "task": task}
    return _emit(args, data, _format_task(task), 0 if task.get("exit_code") in (None, 0) else 1)


def _cmd_send_file(args: argparse.Namespace) -> int:
    cfg = load_config()
    local = Path(args.path)
    if not local.is_file():
        return _fail(args, f"not a file: {local}")
    peer = _connect(cfg, args.peer)
    result = _client_for(cfg, peer, timeout=3600).send_file(
        local,
        args.dest,
        cfg.name,
        agent_name(args.from_agent),
    )
    result["peer"] = peer["name"]
    return _emit(args, result, f"sent {local.name} to {peer['name']}:{result.get('path')}")


def _cmd_pull(args: argparse.Namespace) -> int:
    cfg = load_config()
    peer = _connect(cfg, args.peer)
    result = _client_for(cfg, peer, timeout=3600).export_path(args.path, Path(args.output))
    result["peer"] = peer["name"]
    return _emit(args, result, f"saved {result.get('path')} ({result.get('bytes')} bytes)")


def _cmd_fetch_file(args: argparse.Namespace) -> int:
    cfg = load_config()
    peer = _connect(cfg, args.peer)
    result = _client_for(cfg, peer, timeout=3600).fetch_file(args.file_id, Path(args.output))
    return _emit(args, result, f"saved {result.get('path')} ({result.get('bytes')} bytes)")


def _cmd_pair_export(args: argparse.Namespace) -> int:
    cfg = load_config()
    path = Path(args.output) if args.output else home_dir() / "pair.json"
    write_pair_file(path, cfg.secret, cfg.http_port)
    payload = {"ok": True, "path": str(path)}
    human = (
        f"wrote {path}\n"
        "Copy that file to the other computer and run:\n"
        "aim install --secret-file <path> --name <machine>\n"
        "The file is the network key. Do not commit it or paste it into a chat."
    )
    return _emit(args, payload, human)


_COMMANDS = {
    "install": _cmd_install,
    "serve": _cmd_serve,
    "stop": _cmd_stop,
    "status": _cmd_status,
    "whoami": _cmd_whoami,
    "version": _cmd_version,
    "self-test": _cmd_self_test,
    "peers": _cmd_peers,
    "repos": _cmd_repos,
    "send": _cmd_send,
    "inbox": _cmd_inbox,
    "ack": _cmd_ack,
    "task": _cmd_task,
    "tasks": _cmd_tasks,
    "task-status": _cmd_task_status,
    "send-file": _cmd_send_file,
    "pull": _cmd_pull,
    "fetch-file": _cmd_fetch_file,
    "pair-export": _cmd_pair_export,
}


def daemon_running(home: Path | None = None) -> bool:
    home = home or home_dir()
    lock = home / "daemon.lock"
    if not lock.is_file():
        return False
    try:
        pid = int(lock.read_text(encoding="utf-8").strip())
    except ValueError:
        return False
    return pid_alive(pid)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return ctypes.windll.kernel32.GetLastError() == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _visible_peers(cfg, store: Store) -> list[dict]:
    self_row = {
        "node_id": cfg.node_id,
        "name": cfg.name,
        "os": os_label(),
        "host": "127.0.0.1",
        "port": cfg.http_port,
        "github_root": cfg.github_root,
        "agents": list(cfg.agents),
        "exec_enabled": cfg.exec_enabled,
        "last_seen": utc_now(),
        "pinned": False,
        "online": _daemon_answers(cfg),
        "self": True,
        "age_seconds": 0,
    }
    others = []
    for peer in store.list_peers():
        if peer["node_id"] == cfg.node_id:
            continue
        others.append(annotate(peer))
    return [self_row, *others]


def _connect(cfg, query: str) -> dict:
    store = _store()
    peer = resolve_peer(_visible_peers(cfg, store), query)
    if peer.get("self"):
        return peer
    if str(peer.get("node_id", "")).startswith("addr:") or not peer.get("github_root"):
        return annotate(learn_peer(store, cfg, peer["host"], int(peer["port"]), pinned=bool(peer.get("pinned"))))
    return peer


def _client_for(cfg, peer: dict, timeout: float = 30) -> Client:
    return Client(peer["host"], int(peer["port"]), cfg.secret, timeout=timeout)


def _local_client(cfg, timeout: float = 10) -> Client:
    return Client("127.0.0.1", cfg.http_port, cfg.secret, timeout=timeout)


def _store() -> Store:
    return Store(home_dir() / "messenger.db")


def _daemon_answers(cfg) -> bool:
    try:
        _local_client(cfg, timeout=2).health()
    except AimError:
        return False
    return True


def _wait_healthy() -> bool:
    try:
        cfg = load_config()
    except (OSError, ValueError, FileNotFoundError):
        return False
    deadline = time.time() + 15
    while time.time() < deadline:
        if _daemon_answers(cfg):
            return True
        time.sleep(0.4)
    return False


def _poll_task(client: Client, task_id: str, timeout_s: int) -> dict:
    deadline = time.time() + timeout_s + 30
    while True:
        task = client.get_task(task_id)["task"]
        if task.get("status") not in {"queued", "running"}:
            return task
        if time.time() > deadline:
            raise AimError(f"timed out waiting for task {task_id}")
        time.sleep(0.5)


def _fetch_results(client: Client, task: dict, fetch_to: str | None) -> list[str]:
    files = task.get("result_files") or []
    if not fetch_to or not files:
        return []
    folder = Path(fetch_to)
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for item in files:
        dest = folder / Path(str(item.get("filename") or "file")).name
        client.fetch_file(item["id"], dest)
        saved.append(str(client.last_saved or dest))
    return saved


def _remove_peer(args: argparse.Namespace, store: Store) -> int:
    peers = store.list_peers()
    try:
        peer = resolve_peer([annotate(item) for item in peers], args.host)
    except PeerError:
        count = store.delete_peer(host=args.host)
        if not count:
            return _fail(args, f"no peer named {args.host}")
        return _emit(args, {"ok": True, "removed": count}, f"removed {count}")
    count = store.delete_peer(node_id=peer["node_id"])
    return _emit(args, {"ok": True, "removed": peer["name"]}, f"removed {peer['name']}")


def _format_task(task: dict) -> str:
    lines = [
        f"{task.get('id')}  {task.get('status')}  exit={task.get('exit_code')}",
        str(task.get("command") or ""),
    ]
    if task.get("error"):
        lines.append(str(task["error"]))
    if task.get("output"):
        lines.append(str(task["output"]).rstrip())
    if task.get("fetched"):
        lines.append("fetched " + ", ".join(task["fetched"]))
    return "\n".join(line for line in lines if line)


def _without_output(task: dict) -> dict:
    item = dict(task)
    item.pop("output", None)
    return item


def _to_agent(value: str | None) -> str:
    if value in (None, "", "*"):
        return "*"
    return agent_name(value)


def _split_host(text: str, default_port: int) -> tuple[str, int]:
    if text.count(":") == 1 and text.rsplit(":", 1)[1].isdigit():
        host, port_text = text.rsplit(":", 1)
        return host, int(port_text)
    return text, default_port


def _kill_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=15,
        )
        return
    os.kill(pid, signal.SIGTERM)


def _release_lock(lock: Path) -> None:
    try:
        if lock.is_file() and lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock.unlink(missing_ok=True)
        elif lock.is_file():
            # stop may be a different process from the one that acquired the lock.
            try:
                pid = int(lock.read_text(encoding="utf-8").strip())
            except ValueError:
                lock.unlink(missing_ok=True)
                return
            if not pid_alive(pid):
                lock.unlink(missing_ok=True)
    except OSError:
        pass


def _setup_logging(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "daemon.log"
    handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=1, encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[handler, logging.StreamHandler(sys.stderr)],
    )


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _extract(argv: list[str]) -> tuple[list[str], bool, str | None, list[str]]:
    command: list[str] = []
    if "--" in argv:
        index = argv.index("--")
        command = argv[index + 1 :]
        argv = argv[:index]
    cleaned: list[str] = []
    json_mode = False
    home = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--json":
            json_mode = True
        elif arg == "--home":
            index += 1
            if index >= len(argv):
                raise ValueError("--home needs a directory")
            home = argv[index]
        else:
            cleaned.append(arg)
        index += 1
    return cleaned, json_mode, home, command


def _emit(args: argparse.Namespace, payload: dict, human: str, code: int = 0) -> int:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    elif human:
        print(human)
    return code


def _fail(args: argparse.Namespace, message: str, code: int = 1) -> int:
    if getattr(args, "json", False):
        print(json.dumps({"ok": False, "error": message}))
    else:
        print(message, file=sys.stderr)
    return code
