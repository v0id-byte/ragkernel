"""Web 界面：上传自动索引 + 带引用问答。SSE 推送工具轨迹、答案与引用。"""

import json
import queue
import secrets
import threading
import time
from pathlib import Path

from flask import Flask, Response, g, jsonify, redirect, request, send_file

from . import agent, audit, auth, backends, config, pipeline, store
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


@app.post("/api/auth/check-user")
def check_user():
    """两步登录第一步：只看用户名，决定前端下一步是弹密码框还是设密码框。"""
    ip = _client_ip()
    if not _rate_ok([f"checkuser:{ip}"], int(_rl().get("session_per_min", 20))):
        return jsonify({"error": "请求过于频繁，稍后再试"}), 429
    username = ((request.json or {}).get("username") or "").strip()
    status = auth.user_status(username) if username else None
    if not status:
        return jsonify({"exists": False})
    return jsonify({"exists": True, **status})


@app.post("/api/auth/login")
def login():
    ip = _client_ip()
    if not _rate_ok([f"login:{ip}"], int(_rl().get("session_per_min", 20))):
        return jsonify({"error": "登录过于频繁，稍后再试"}), 429
    body = request.json or {}
    user = auth.authenticate((body.get("username") or "").strip(), body.get("password") or "")
    if not user:
        return jsonify({"error": "用户名或密码不正确"}), 401
    token = auth.issue_token(user["id"])
    return jsonify({
        "token": token,
        "user": {"id": user["id"], "username": user["username"], "is_admin": bool(user["is_admin"])},
    })


@app.post("/api/auth/setup-password")
def setup_password():
    """待激活账号首登：凭一次性建号口令设置密码，成功即登录。"""
    ip = _client_ip()
    if not _rate_ok([f"setup:{ip}"], int(_rl().get("session_per_min", 20))):
        return jsonify({"error": "请求过于频繁，稍后再试"}), 429
    body = request.json or {}
    username = (body.get("username") or "").strip()
    setup_code = (body.get("setup_code") or "").strip()
    password = body.get("password") or ""
    if len(password) < 6:
        return jsonify({"error": "密码至少 6 位"}), 400
    user = auth.setup_password(username, setup_code, password)
    if not user:
        return jsonify({"error": "建号口令不正确或已过期"}), 400
    token = auth.issue_token(user["id"])
    return jsonify({
        "token": token,
        "user": {"id": user["id"], "username": user["username"], "is_admin": bool(user["is_admin"])},
    })


@app.post("/api/auth/logout")
@auth.require_auth
def logout():
    header = request.headers.get("Authorization", "")
    auth.revoke_token(header[7:] if header.startswith("Bearer ") else "")
    return jsonify({"ok": True})


