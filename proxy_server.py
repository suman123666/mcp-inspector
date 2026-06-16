"""
Cline LLM API 反向代理 - 抓取Cline与大模型交互的完整提示词

工作原理：
1. 启动一个本地HTTP服务器，模拟OpenAI兼容API
2. Cline的请求先经过本代理，代理记录完整的请求/响应内容
3. 代理将请求转发到真实的LLM API，并将响应返回给Cline
4. 所有交互记录保存为JSON文件，并可通过Web UI查看

使用方法：
1. 启动代理: python proxy_server.py --target http://localhost:8000 --port 8001
2. 将Cline的API端点改为 http://localhost:8001
3. 打开 http://localhost:8001/ui 查看抓取的提示词
"""

import argparse
import json
import time
import uuid
import os
import sys
import io
import threading
from datetime import datetime
from pathlib import Path

# 修复 Windows 终端中文/emoji 输出编码问题
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# 清除环境中的代理设置，避免httpx自动使用SOCKS代理
for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(proxy_var, None)

import httpx
from flask import Flask, request, Response, jsonify, send_from_directory
from flask_cors import CORS

# ──────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────
DATA_DIR = Path(__file__).parent / "captures"
DATA_DIR.mkdir(exist_ok=True)
MCP_LOGS_DIR = Path(__file__).parent / "mcp_logs"

app = Flask(__name__, static_folder="web")
CORS(app)

TARGET_URL = "http://localhost:8000"  # 默认目标LLM API地址
CAPTURES = []  # 内存中的捕获记录
MAX_MEMORY_CAPTURES = 500  # 内存中最多保留的记录数


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────
def save_capture(capture: dict):
    """保存捕获记录到文件和内存"""
    CAPTURES.append(capture)
    if len(CAPTURES) > MAX_MEMORY_CAPTURES:
        CAPTURES.pop(0)

    # 保存到文件
    filename = f"{capture['timestamp']}_{capture['id'][:8]}.json"
    filepath = DATA_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(capture, f, ensure_ascii=False, indent=2)


def format_messages_summary(messages: list) -> str:
    """生成消息摘要"""
    if not messages:
        return "(empty)"
    parts = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            # 多模态消息
            text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
            content = " ".join(text_parts)
        if isinstance(content, str):
            preview = content[:100] + "..." if len(content) > 100 else content
        else:
            preview = str(content)[:100]
        parts.append(f"[{role}] {preview}")
    return "\n".join(parts)


def extract_tool_info(messages: list) -> dict:
    """提取MCP工具相关信息"""
    tools_used = []
    tool_results = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        # 检查tool_calls (assistant消息中)
        if role == "assistant" and "tool_calls" in msg:
            for tc in msg["tool_calls"]:
                func = tc.get("function", {})
                tools_used.append({
                    "id": tc.get("id"),
                    "name": func.get("name"),
                    "arguments": func.get("arguments"),
                })

        # 检查tool角色的消息 (工具返回结果)
        if role == "tool":
            tool_results.append({
                "tool_call_id": msg.get("tool_call_id"),
                "content": content[:500] if isinstance(content, str) else str(content)[:500],
            })

    return {"tools_used": tools_used, "tool_results": tool_results}


# ──────────────────────────────────────────────
# 非流式请求处理
# ──────────────────────────────────────────────
def get_target_chat_url():
    """获取目标chat completions的完整URL"""
    # 如果TARGET_URL已经包含/v1，直接追加/chat/completions
    if TARGET_URL.rstrip("/").endswith("/v1"):
        return f"{TARGET_URL.rstrip('/')}/chat/completions"
    return f"{TARGET_URL.rstrip('/')}/v1/chat/completions"


def get_target_url(path: str):
    """获取目标URL"""
    # 如果TARGET_URL已经包含/v1，不要重复
    if TARGET_URL.rstrip("/").endswith("/v1"):
        return f"{TARGET_URL.rstrip('/')}/{path.lstrip('/')}"
    return f"{TARGET_URL.rstrip('/')}/v1/{path.lstrip('/')}"


