"""
MCP stdio 代理包装器 - 抓取Cline与MCP服务器之间的所有通信

工作原理：
1. 本脚本作为MCP服务器的"包装器"运行
2. Cline启动本脚本，本脚本再启动真实的MCP服务器
3. 所有stdin/stdout通信经过本脚本，被完整记录
4. 通信日志保存到文件

使用方法：
在Cline的MCP设置中，将原本的command替换为本脚本：

原始配置：
  "fetch": {
    "command": "C:/Users/16847/.local/bin/uvx.exe",
    "args": ["mcp-server-fetch"]
  }

改为：
  "fetch": {
    "command": "python",
    "args": [
      "/path/to/mcp_stdio_proxy.py",
      "--log-dir", "/path/to/mcp_logs",
      "--", "uvx", "mcp-server-fetch"
    ]
  }
"""

import sys
import os
import json
import subprocess
import threading
import argparse
import time
from datetime import datetime
from pathlib import Path


def create_logger(log_dir: Path, server_name: str):
    """创建日志记录器"""
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"mcp_{server_name}_{timestamp}.jsonl"
    fh = open(log_file, "w", encoding="utf-8")
    print(f"[MCP代理] 日志文件: {log_file}", file=sys.stderr)
    return fh


def log_message(fh, direction: str, data: str):
    """记录一条消息"""
    try:
        # 尝试解析JSON-RPC消息
        parsed = json.loads(data)
        entry = {
            "timestamp": datetime.now().isoformat(),
            "direction": direction,  # "client->server" 或 "server->client"
            "parsed": parsed,
            "raw_length": len(data),
        }
    except json.JSONDecodeError:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "direction": direction,
            "raw": data,
            "raw_length": len(data),
        }

    line = json.dumps(entry, ensure_ascii=False)
    fh.write(line + "\n")
    fh.flush()

    # 在stderr输出简要信息
    if "parsed" in entry:
        parsed = entry["parsed"]
        method = parsed.get("method", "")
        msg_id = parsed.get("id", "")
        if method:
            print(f"[MCP代理] {direction} | method={method} id={msg_id}", file=sys.stderr)
        elif "result" in parsed:
            result = parsed["result"]
            preview = str(result)[:100]
            print(f"[MCP代理] {direction} | result id={msg_id} | {preview}", file=sys.stderr)
        elif "error" in parsed:
            print(f"[MCP代理] {direction} | error id={msg_id} | {parsed['error']}", file=sys.stderr)
    else:
        print(f"[MCP代理] {direction} | raw ({len(data)} bytes)", file=sys.stderr)


def pipe_stdin_to_process(proc, log_fh):
    """将stdin数据转发到子进程的stdin，并记录"""
    try:
        for line in sys.stdin.buffer:
            data = line.decode("utf-8", errors="replace").rstrip("\n\r")
            if data:
                log_message(log_fh, "client->server", data)
            proc.stdin.write(line)
            proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass


def pipe_process_to_stdout(proc, log_fh):
    """将子进程的stdout数据转发到stdout，并记录"""
    try:
        for line in proc.stdout:
            data = line.decode("utf-8", errors="replace").rstrip("\n\r")
            if data:
                log_message(log_fh, "server->client", data)
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
    except (BrokenPipeError, OSError):
        pass


def pipe_process_stderr(proc):
    """转发子进程的stderr"""
    try:
        for line in proc.stderr:
            sys.stderr.buffer.write(b"[MCP-Server] ")
            sys.stderr.buffer.write(line)
            sys.stderr.buffer.flush()
    except (BrokenPipeError, OSError):
        pass


def main():
    # 分离代理参数和被代理命令
    # 用法: mcp_stdio_proxy.py [--log-dir DIR] [--name NAME] -- command [args...]
    proxy_args = []
    cmd_args = []

    found_separator = False
    for arg in sys.argv[1:]:
        if arg == "--":
            found_separator = True
            continue
        if found_separator:
            cmd_args.append(arg)
        else:
            proxy_args.append(arg)

    if not cmd_args:
        print("用法: mcp_stdio_proxy.py [--log-dir DIR] [--name NAME] -- command [args...]",
              file=sys.stderr)
        sys.exit(1)

    # 解析代理参数
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-dir", default=str(Path(__file__).parent / "mcp_logs"))
    parser.add_argument("--name", default=os.path.basename(cmd_args[-1]) if cmd_args else "unknown")
    args = parser.parse_args(proxy_args)

    log_dir = Path(args.log_dir)
    server_name = args.name

    print(f"[MCP代理] 启动 | 服务器: {server_name}", file=sys.stderr)
    print(f"[MCP代理] 命令: {' '.join(cmd_args)}", file=sys.stderr)

    log_fh = create_logger(log_dir, server_name)

    # 记录启动信息
    log_message(log_fh, "proxy", json.dumps({
        "event": "start",
        "command": cmd_args,
        "server_name": server_name,
    }))

    # 清除父进程的 Python venv 环境变量，避免影响子进程的 Python 版本选择
    child_env = os.environ.copy()
    for _key in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "__PYVENV_LAUNCHER__",
                 "PYTHONPATH", "PYTHONHOME"):
        child_env.pop(_key, None)

    # 启动真实的MCP服务器
    proc = subprocess.Popen(
        cmd_args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env,
    )

    # 启动转发线程
    t_stdin = threading.Thread(target=pipe_stdin_to_process, args=(proc, log_fh), daemon=True)
    t_stdout = threading.Thread(target=pipe_process_to_stdout, args=(proc, log_fh), daemon=True)
    t_stderr = threading.Thread(target=pipe_process_stderr, args=(proc,), daemon=True)

    t_stdin.start()
    t_stdout.start()
    t_stderr.start()

    # 等待进程结束
    returncode = proc.wait()

    log_message(log_fh, "proxy", json.dumps({
        "event": "exit",
        "returncode": returncode,
    }))

    log_fh.close()
    print(f"[MCP代理] 进程退出 | code={returncode}", file=sys.stderr)
    os._exit(returncode)  # 跳过 Python 清理，避免守护线程与主线程竞争 stdin 锁


if __name__ == "__main__":
    main()
