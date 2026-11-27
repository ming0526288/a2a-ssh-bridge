#!/usr/bin/python3
import argparse
import configparser
import datetime
import getpass
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import select
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID, uuid4


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "connection.ini"
TOKEN = ROOT / ".token"
PORT, PEER_PORT = 18766, 18767
TASK_LIMIT, REQUEST_LIMIT = 24, 8192
MAX_AGE = 600
TOOL_NAMES = {
    "identity": "Read the receiving host name and configured workspace revision",
    "ask": "Submit a text task to the receiving Codex in read-only mode",
    "threads": "List saved Codex conversations on the receiving host",
}
TAG = re.compile(r"[A-Za-z0-9_.-]{1,60}\Z")


def timestamp(value=None):
    instant = time.time() if value is None else value
    return datetime.datetime.fromtimestamp(instant, datetime.timezone.utc).isoformat()


def private_file(path, data):
    staging = path.with_name(f".{path.name}.{uuid4().hex}")
    try:
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)


def token():
    if not TOKEN.exists():
        raise ValueError("先在 A 机执行 prepare")
    return TOKEN.read_text().strip()


def peer_settings():
    if CONFIG.stat().st_mode & 0o077:
        raise ValueError("请执行 chmod 600 connection.ini")
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        cfg.read(CONFIG)
    except configparser.Error as error:
        raise ValueError("connection.ini 格式有误") from error
    peer = cfg["peer"]
    user, address = peer["username"], peer["address"]
    port = peer.getint("port", 22)
    if not TAG.fullmatch(user) or not re.fullmatch(r"[A-Za-z0-9.:-]+", address) or not 1 <= port <= 65535:
        raise ValueError("SSH 地址、用户或端口格式有误")
    if not peer.get("password"):
        raise ValueError("请在 connection.ini 填写 B 机 SSH 密码")
    return user, address, port, peer["password"]


def ssh_arguments():
    user, address, port, _ = peer_settings()
    arguments = [
        "/usr/bin/ssh", "-F", "/dev/null", "-T", "-p", str(port),
        "-o", "StrictHostKeyChecking=yes", "-o", "ExitOnForwardFailure=yes",
        "-o", "ConnectTimeout=8", "-o", "NumberOfPasswordPrompts=1",
        "-o", "PreferredAuthentications=password,keyboard-interactive",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
    ]
    environment = dict(
        os.environ, SSH_ASKPASS=str(Path(__file__).resolve()),
        SSH_ASKPASS_REQUIRE="force", A2A_BRIDGE_ASKPASS="1"
    )
    return arguments, f"{user}@{address}", environment


def agent_settings():
    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read(ROOT / "agent.ini")
    except configparser.Error as error:
        raise ValueError("agent.ini 格式有误") from error
    configured = Path(config.get("agent", "workspace", fallback="..")).expanduser()
    directory = (ROOT / configured).resolve()
    if not directory.is_dir():
        raise ValueError("agent.ini 的 workspace 目录不存在")
    return directory, config.get("agent", "proxy", fallback="")


def codex_binary():
    return shutil.which("codex") or str(Path.home() / ".local/bin/codex")


def rpc_line(process, message, response_id=None):
    process.stdin.write(json.dumps(message).encode() + b"\n")
    process.stdin.flush()
    if response_id is None:
        return None
    deadline = time.monotonic() + 15
    buffer = b""
    while time.monotonic() < deadline:
        if not select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
            break
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk:
            raise ValueError("Codex App Server 未返回会话列表")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            try:
                response = json.loads(line)
            except ValueError:
                continue
            if response.get("id") == response_id:
                if "error" in response:
                    raise ValueError(response["error"].get("message", "Codex 会话查询失败"))
                return response["result"]
    raise ValueError("Codex 会话查询超时")