def handle_non_streaming(req_body: dict, headers: dict) -> tuple:
    """处理非流式请求，返回 (response_body, status_code, response_headers)"""
    url = get_target_chat_url()
    print(f"  -> 转发到: {url}")
    with httpx.Client(timeout=300, proxy=None) as client:
        resp = client.post(url, json=req_body, headers=headers)
    return resp.json(), resp.status_code, dict(resp.headers)


# ──────────────────────────────────────────────
# 流式请求处理
# ──────────────────────────────────────────────
def handle_streaming(req_body: dict, headers: dict, capture: dict):
    """处理流式(SSE)请求，使用原始字节透传确保格式不被破坏"""
    collected_content = []
    collected_tool_calls = {}  # id -> {name, arguments}
    finish_reason = None

    url = get_target_chat_url()
    with httpx.Client(timeout=300, proxy=None) as client:
        with client.stream(
            "POST",
            url,
            json=req_body,
            headers=headers,
        ) as resp:
            # 用原始字节流逐块转发，不破坏SSE格式
            buffer = b""
            for raw_bytes in resp.iter_bytes():
                # 原样转发给客户端
                yield raw_bytes

                # 同时解析内容用于记录
                buffer += raw_bytes
                while b"\n" in buffer:
                    line_bytes, buffer = buffer.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(data_str)
                            choices = chunk.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                if "content" in delta and delta["content"]:
                                    collected_content.append(delta["content"])
                                if "tool_calls" in delta:
                                    for tc in delta["tool_calls"]:
                                        idx = tc.get("index", 0)
                                        if idx not in collected_tool_calls:
                                            collected_tool_calls[idx] = {
                                                "id": tc.get("id", ""),
                                                "name": "",
                                                "arguments": "",
                                            }
                                        func = tc.get("function", {})
                                        if "name" in func:
                                            collected_tool_calls[idx]["name"] = func["name"]
                                        if "arguments" in func:
                                            collected_tool_calls[idx]["arguments"] += func["arguments"]
                                fr = choices[0].get("finish_reason")
                                if fr:
                                    finish_reason = fr
                        except json.JSONDecodeError:
                            pass

    # 组装完整的响应
    response_message = {"role": "assistant", "content": "".join(collected_content)}
    if collected_tool_calls:
        response_message["tool_calls"] = [
            {
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]},
            }
            for tc in collected_tool_calls.values()
        ]

    capture["response"] = {
        "message": response_message,
        "finish_reason": finish_reason,
        "streaming": True,
    }
    capture["duration_ms"] = int((time.time() - capture["_start_time"]) * 1000)
    del capture["_start_time"]
    save_capture(capture)

    print(f"  ✓ 流式响应完成 | 耗时: {capture['duration_ms']}ms | "
          f"内容长度: {len(response_message['content'])} 字符")


