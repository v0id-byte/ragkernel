"""Web 界面：上传自动索引 + 带引用问答。SSE 推送工具轨迹、答案与引用。"""

import json
import queue
import secrets
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, request, send_file

from . import agent, audit, config, pipeline, store
from .tools import Toolbox

app = Flask(__name__)
_sessions: dict[str, dict] = {}
_lock = threading.Lock()
STATIC = Path(__file__).parent / "static"


def _upcfg() -> dict:
    return config.settings().get("upload") or {}


app.config["MAX_CONTENT_LENGTH"] = int(_upcfg().get("max_file_mb", 50)) * 1024 * 1024


def _rl() -> dict:
    return config.settings().get("ratelimit", {}) or {}


# ── 滑动窗口限流 ──────────────────────────────────────────────
_ratelog: dict[str, list] = {}
_ratelog_lock = threading.Lock()


def _rate_ok(keys: list[str], per_min: int) -> bool:
    now = time.time()
    with _ratelog_lock:
        for k in keys:
            hits = [t for t in _ratelog.get(k, []) if now - t < 60]
            _ratelog[k] = hits
            if len(hits) >= per_min:
                return False
        for k in keys:
            _ratelog[k].append(now)
        return True


def _client_ip() -> str:
    cf = request.headers.get("CF-Connecting-IP", "")
    if cf:
        return cf.strip()
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "?"


@app.before_request
def _force_https():
    """经 Cloudflare 转发的明文 http → 301 跳 https（本机直连不带此头，不受影响）。"""
    if request.headers.get("X-Forwarded-Proto", "").lower() == "http":
        return redirect(request.url.replace("http://", "https://", 1), code=301)


@app.get("/")
def index():
    return send_file(STATIC / "index.html")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/session")
def new_session():
    ip = _client_ip()
    if not _rate_ok([f"sess:{ip}"], int(_rl().get("session_per_min", 20))):
        return jsonify({"error": "建立会话过于频繁，稍后再试"}), 429
    fp = ((request.json or {}).get("fingerprint") or "")[:64]
    ua = (request.headers.get("User-Agent", "") or "")[:200]
    aud = audit.Audit(client="web", ip=ip, fingerprint=fp, user_agent=ua)
    sid = secrets.token_hex(16)
    with _lock:
        _sessions[sid] = {
            "toolbox": Toolbox(audit=aud),
            "audit": aud,
            "history": [],
            "busy": threading.Lock(),
            "fingerprint": fp,
            "ip": ip,
        }
    return jsonify({"session_id": sid})


def _get_session(sid: str) -> dict | None:
    with _lock:
        return _sessions.get(sid)