def codex_query(directory, method, params):
    process = subprocess.Popen(
        [codex_binary(), "app-server", "--listen", "stdio://"],
        cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        rpc_line(process, {"method": "initialize", "id": 0, "params": {
            "clientInfo": {"name": "a2a_ssh_bridge", "version": "0.2.0"},
        }}, 0)
        rpc_line(process, {"method": "initialized"})
        return rpc_line(process, {"method": method, "id": 1, "params": params}, 1)
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()


def codex_threads(directory, cursor):
    params = {
        "limit": 20, "sortKey": "updated_at",
        "sourceKinds": ["cli", "vscode", "exec", "appServer"],
    }
    if cursor:
        params["cursor"] = cursor
    result = codex_query(directory, "thread/list", params)
    return {
        "threads": [
            {"id": item.get("id"), "title": item.get("name"), "workspace": item.get("cwd"),
             "updated_at": timestamp(item["updatedAt"]) if item.get("updatedAt") else None}
            for item in result.get("data", [])
        ],
        "next_cursor": result.get("nextCursor"),
    }


def codex_answer(prompt, directory, proxy, mode, thread_id):
    env = os.environ.copy()
    if proxy:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[name] = proxy
    command = [codex_binary(), "exec", "--sandbox", "read-only", "-c", 'approval_policy="never"']
    if thread_id:
        command.extend(["resume", "--json", "--skip-git-repo-check", thread_id, "-"])
    else:
        if mode == "independent":
            command.append("--ephemeral")
        command.extend(["--json", "--skip-git-repo-check", "-"])
    completed = subprocess.run(
        command,
        cwd=directory, env=env, input=prompt, capture_output=True, text=True, timeout=90,
    )
    if completed.returncode:
        raise ValueError(f"Codex 分析退出码 {completed.returncode}。请检查登录和模型网络。")
    answer = None
    actual_thread = None
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item", {})
        if event.get("type") == "thread.started":
            actual_thread = event.get("thread_id")
        elif event.get("type") == "item.completed" and item.get("type") == "agent_message":
            answer = item.get("text", "")[:3000]
    if answer is None:
        raise ValueError("Codex 未返回分析结果。")
    if thread_id and actual_thread != thread_id:
        raise ValueError("Codex 返回的会话 ID 与指定的 ID 不一致")
    if mode == "new" and not actual_thread:
        raise ValueError("Codex 没有返回新会话 ID")
    return answer, actual_thread if mode != "independent" else None


def execute(tool, run_id, task_text, mode, thread_id, cursor):
    directory, proxy = agent_settings()
    if thread_id:
        thread = codex_query(directory, "thread/read", {"threadId": thread_id})["thread"]
        saved_directory = thread.get("cwd")
        if thread.get("id") != thread_id or not saved_directory or not Path(saved_directory).is_dir():
            raise ValueError("指定会话或其原工作目录不可用")
        directory = Path(saved_directory)
    head = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, timeout=6,
    )
    result = {
        "hostname": socket.gethostname(), "observed_at": timestamp(), "run_id": run_id,
        "workspace": str(directory), "source_head": head.stdout.strip() if head.returncode == 0 else None,
    }
    if tool == "ask":
        answer, actual_thread = codex_answer(
            "这是一条已认证的远端任务。按只读方式完成。返回结论与依据。"
            "凭据、令牌、私钥和本服务的私有配置不得披露。\n\n" + task_text,
            directory, proxy, mode, thread_id,
        )
        result.update({"codex": answer, "session": mode, "thread_id": actual_thread})
    elif tool == "threads":
        result.update(codex_threads(directory, cursor))
    return result


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Tasks:
    def __init__(self):
        self.lock = threading.Lock()
        self.items = OrderedDict()

    def get(self, task_id):
        with self.lock:
            entry = self.items.get(task_id)
            if entry is None or (entry[1] and time.monotonic() - entry[1] > MAX_AGE):
                raise RpcError(-32001, "Task not found")
            return entry[0].copy()

    def submit(self, message):
        if (
            not isinstance(message, dict) or message.get("role") not in ("user", "agent")
            or not isinstance(message.get("messageId"), str) or message.get("taskId")
        ):
            raise RpcError(-32602, "Expected a new A2A message with messageId")
        parts = message.get("parts")
        if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
            raise RpcError(-32602, "Expected one data part")
        data = parts[0].get("data")
        if parts[0].get("kind") != "data" or not isinstance(data, dict):
            raise RpcError(-32602, "Expected a data part")
        tool, run_id, task_text = data.get("task"), data.get("run_id"), data.get("text")
        if not isinstance(tool, str) or tool not in TOOL_NAMES or not isinstance(run_id, str) or not TAG.fullmatch(run_id):
            raise RpcError(-32602, "Expected a supported task and a valid run_id")
        if tool == "ask" and (not isinstance(task_text, str) or not 0 < len(task_text) <= 1000):
            raise RpcError(-32602, "Expected ask text of at most 1000 characters")
        mode, thread_id, cursor = data.get("session"), data.get("thread_id"), data.get("cursor")
        if mode not in (None, "independent", "new") or (thread_id is not None and mode is not None):
            raise RpcError(-32602, "Choose independent, new, or one thread ID")
        if (mode is not None or thread_id is not None) and tool != "ask":
            raise RpcError(-32602, "Conversation options apply to ask")
        if thread_id is not None:
            try:
                thread_id = str(UUID(thread_id)) if isinstance(thread_id, str) else None
            except ValueError as error:
                raise RpcError(-32602, "Expected a Codex conversation UUID") from error
            if not thread_id:
                raise RpcError(-32602, "Expected a Codex conversation UUID")
        if cursor is not None and (tool != "threads" or not isinstance(cursor, str) or len(cursor) > 2048):
            raise RpcError(-32602, "Expected a valid thread list cursor")
        mode = "resume" if thread_id else (mode or "independent")
        task_id = str(uuid4())
        job = {
            "id": task_id, "contextId": str(uuid4()), "kind": "task",
            "status": {"state": "submitted", "timestamp": timestamp()},
            "metadata": {"run_id": run_id},
        }
        with self.lock:
            if any(entry[1] is None for entry in self.items.values()):
                raise RpcError(-32000, "One diagnostic task is already running")
            while self.items and (
                len(self.items) >= TASK_LIMIT or time.monotonic() - next(iter(self.items.values()))[1] > MAX_AGE
            ):
                self.items.popitem(last=False)
            self.items[task_id] = (job, None)
        try:
            threading.Thread(
                target=self.run, args=(job.copy(), tool, run_id, task_text, mode, thread_id, cursor), daemon=True,
            ).start()
        except RuntimeError as error:
            with self.lock:
                self.items.pop(task_id, None)
            raise RpcError(-32603, "Could not start diagnostic task") from error
        return job

    def run(self, job, tool, run_id, task_text, mode, thread_id, cursor):
        task_id = job["id"]
        with self.lock:
            job["status"] = {"state": "working", "timestamp": timestamp()}
            self.items[task_id] = (job.copy(), None)
        try:
            data = execute(tool, run_id, task_text, mode, thread_id, cursor)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
            state, data = "failed", {"error": str(error), "run_id": run_id}
        except Exception as error:
            state, data = "failed", {
                "error": f"Unexpected diagnostic task failure ({type(error).__name__})",
                "run_id": run_id,
            }
        else:
            state = "completed"
        job["status"] = {"state": state, "timestamp": timestamp()}
        job["artifacts"] = [{"artifactId": str(uuid4()), "parts": [{"kind": "data", "data": data}]}]
        with self.lock:
            self.items[task_id] = (job, time.monotonic())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, data, status=200):
        content = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def authorized(self):
        header = self.headers.get("Authorization", "")
        if hmac.compare_digest(header.encode(), ("Bearer " + self.server.key).encode()):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self.authorized():
            return
        if self.path != "/.well-known/agent-card.json":
            self.reply({"error": "Not found"}, 404)
            return
        self.reply({
            "protocolVersion": "0.3.0", "name": socket.gethostname(),
            "description": "Cross-host diagnostic and Codex tasks over a private SSH tunnel",
            "url": f"http://127.0.0.1:{self.server.external_port}/",
            "preferredTransport": "JSONRPC", "version": "0.1.0",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["application/json"], "defaultOutputModes": ["application/json"],
            "securitySchemes": {"lab": {"type": "http", "scheme": "bearer"}},
            "security": [{"lab": []}],
            "skills": [
                {"id": name, "name": name, "description": description, "tags": ["diagnostics"]}
                for name, description in TOOL_NAMES.items()
            ],
        })

    def do_POST(self):
        if not self.authorized():
            return
        if self.path != "/":
            self.reply({"error": "Not found"}, 404)
            return
        answer = {"jsonrpc": "2.0", "id": None}
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= REQUEST_LIMIT:
                raise RpcError(-32600, "Invalid request length")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or "id" not in request:
                raise RpcError(-32600, "Invalid JSON-RPC request")
            answer["id"] = request["id"]
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise RpcError(-32602, "Expected object params")
            if request.get("method") == "message/send":
                answer["result"] = self.server.tasks.submit(params.get("message"))
            elif request.get("method") == "tasks/get":
                task_id = params.get("id")
                if not isinstance(task_id, str):
                    raise RpcError(-32602, "Expected task id")
                answer["result"] = self.server.tasks.get(task_id)
            else:
                raise RpcError(-32601, "Method not supported")
        except (ValueError, UnicodeError):
            answer["error"] = {"code": -32700, "message": "Invalid JSON"}
        except RpcError as error:
            answer["error"] = {"code": error.code, "message": str(error)}
        self.reply(answer)