# ──────────────────────────────────────────────
# API路由 - 代理转发
# ──────────────────────────────────────────────
@app.route("/v1/chat/completions", methods=["POST"])
@app.route("/chat/completions", methods=["POST"])
def proxy_chat_completions():
    """代理 chat/completions 请求（支持带或不带/v1/前缀）"""
    req_body = request.get_json(force=True)
    capture_id = str(uuid.uuid4())
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    messages = req_body.get("messages", [])
    tools = req_body.get("tools", [])
    model = req_body.get("model", "unknown")
    is_streaming = req_body.get("stream", False)

    print(f"\n{'='*60}")
    print(f"📡 捕获请求 #{len(CAPTURES)+1} | {datetime.now().strftime('%H:%M:%S')}")
    print(f"  模型: {model} | 消息数: {len(messages)} | 工具数: {len(tools)} | 流式: {is_streaming}")

    # 显示消息角色分布
    role_counts = {}
    for m in messages:
        r = m.get("role", "unknown")
        role_counts[r] = role_counts.get(r, 0) + 1
    print(f"  消息角色: {role_counts}")

    # 提取MCP工具信息
    tool_info = extract_tool_info(messages)
    if tool_info["tools_used"]:
        print(f"  🔧 MCP工具调用: {[t['name'] for t in tool_info['tools_used']]}")
    if tool_info["tool_results"]:
        print(f"  📋 工具返回结果: {len(tool_info['tool_results'])} 条")

    # 构建捕获记录
    capture = {
        "id": capture_id,
        "timestamp": timestamp,
        "datetime": datetime.now().isoformat(),
        "model": model,
        "request": {
            "messages": messages,
            "tools": tools,
            "tool_choice": req_body.get("tool_choice"),
            "temperature": req_body.get("temperature"),
            "max_tokens": req_body.get("max_tokens"),
            "other_params": {
                k: v for k, v in req_body.items()
                if k not in ("messages", "tools", "tool_choice", "temperature",
                             "max_tokens", "model", "stream")
            },
        },
        "tool_info": tool_info,
        "_start_time": time.time(),
    }

    # 转发请求头(过滤掉host等，保留OpenRouter等需要的头)
    forward_headers = {}
    for key in ("authorization", "content-type", "accept",
                "http-referer", "x-title", "x-api-key"):
        val = request.headers.get(key)
        if val:
            forward_headers[key] = val

    if is_streaming:
        # 流式处理
        def generate():
            yield from handle_streaming(req_body, forward_headers, capture)

        return Response(
            generate(),
            content_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        # 非流式处理
        resp_body, status_code, resp_headers = handle_non_streaming(req_body, forward_headers)

        # 提取响应内容
        choices = resp_body.get("choices", [])
        response_message = choices[0].get("message", {}) if choices else {}
        finish_reason = choices[0].get("finish_reason") if choices else None

        capture["response"] = {
            "message": response_message,
            "finish_reason": finish_reason,
            "usage": resp_body.get("usage"),
            "streaming": False,
            "full_response": resp_body,
        }
        capture["duration_ms"] = int((time.time() - capture["_start_time"]) * 1000)
        del capture["_start_time"]
        save_capture(capture)

        print(f"  ✓ 响应完成 | 耗时: {capture['duration_ms']}ms | "
              f"finish: {finish_reason}")

        return jsonify(resp_body), status_code


@app.route("/v1/models", methods=["GET"])
@app.route("/models", methods=["GET"])
def proxy_models():
    """代理 models 请求"""
    url = get_target_url("models")
    print(f"  -> 转发 models 请求到: {url}")
    with httpx.Client(timeout=30, proxy=None) as client:
        resp = client.get(
            url,
            headers={"Authorization": request.headers.get("Authorization", "")},
        )
    return Response(resp.content, status=resp.status_code,
                    content_type=resp.headers.get("content-type", "application/json"))


@app.route("/v1/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE"])
def proxy_other_v1(subpath):
    """代理其他 /v1/* 请求"""
    url = get_target_url(subpath)
    print(f"  -> 转发 /v1/{subpath} 到: {url}")
    forward_headers = {}
    for key in ("authorization", "content-type", "accept", "http-referer", "x-title"):
        val = request.headers.get(key)
        if val:
            forward_headers[key] = val
    with httpx.Client(timeout=60, proxy=None) as client:
        resp = client.request(
            method=request.method,
            url=url,
            content=request.get_data(),
            headers=forward_headers,
        )
    return Response(resp.content, status=resp.status_code,
                    content_type=resp.headers.get("content-type", "application/json"))