def _sse(gen):
    return Response(gen, mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── 文档管理 ──────────────────────────────────────────────────

@app.get("/api/documents")
def documents():
    return jsonify({"documents": store.list_documents(store.connect())})


@app.delete("/api/documents/<int:doc_id>")
def delete_document(doc_id: int):
    n = store.delete_document(store.connect(), doc_id)
    return jsonify({"deleted": doc_id, "chunks": n})


@app.post("/api/upload")
def upload():
    ip = _client_ip()
    if not _rate_ok([f"up:{ip}"], int(_rl().get("upload_per_min", 20))):
        return jsonify({"error": "上传过于频繁，稍后再试"}), 429
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "没有文件"}), 400
    ext = Path(f.filename).suffix.lower()
    allowed = set(_upcfg().get("allowed_ext") or [])
    if allowed and ext not in allowed:
        return jsonify({"error": f"不支持的类型 {ext}（支持 {sorted(allowed)}）"}), 400

    updir = config.data_dir() / "uploads"
    updir.mkdir(parents=True, exist_ok=True)
    safe = Path(f.filename).name  # 去路径，防目录穿越
    dest = updir / safe
    f.save(dest)

    q: queue.Queue = queue.Queue()

    def work():
        try:
            pipeline.ingest_file(dest, on_stage=lambda s, d: q.put({"t": "stage", "stage": s, **d}))
        except Exception as e:
            q.put({"t": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream():
        # 首个事件提示模型可能正在预热
        yield f"data: {json.dumps({'t': 'stage', 'stage': 'received', 'filename': safe}, ensure_ascii=False)}\n\n"
        while True:
            item = q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"

    return _sse(stream())


@app.get("/api/stats")
def stats():
    db = store.connect()
    return jsonify({
        "totals": store.stats(db),
        "documents": store.list_documents(db),
        "ingestion": store.ingestion_history(db),
        "categories": store.category_counts(db),
        "queries": audit.query_stats(),
    })


@app.post("/api/feedback")
def feedback():
    """回填闭环：把一条处理结果写成新故障案例入库、立即可检索（KB 越用越准）。"""
    ip = _client_ip()
    if not _rate_ok([f"up:{ip}"], int(_rl().get("upload_per_min", 20))):
        return jsonify({"error": "提交过于频繁，稍后再试"}), 429
    body = request.json or {}
    resolution = (body.get("resolution") or "").strip()
    if not resolution:
        return jsonify({"error": "请填写实际处理/解决办法"}), 400
    question = (body.get("question") or "").strip()
    equipment = (body.get("equipment") or "").strip()
    result = (body.get("result") or "").strip()
    parts = []
    if equipment:
        parts.append(f"设备/型号：{equipment}")
    if question:
        parts.append(f"故障现象/问题：{question}")
    parts.append(f"实际处理：{resolution}")
    if result:
        parts.append(f"结果：{result}")
    text = "\n".join(parts)
    title = equipment or question or "反馈案例"
    rec = pipeline.ingest_record(title=title, text=text, source="feedback")
    return jsonify({"ok": True, **rec})


# ── 问答 ──────────────────────────────────────────────────────

@app.post("/api/ask")
def ask():
    body = request.json or {}
    s = _get_session(body.get("session_id", ""))
    question = (body.get("question") or "").strip()
    if not s:
        return jsonify({"error": "会话不存在，先建立会话"}), 404
    if not question:
        return jsonify({"error": "空问题"}), 400
    if len(question) > int(_rl().get("max_question_chars", 2000)):
        return jsonify({"error": "问题太长了，说重点"}), 400
    ip = _client_ip()
    keys = [f"ask:ip:{ip}"]
    if s.get("fingerprint"):
        keys.append(f"ask:fp:{s['fingerprint']}")
    if not _rate_ok(keys, int(_rl().get("ask_per_min", 30))):
        return jsonify({"error": "慢一点～稍后再问"}), 429
    if not s["busy"].acquire(blocking=False):
        return jsonify({"error": "上一个问题还在处理中"}), 429

    q: queue.Queue = queue.Queue()

    def work():
        try:
            answer, messages, tb, model = agent.ask(
                question, toolbox=s["toolbox"], history=s["history"],
                on_tool=lambda name, inp: q.put({"t": "tool", "name": name, "input": inp}),
            )
            s["history"] = agent.trim_history(messages)
            # 去重引用（按 ref）
            seen, cites = set(), []
            for c in tb.touched:
                if c["ref"] not in seen:
                    seen.add(c["ref"])
                    cites.append(c)
            q.put({"t": "answer", "text": answer})
            q.put({"t": "done", "citations": cites, "model": model})
            tb.touched.clear()
        except Exception as e:
            q.put({"t": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            q.put(None)
            s["busy"].release()

    threading.Thread(target=work, daemon=True).start()

    def stream():
        while True:
            item = q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"

    return _sse(stream())


def main():
    import os

    config.load_env()
    web = config.settings().get("web", {})
    host = os.environ.get("RAGKERNEL_HOST") or web.get("host", "127.0.0.1")
    port = int(os.environ.get("RAGKERNEL_PORT") or web.get("port", 8360))
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