def rpc(method, params, port):
    payload = {"id": str(uuid4()), "jsonrpc": "2.0", "method": method, "params": params}
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token()},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=8) as response:
        result = json.load(response)
    if "error" in result:
        raise ValueError(result["error"]["message"])
    return result["result"]


def unit(kind):
    start = shlex.quote(str(ROOT / "bridge.py")).replace("%", "%%")
    network = "After=network-online.target\n" if kind == "tunnel" else ""
    restart = "always" if kind == "tunnel" else "on-failure"
    return (
        f"[Unit]\nDescription=A2A SSH Bridge {kind}\n{network}\n"
        f"[Service]\nType=simple\nExecStart=/usr/bin/python3 {start} {kind}\n"
        f"Restart={restart}\nRestartSec=4s\nNoNewPrivileges=yes\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def install_units(tunnel):
    name = getpass.getuser()
    current = subprocess.run(
        ["loginctl", "show-user", name, "--property=Linger", "--value"],
        capture_output=True, text=True, timeout=8,
    )
    if current.stdout.strip() != "yes":
        enable = subprocess.run(
            ["loginctl", "--no-ask-password", "enable-linger", name], capture_output=True, timeout=8,
        )
        if enable.returncode:
            raise ValueError(f"开机常驻需要在这台机器上执行 sudo loginctl enable-linger {name}")
    folder = Path.home() / ".config/systemd/user"
    folder.mkdir(parents=True, exist_ok=True)
    units = {"a2a-bridge.service": unit("serve")}
    if tunnel:
        units["a2a-bridge-tunnel.service"] = unit("tunnel")
    for name, data in units.items():
        private_file(folder / name, data.encode())
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", *units], check=True)
    subprocess.run(["systemctl", "--user", "restart", *units], check=True)


def prepare():
    if not CONFIG.exists():
        private_file(CONFIG, (ROOT / "connection.ini.example").read_bytes())
    if not TOKEN.exists():
        private_file(TOKEN, (secrets.token_urlsafe(32) + "\n").encode())


def pair():
    prepare()
    args, destination, env = ssh_arguments()
    home = Path.home()
    remote_root = PurePosixPath(ROOT.relative_to(home) if home in ROOT.parents else ROOT.name)
    remote = shlex.quote(str(remote_root))
    prelude = f"umask 077 && mkdir -p {remote} && cd {remote}"
    for path, mode in ((ROOT / "bridge.py", "755"), (TOKEN, "600")):
        name = shlex.quote(path.name)
        temp = shlex.quote(f".{path.name}.{uuid4().hex}")
        command = f"{prelude} && cat > {temp} && chmod {mode} {temp} && mv -f {temp} {name}"
        subprocess.run([*args, destination, command], input=path.read_bytes(), env=env, check=True, timeout=25)
    command = f"{prelude} && /usr/bin/python3 bridge.py install-peer"
    subprocess.run([*args, destination, command], env=env, check=True, timeout=25)
    install_units(tunnel=True)


def main():
    if os.environ.get("A2A_BRIDGE_ASKPASS") == "1":
        print(peer_settings()[3])
        return
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "pair", "tunnel", "install-peer"):
        commands.add_parser(name)
    server = commands.add_parser("serve")
    server.add_argument("--port", type=int, default=PORT)
    server.add_argument("--external-port", type=int, default=PEER_PORT)
    send = commands.add_parser("send")
    send.add_argument("task", choices=TOOL_NAMES)
    send.add_argument("run_id")
    send.add_argument("--text")
    session = send.add_mutually_exclusive_group()
    session.add_argument("--session", choices=("independent", "new"))
    session.add_argument("--thread-id")
    send.add_argument("--cursor")
    send.add_argument("--port", type=int, default=PEER_PORT)
    send.add_argument("--wait", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("id")
    status.add_argument("--port", type=int, default=PEER_PORT)
    options = parser.parse_args()
    if options.command == "prepare":
        prepare()
    elif options.command == "pair":
        pair()
    elif options.command == "install-peer":
        token()
        install_units(tunnel=False)
    elif options.command == "tunnel":
        args, destination, env = ssh_arguments()
        os.execve(args[0], [*args, "-N",
            "-L", f"127.0.0.1:{PEER_PORT}:127.0.0.1:{PORT}",
            "-R", f"127.0.0.1:{PEER_PORT}:127.0.0.1:{PORT}", destination,
        ], env)
    elif options.command == "serve":
        http = ThreadingHTTPServer(("127.0.0.1", options.port), Handler)
        http.key, http.tasks, http.external_port = token(), Tasks(), options.external_port
        with http:
            http.serve_forever()
    else:
        if options.command == "send":
            message = {
                "kind": "message", "role": "user", "messageId": str(uuid4()),
                "parts": [{"kind": "data", "data": {
                    "task": options.task, "run_id": options.run_id, "text": options.text,
                    "session": options.session, "thread_id": options.thread_id, "cursor": options.cursor,
                }}],
            }
            job = rpc("message/send", {"message": message, "configuration": {"blocking": False}}, options.port)
            if options.wait:
                end = time.monotonic() + (100 if options.task == "ask" else 35)
                while job["status"]["state"] in ("submitted", "working") and time.monotonic() < end:
                    time.sleep(0.6)
                    job = rpc("tasks/get", {"id": job["id"]}, options.port)
        else:
            job = rpc("tasks/get", {"id": options.id}, options.port)
        print(json.dumps(job, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