# ──────────────────────────────────────────────
# API路由 - 查看捕获数据
# ──────────────────────────────────────────────
@app.route("/api/captures", methods=["GET"])
def list_captures():
    """列出所有捕获记录(摘要)"""
    summaries = []
    for cap in reversed(CAPTURES):  # 最新的在前
        messages = cap.get("request", {}).get("messages", [])
        tools = cap.get("request", {}).get("tools", [])
        resp_msg = cap.get("response", {}).get("message", {})

        # 获取最后一条user消息作为摘要
        last_user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, list):
                    text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                    content = " ".join(text_parts)
                last_user_msg = content[:200] if isinstance(content, str) else str(content)[:200]
                break

        # 获取system prompt前200字
        system_prompt_preview = ""
        for m in messages:
            if m.get("role") == "system":
                content = m.get("content", "")
                if isinstance(content, str):
                    system_prompt_preview = content[:200]
                break

        summaries.append({
            "id": cap["id"],
            "datetime": cap.get("datetime"),
            "model": cap.get("model"),
            "message_count": len(messages),
            "tool_count": len(tools),
            "tools_used": [t["name"] for t in cap.get("tool_info", {}).get("tools_used", [])],
            "last_user_message": last_user_msg,
            "system_prompt_preview": system_prompt_preview,
            "response_preview": (resp_msg.get("content", "") or "")[:200],
            "finish_reason": cap.get("response", {}).get("finish_reason"),
            "duration_ms": cap.get("duration_ms"),
            "has_tool_calls": bool(resp_msg.get("tool_calls")),
        })
    return jsonify(summaries)


@app.route("/api/captures/<capture_id>", methods=["GET"])
def get_capture(capture_id):
    """获取单条捕获记录完整内容"""
    for cap in CAPTURES:
        if cap["id"] == capture_id:
            return jsonify(cap)
    return jsonify({"error": "not found"}), 404


@app.route("/api/captures/<capture_id>/messages", methods=["GET"])
def get_capture_messages(capture_id):
    """获取单条捕获记录的消息列表(格式化)"""
    for cap in CAPTURES:
        if cap["id"] == capture_id:
            messages = cap.get("request", {}).get("messages", [])
            formatted = []
            for i, msg in enumerate(messages):
                content = msg.get("content", "")
                if isinstance(content, list):
                    # 多模态内容展开
                    parts = []
                    for p in content:
                        if p.get("type") == "text":
                            parts.append(p.get("text", ""))
                        elif p.get("type") == "image_url":
                            parts.append("[图片]")
                        else:
                            parts.append(f"[{p.get('type', 'unknown')}]")
                    content = "\n".join(parts)

                formatted.append({
                    "index": i,
                    "role": msg.get("role", "unknown"),
                    "content": content,
                    "tool_calls": msg.get("tool_calls"),
                    "tool_call_id": msg.get("tool_call_id"),
                    "name": msg.get("name"),
                    "content_length": len(content) if isinstance(content, str) else 0,
                })
            return jsonify(formatted)
    return jsonify({"error": "not found"}), 404


@app.route("/api/captures/<capture_id>/tools", methods=["GET"])
def get_capture_tools(capture_id):
    """获取单条捕获记录的工具定义"""
    for cap in CAPTURES:
        if cap["id"] == capture_id:
            tools = cap.get("request", {}).get("tools", [])
            return jsonify(tools)
    return jsonify({"error": "not found"}), 404


@app.route("/api/stats", methods=["GET"])
def get_stats():
    """获取统计信息"""
    total = len(CAPTURES)
    if total == 0:
        return jsonify({"total": 0})

    models = {}
    total_messages = 0
    total_tools = set()
    total_duration = 0

    for cap in CAPTURES:
        model = cap.get("model", "unknown")
        models[model] = models.get(model, 0) + 1
        total_messages += len(cap.get("request", {}).get("messages", []))
        for t in cap.get("request", {}).get("tools", []):
            fn = t.get("function", {})
            total_tools.add(fn.get("name", ""))
        total_duration += cap.get("duration_ms", 0)

    return jsonify({
        "total_captures": total,
        "models": models,
        "total_messages": total_messages,
        "unique_tools": list(total_tools),
        "avg_duration_ms": total_duration // total if total else 0,
    })


@app.route("/api/export", methods=["GET"])
def export_all():
    """导出所有捕获数据"""
    return jsonify(CAPTURES)


@app.route("/api/clear", methods=["POST"])
def clear_captures():
    """清空内存中的捕获记录"""
    CAPTURES.clear()
    return jsonify({"status": "cleared"})