@app.post("/api/session")
@auth.require_auth
def new_session():
    ip = _client_ip()
    if not _rate_ok([f"sess:{ip}"], int(_rl().get("session_per_min", 20))):
        return jsonify({"error": "建立会话过于频繁，稍后再试"}), 429
    fp = ((request.json or {}).get("fingerprint") or "")[:64]
    ua = (request.headers.get("User-Agent", "") or "")[:200]
    aud = audit.Audit(client="web", ip=ip, fingerprint=fp, user_agent=ua, user_id=g.user["id"])
    sid = secrets.token_hex(16)
    with _lock:
        _sessions[sid] = {
            "toolbox": Toolbox(audit=aud),
            "audit": aud,
            "history": [],
            "busy": threading.Lock(),
            "fingerprint": fp,
            "ip": ip,
            "user_id": g.user["id"],
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
@auth.require_auth
def documents():
    return jsonify({"documents": store.list_documents(store.connect())})


@app.delete("/api/documents/<int:doc_id>")
@auth.require_auth
def delete_document(doc_id: int):
    n = store.delete_document(store.connect(), doc_id)
    return jsonify({"deleted": doc_id, "chunks": n})


@app.post("/api/upload")
@auth.require_auth
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
@auth.require_auth
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
@auth.require_auth
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
@auth.require_auth
def ask():
    body = request.json or {}
    s = _get_session(body.get("session_id", ""))
    question = (body.get("question") or "").strip()
    image = body.get("image")
    images = None
    if isinstance(image, str) and image.startswith("data:"):
        header, _, data = image.partition(",")
        media_type = header[5:].split(";")[0] or "image/jpeg"
        if data:
            images = [{"media_type": media_type, "data": data}]
    if not s:
        return jsonify({"error": "会话不存在，先建立会话"}), 404
    if not question and not images:
        return jsonify({"error": "空问题"}), 400
    if len(question) > int(_rl().get("max_question_chars", 2000)):
        return jsonify({"error": "问题太长了，说重点"}), 400
    ip = _client_ip()
    keys = [f"ask:ip:{ip}", f"ask:user:{g.user['id']}"]
    if not _rate_ok(keys, int(_rl().get("ask_per_min", 30))):
        return jsonify({"error": "慢一点～稍后再问"}), 429
    if not s["busy"].acquire(blocking=False):
        return jsonify({"error": "上一个问题还在处理中"}), 429

    q: queue.Queue = queue.Queue()

    def work():
        try:
            answer, messages, tb, model = agent.ask(
                question, toolbox=s["toolbox"], history=s["history"], images=images,
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


# ── 后台管理（IP 白名单 + 管理员账号）────────────────────────────

@app.get("/admin")
@auth.require_admin_ip
def admin_page():
    return send_file(STATIC / "admin.html")


@app.get("/admin/api/users")
@auth.require_admin_ip
@auth.require_auth
@auth.require_admin
def admin_list_users():
    return jsonify({"users": auth.list_users()})


@app.post("/admin/api/users")
@auth.require_admin_ip
@auth.require_auth
@auth.require_admin
def admin_create_user():
    """密码留空 = 建待激活账号，返回一次性建号口令给管理员转交本人（只出现这一次，库里只存哈希）。"""
    body = request.json or {}
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip() or None
    if not username:
        return jsonify({"error": "用户名不能为空"}), 400
    try:
        result = auth.create_user(username, password, is_admin=bool(body.get("is_admin")))
    except Exception:
        return jsonify({"error": "用户名已存在"}), 400
    return jsonify({"id": result["id"], "username": username, "setup_code": result["setup_code"]})


@app.post("/admin/api/users/<int:user_id>/deactivate")
@auth.require_admin_ip
@auth.require_auth
@auth.require_admin
def admin_deactivate_user(user_id: int):
    auth.set_active(user_id, False)
    return jsonify({"ok": True})


@app.post("/admin/api/users/<int:user_id>/activate")
@auth.require_admin_ip
@auth.require_auth
@auth.require_admin
def admin_activate_user(user_id: int):
    auth.set_active(user_id, True)
    return jsonify({"ok": True})


# ── AI 服务提供方设置（云端 API Key，或指向已在跑的本地推理服务）──

def _provider_view(prov: dict) -> dict:
    import os as _os

    key = prov.get("api_key") or _os.environ.get(prov.get("api_key_env", ""), "")
    return {
        "kind": prov.get("kind", "anthropic"),
        "base_url": prov.get("base_url", ""),
        "model": prov.get("model", ""),
        "max_tokens": int(prov.get("max_tokens", 8000)),
        "api_key_set": bool(key),
        "api_key_hint": ("···" + key[-4:]) if key else "",
    }


@app.get("/admin/api/provider")
@auth.require_admin_ip
@auth.require_auth
@auth.require_admin
def admin_get_provider():
    return jsonify(_provider_view(config.provider()))


@app.post("/admin/api/provider")
@auth.require_admin_ip
@auth.require_auth
@auth.require_admin
def admin_set_provider():
    body = request.json or {}
    kind = (body.get("kind") or "anthropic").strip()
    if kind not in ("anthropic", "openai"):
        return jsonify({"error": "服务类型只能是 anthropic 或 openai"}), 400
    config.set_provider_override(
        kind=kind,
        base_url=(body.get("base_url") or "").strip(),
        model=(body.get("model") or "").strip(),
        max_tokens=int(body.get("max_tokens") or 8000),
        api_key=(body.get("api_key") or "").strip() or None,
    )
    return jsonify(_provider_view(config.provider()))


@app.post("/admin/api/provider/reset")
@auth.require_admin_ip
@auth.require_auth
@auth.require_admin
def admin_reset_provider():
    config.clear_provider_override()
    return jsonify(_provider_view(config.provider()))


@app.post("/admin/api/provider/test")
@auth.require_admin_ip
@auth.require_auth
@auth.require_admin
def admin_test_provider():
    """用表单里当前填的值（未保存也能测）试连一次，不落库。"""
    body = request.json or {}
    prov = dict(config.provider())
    if body.get("kind"):
        prov["kind"] = body["kind"]
    if body.get("base_url") is not None:
        prov["base_url"] = body["base_url"]
    if body.get("model"):
        prov["model"] = body["model"]
    if body.get("max_tokens"):
        prov["max_tokens"] = int(body["max_tokens"])
    if body.get("api_key"):
        prov["api_key"] = body["api_key"]
    try:
        be = backends.get_backend(prov)
        turn = be.step(system="", tools=[], messages=[be.user_message("ping，请只回复一个字确认连通")])
        return jsonify({"ok": True, "reply": turn.text[:120]})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 400


def main():
    import os

    config.load_env()
    web = config.settings().get("web", {})
    host = os.environ.get("RAGKERNEL_HOST") or web.get("host", "127.0.0.1")
    port = int(os.environ.get("RAGKERNEL_PORT") or web.get("port", 8360))
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