# ──────────────────────────────────────────────
# API路由 - MCP日志
# ──────────────────────────────────────────────
@app.route("/api/mcp-logs", methods=["GET"])
def list_mcp_logs():
    """列出所有MCP日志文件"""
    if not MCP_LOGS_DIR.exists():
        return jsonify([])
    logs = []
    for f in sorted(MCP_LOGS_DIR.glob("*.jsonl"), reverse=True):
        entries = []
        try:
            with open(f, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        except Exception:
            continue
        server_name, start_time, msg_count = "unknown", None, 0
        for entry in entries:
            parsed = entry.get("parsed", {})
            if entry.get("direction") == "proxy" and parsed.get("event") == "start":
                server_name = parsed.get("server_name", "unknown")
                start_time = entry.get("timestamp")
            elif entry.get("direction") in ("client->server", "server->client"):
                msg_count += 1
        if msg_count > 0:
            logs.append({
                "filename": f.name,
                "server_name": server_name,
                "start_time": start_time,
                "message_count": msg_count,
            })
    return jsonify(logs)


@app.route("/api/mcp-logs/<filename>", methods=["GET"])
def get_mcp_log(filename):
    """获取指定MCP日志文件内容"""
    if not filename.endswith(".jsonl") or "/" in filename or "\\" in filename:
        return jsonify({"error": "invalid filename"}), 400
    filepath = MCP_LOGS_DIR / filename
    if not filepath.exists():
        return jsonify({"error": "not found"}), 404
    entries = []
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(entries)


# ──────────────────────────────────────────────
# Web UI
# ──────────────────────────────────────────────
@app.route("/ui")
@app.route("/ui/")
def serve_ui():
    """提供Web UI"""
    return send_from_directory("web", "index.html")


@app.route("/")
def index():
    """首页重定向到UI"""
    return """
    <html><body style="font-family:sans-serif;padding:40px">
    <h1>🔍 Cline MCP 提示词抓包工具</h1>
    <p>代理已运行，正在转发到: <code>{target}</code></p>
    <ul>
        <li><a href="/ui">📊 Web UI - 查看捕获的提示词</a></li>
        <li><a href="/api/captures">📋 API - 捕获列表</a></li>
        <li><a href="/api/stats">📈 API - 统计信息</a></li>
        <li><a href="/api/export">💾 API - 导出全部数据</a></li>
    </ul>
    <p>已捕获 <strong>{count}</strong> 条记录</p>
    </body></html>
    """.format(target=TARGET_URL, count=len(CAPTURES))


# ──────────────────────────────────────────────
# 启动
# ──────────────────────────────────────────────
def load_existing_captures():
    """启动时加载已有的捕获文件"""
    files = sorted(DATA_DIR.glob("*.json"))
    for f in files[-MAX_MEMORY_CAPTURES:]:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                cap = json.load(fh)
                CAPTURES.append(cap)
        except Exception:
            pass
    if CAPTURES:
        print(f"📂 已加载 {len(CAPTURES)} 条历史捕获记录")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cline MCP 提示词抓包代理")
    parser.add_argument("--target", "-t", default="http://localhost:8000",
                        help="目标LLM API地址 (默认: http://localhost:8000)")
    parser.add_argument("--port", "-p", type=int, default=8001,
                        help="代理监听端口 (默认: 8001)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="代理监听地址 (默认: 127.0.0.1)")
    args = parser.parse_args()

    TARGET_URL = args.target

    print(f"""
╔══════════════════════════════════════════════════════════╗
║         Cline MCP 提示词抓包代理                         ║
╠══════════════════════════════════════════════════════════╣
║  代理地址:  http://{args.host}:{args.port}
║  目标API:   {TARGET_URL}
║  Web UI:    http://{args.host}:{args.port}/ui
║  数据目录:  {DATA_DIR}
╚══════════════════════════════════════════════════════════╝

📌 使用方法:
   1. 将Cline的API Base URL改为: http://{args.host}:{args.port}
   2. 正常使用Cline，所有交互将被自动抓取
   3. 打开 http://{args.host}:{args.port}/ui 查看提示词
""")

    load_existing_captures()
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
