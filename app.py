import argparse
import base64
import secrets
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, render_template_string
from werkzeug.utils import secure_filename

import relogin
from config_loader import ROOT, load_config, mail_db_path, save_config_patch
from mail_store import MailStore, normalize_email
from providers import create_provider_client, mask_secret, target_config


app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
JOBS = {}
JOBS_LOCK = threading.Lock()
RELOGIN_QUEUE_LOCK = threading.Lock()


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def current_config():
    cfg = load_config()
    relogin.configure(cfg)
    return cfg


def open_store(cfg=None):
    return MailStore(mail_db_path(cfg or current_config()))


def is_local_host(host):
    host = str(host or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1", "[::1]"}


def web_password():
    cfg = load_config()
    return str((cfg.get("web") or {}).get("password") or "").strip()


@app.before_request
def require_basic_auth():
    password = web_password()
    if not password:
        return None
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8", errors="replace")
            _, supplied = decoded.split(":", 1)
            if secrets.compare_digest(supplied, password):
                return None
        except Exception:
            pass
    return ("Authorization required", 401, {"WWW-Authenticate": 'Basic realm="relogin"'})


class Job:
    def __init__(self, kind, total=0, target=""):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.target = target
        self.status = "running"
        self.total = int(total or 0)
        self.done = 0
        self.ok = 0
        self.failed = 0
        self.started_at = utc_now()
        self.finished_at = ""
        self.error = ""
        self.logs = []
        self.lock = threading.Lock()

    def log(self, message):
        line = f"{utc_now()} {message}"
        with self.lock:
            self.logs.append(line)
            self.logs = self.logs[-500:]

    def finish(self, status="done", error=""):
        with self.lock:
            self.status = status
            self.error = str(error or "")
            self.finished_at = utc_now()

    def step(self, ok=False, failed=False):
        with self.lock:
            self.done += 1
            if ok:
                self.ok += 1
            if failed:
                self.failed += 1

    def to_dict(self):
        with self.lock:
            percent = round((self.done / self.total) * 100, 1) if self.total else 0
            return {
                "id": self.id,
                "kind": self.kind,
                "target": self.target,
                "status": self.status,
                "total": self.total,
                "done": self.done,
                "ok": self.ok,
                "failed": self.failed,
                "percent": percent,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
                "logs": list(self.logs),
            }


def start_job(kind, target, fn, total=0):
    job = Job(kind, total=total, target=target)
    with JOBS_LOCK:
        JOBS[job.id] = job

    def runner():
        try:
            fn(job)
            if job.status == "running":
                job.finish("done")
        except Exception as exc:
            job.log(f"任务失败: {exc}")
            job.finish("failed", str(exc))

    thread = threading.Thread(target=runner, name=f"relogin-job-{job.id}", daemon=True)
    thread.start()
    return job


def config_view(cfg):
    target = target_config(cfg)
    return {
        "target": {
            "provider": target["provider"],
            "base_url": target["base_url"],
            "management_key_masked": mask_secret(target["management_key"]),
            "enabled": target["enabled"],
            "sub2api_concurrency": target["sub2api_concurrency"],
            "sub2api_priority": target["sub2api_priority"],
        },
        "mail_db": str(mail_db_path(cfg)),
        "web": {
            "host": (cfg.get("web") or {}).get("host") or "127.0.0.1",
            "port": (cfg.get("web") or {}).get("port") or 8787,
            "password_masked": mask_secret((cfg.get("web") or {}).get("password") or ""),
        },
    }


def relogin_log_to_job(job):
    return relogin.log_to(lambda message: job.log(message))


def normalize_email_list(values):
    seen = set()
    emails = []
    for value in values or []:
        email = normalize_email(value)
        if email and email not in seen:
            seen.add(email)
            emails.append(email)
    return emails


def run_relogin_queued(email, store, provider, job):
    job.log(f"{email} 等待重登队列（1 并发）")
    with RELOGIN_QUEUE_LOCK:
        job.log(f"{email} 进入重登队列")
        with relogin_log_to_job(job):
            return relogin.relogin_by_email(email, store=store, provider=provider)


def sync_worker(job):
    cfg = current_config()
    provider = create_provider_client(cfg, require_config=True)
    store = open_store(cfg)
    try:
        accounts = store.list()
        job.total = len(accounts)
        job.log(f"读取本地邮箱 {len(accounts)} 个，目标 provider={provider.provider}")
        remotes = provider.list_accounts()
        job.log(f"远端账号 {len(remotes)} 个")
        for account in accounts:
            email = account["email"]
            remote = provider.find_match(account, remotes)
            if remote:
                store.set_status(email, "normal", "", **remote.store_fields())
                job.log(f"{email} -> normal ({remote.name or remote.remote_id or 'remote exists'})")
                job.step(ok=True)
                continue

            store.set_status(email, "dropped", "远端不存在，开始自动重新登录")
            job.log(f"{email} -> dropped，开始重登")
            try:
                result = run_relogin_queued(email, store, provider, job)
                action = ((result.get("provider") or {}).get("action") or "uploaded")
                job.log(f"{email} 重登成功并上传: {action}")
                job.step(ok=True)
            except Exception as exc:
                reason = str(exc)
                store.set_status(email, "unavailable", reason)
                job.log(f"{email} 重登失败: {reason}")
                job.step(failed=True)
    finally:
        store.close()


def relogin_worker(email):
    def run(job):
        cfg = current_config()
        provider = create_provider_client(cfg)
        store = open_store(cfg)
        try:
            job.total = 1
            job.log(f"{email} 开始重新登录")
            result = run_relogin_queued(email, store, provider, job)
            action = ((result.get("provider") or {}).get("action") or "local")
            job.log(f"{email} 完成: {action}")
            job.step(ok=True)
        except Exception as exc:
            store.set_status(email, "unavailable", str(exc))
            job.log(f"{email} 失败: {exc}")
            job.step(failed=True)
            raise
        finally:
            store.close()

    return run


def relogin_batch_worker(emails):
    def run(job):
        cfg = current_config()
        provider = create_provider_client(cfg)
        store = open_store(cfg)
        try:
            job.total = len(emails)
            job.log(f"批量重登 {len(emails)} 个邮箱，队列并发=1")
            for email in emails:
                try:
                    job.log(f"{email} 开始重新登录")
                    result = run_relogin_queued(email, store, provider, job)
                    action = ((result.get("provider") or {}).get("action") or "local")
                    job.log(f"{email} 完成: {action}")
                    job.step(ok=True)
                except Exception as exc:
                    store.set_status(email, "unavailable", str(exc))
                    job.log(f"{email} 失败: {exc}")
                    job.step(failed=True)
        finally:
            store.close()

    return run


HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>重新登录</title>
  <style>
    :root { color-scheme: light; --line:#d9dee8; --bg:#f7f8fb; --ink:#172033; --muted:#667085; --accent:#1f7a5b; --bad:#b42318; --warn:#a15c07; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: Arial, "Microsoft YaHei", sans-serif; color:var(--ink); background:var(--bg); font-size:14px; letter-spacing:0; }
    header { padding:18px 24px; background:#fff; border-bottom:1px solid var(--line); display:flex; align-items:center; justify-content:space-between; gap:16px; }
    h1 { font-size:20px; margin:0; font-weight:700; }
    main { max-width:1280px; margin:0 auto; padding:18px 24px 40px; display:grid; gap:16px; }
    section { background:#fff; border:1px solid var(--line); border-radius:8px; padding:14px; }
    h2 { margin:0 0 12px; font-size:15px; }
    .grid { display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap:10px; }
    label { display:grid; gap:5px; color:var(--muted); font-size:12px; }
    input, select, button { height:36px; border:1px solid var(--line); border-radius:6px; padding:0 10px; background:#fff; color:var(--ink); font:inherit; min-width:0; }
    button { cursor:pointer; background:#172033; color:#fff; border-color:#172033; white-space:nowrap; }
    button.secondary { background:#fff; color:#172033; }
    button.danger { background:var(--bad); border-color:var(--bad); }
    button:disabled { opacity:.55; cursor:not-allowed; }
    .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:end; }
    .summary { display:flex; flex-wrap:wrap; gap:8px; }
    .pill { border:1px solid var(--line); border-radius:999px; padding:6px 10px; background:#fff; }
    .normal { color:var(--accent); }
    .dropped, .unknown { color:var(--warn); }
    .unavailable { color:var(--bad); }
    table { width:100%; border-collapse:collapse; table-layout:fixed; }
    th, td { border-bottom:1px solid var(--line); padding:9px 8px; text-align:left; vertical-align:top; overflow:hidden; text-overflow:ellipsis; }
    th { color:var(--muted); font-size:12px; font-weight:600; background:#fbfcfe; }
    td.actions { width:122px; }
    th.select, td.select { width:42px; text-align:center; }
    input[type="checkbox"] { width:16px; height:16px; }
    .selected-count { color:var(--muted); font-size:13px; }
    .progress { height:10px; border-radius:999px; background:#edf0f5; overflow:hidden; }
    .bar { height:100%; width:0; background:var(--accent); transition:width .2s; }
    pre { margin:8px 0 0; padding:10px; background:#101828; color:#e6edf7; border-radius:8px; min-height:180px; max-height:360px; overflow:auto; white-space:pre-wrap; }
    @media (max-width: 860px) { .grid { grid-template-columns:1fr 1fr; } table { table-layout:auto; } .hide-sm { display:none; } }
  </style>
</head>
<body>
  <header>
    <h1>重新登录</h1>
    <div id="dbPath"></div>
  </header>
  <main>
    <section>
      <h2>目标配置</h2>
      <form id="configForm" class="grid">
        <label>Provider
          <select name="provider">
            <option value="">仅本地保存</option>
            <option value="cpa">CPA</option>
            <option value="sub2api">Sub2API</option>
          </select>
        </label>
        <label>地址<input name="base_url" placeholder="https://example.com"></label>
        <label>管理密钥<input name="management_key" placeholder="留空则不改"></label>
        <label>Sub2API 并发<input name="sub2api_concurrency" type="number" min="1" max="50"></label>
        <label>Sub2API 优先级<input name="sub2api_priority" type="number" min="1" max="1000"></label>
        <div class="toolbar"><button type="submit">保存配置</button></div>
      </form>
    </section>

    <section>
      <h2>导入与同步</h2>
      <div class="toolbar">
        <form id="importForm" class="toolbar">
          <input name="file" type="file" accept=".zip" required>
          <button type="submit">导入 ZIP</button>
        </form>
        <button id="syncBtn">全量更新</button>
      </div>
      <div class="summary" id="summary"></div>
    </section>

    <section>
      <h2>邮箱列表</h2>
      <div class="toolbar">
        <label>搜索<input id="search" placeholder="email / phone / reason"></label>
        <label>状态
          <select id="status">
            <option value="">全部</option>
            <option value="unknown">unknown</option>
            <option value="normal">normal</option>
            <option value="dropped">dropped</option>
            <option value="unavailable">unavailable</option>
          </select>
        </label>
        <button class="secondary" id="selectVisibleBtn">选择当前结果</button>
        <button class="secondary" id="clearSelectionBtn">清空选择</button>
        <button id="batchReloginBtn">批量重登</button>
        <span class="selected-count" id="selectedCount">已选 0</span>
        <button class="secondary" id="refreshBtn">刷新</button>
      </div>
      <table>
        <thead>
          <tr>
            <th class="select"><input id="selectAllVisible" type="checkbox" title="选择当前结果"></th><th>Email</th><th class="hide-sm">Phone</th><th>Status</th><th>Remote</th><th>Failure</th><th class="actions">Action</th>
          </tr>
        </thead>
        <tbody id="accounts"></tbody>
      </table>
    </section>

    <section>
      <h2>任务</h2>
      <div id="jobMeta"></div>
      <div class="progress"><div class="bar" id="jobBar"></div></div>
      <pre id="jobLogs"></pre>
    </section>
  </main>
  <script>
    let currentJob = "";
    let visibleEmails = [];
    const selectedEmails = new Set();
    const $ = (id) => document.getElementById(id);
    const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

    async function api(url, options={}) {
      const res = await fetch(url, options);
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.error) throw new Error(data.error || data.message || res.statusText);
      return data;
    }

    async function loadState() {
      const q = new URLSearchParams({search: $("search").value, status: $("status").value});
      const data = await api("/api/accounts?" + q.toString());
      $("dbPath").textContent = data.config.mail_db;
      const t = data.config.target;
      const form = $("configForm");
      form.provider.value = t.provider || "";
      form.base_url.value = t.base_url || "";
      form.management_key.placeholder = t.management_key_masked || "留空则不改";
      form.sub2api_concurrency.value = t.sub2api_concurrency || 3;
      form.sub2api_priority.value = t.sub2api_priority || 50;
      $("summary").innerHTML = Object.entries(data.summary).map(([k,v]) => `<span class="pill ${esc(k)}">${esc(k)}: ${esc(v)}</span>`).join("");
      visibleEmails = data.accounts.map(row => row.email);
      $("accounts").innerHTML = data.accounts.map(row => `
        <tr>
          <td class="select"><input class="rowCheck" type="checkbox" data-email="${esc(row.email)}" ${selectedEmails.has(row.email) ? "checked" : ""}></td>
          <td title="${esc(row.email)}">${esc(row.email)}</td>
          <td class="hide-sm">${esc(row.phone_number)}</td>
          <td class="${esc(row.status)}">${esc(row.status)}</td>
          <td title="${esc(row.remote_provider + " " + row.remote_name)}">${esc(row.remote_provider || "-")} ${esc(row.remote_name || row.remote_id || "")}</td>
          <td title="${esc(row.failure_reason)}">${esc(row.failure_reason)}</td>
          <td class="actions"><button class="secondary" data-email="${esc(row.email)}">重登</button></td>
        </tr>`).join("");
      document.querySelectorAll("button[data-email]").forEach(btn => btn.onclick = () => startRelogin(btn.dataset.email));
      document.querySelectorAll(".rowCheck").forEach(box => {
        box.onchange = () => {
          if (box.checked) selectedEmails.add(box.dataset.email);
          else selectedEmails.delete(box.dataset.email);
          updateSelectionUi();
        };
      });
      updateSelectionUi();
    }

    function updateSelectionUi() {
      $("selectedCount").textContent = `已选 ${selectedEmails.size}`;
      $("batchReloginBtn").disabled = selectedEmails.size === 0;
      const allVisibleSelected = visibleEmails.length > 0 && visibleEmails.every(email => selectedEmails.has(email));
      $("selectAllVisible").checked = allVisibleSelected;
    }

    async function startRelogin(email) {
      const data = await api("/api/relogin/" + encodeURIComponent(email), {method:"POST"});
      currentJob = data.job_id;
      pollJob();
    }

    async function startBatchRelogin() {
      const emails = Array.from(selectedEmails);
      if (!emails.length) return;
      const data = await api("/api/relogin-batch", {
        method:"POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({emails})
      });
      currentJob = data.job_id;
      pollJob();
    }

    async function pollJob() {
      if (!currentJob) return;
      const job = await api("/api/jobs/" + currentJob);
      $("jobMeta").textContent = `${job.kind} ${job.status} ${job.done}/${job.total} ok=${job.ok} failed=${job.failed}`;
      $("jobBar").style.width = `${job.percent || 0}%`;
      $("jobLogs").textContent = (job.logs || []).join("\\n");
      $("jobLogs").scrollTop = $("jobLogs").scrollHeight;
      if (job.status === "running") {
        setTimeout(pollJob, 1500);
      } else {
        loadState().catch(alert);
      }
    }

    $("configForm").onsubmit = async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.currentTarget);
      await api("/api/config", {method:"POST", body:fd});
      ev.currentTarget.management_key.value = "";
      await loadState();
    };
    $("importForm").onsubmit = async (ev) => {
      ev.preventDefault();
      await api("/api/import-zip", {method:"POST", body:new FormData(ev.currentTarget)});
      ev.currentTarget.reset();
      await loadState();
    };
    $("syncBtn").onclick = async () => {
      const data = await api("/api/sync", {method:"POST"});
      currentJob = data.job_id;
      pollJob();
    };
    $("refreshBtn").onclick = () => loadState().catch(alert);
    $("selectVisibleBtn").onclick = () => {
      visibleEmails.forEach(email => selectedEmails.add(email));
      loadState().catch(alert);
    };
    $("clearSelectionBtn").onclick = () => {
      selectedEmails.clear();
      loadState().catch(alert);
    };
    $("selectAllVisible").onchange = (ev) => {
      if (ev.currentTarget.checked) visibleEmails.forEach(email => selectedEmails.add(email));
      else visibleEmails.forEach(email => selectedEmails.delete(email));
      loadState().catch(alert);
    };
    $("batchReloginBtn").onclick = () => startBatchRelogin().catch(alert);
    $("search").oninput = () => loadState().catch(alert);
    $("status").onchange = () => loadState().catch(alert);
    loadState().catch(err => { $("jobLogs").textContent = err.message; });
  </script>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(HTML)


@app.get("/api/accounts")
def api_accounts():
    cfg = current_config()
    store = open_store(cfg)
    try:
        accounts = store.list(status=request.args.get("status", ""), search=request.args.get("search", ""))
        return jsonify({"accounts": accounts, "summary": store.summary(), "config": config_view(cfg)})
    finally:
        store.close()


@app.post("/api/config")
def api_config():
    cfg = current_config()
    existing_target = target_config(cfg)
    payload = request.get_json(silent=True) or request.form.to_dict()
    provider = str(payload.get("provider") or "").strip().lower()
    base_url = str(payload.get("base_url") or "").strip()
    key = str(payload.get("management_key") or "").strip()
    target = {
        "provider": provider,
        "base_url": base_url,
        "management_key": key or existing_target["management_key"],
        "sub2api_concurrency": int(payload.get("sub2api_concurrency") or existing_target["sub2api_concurrency"]),
        "sub2api_priority": int(payload.get("sub2api_priority") or existing_target["sub2api_priority"]),
    }
    updated = save_config_patch({"target": target})
    relogin.configure(updated)
    return jsonify({"ok": True, "config": config_view(updated)})


@app.post("/api/import-zip")
def api_import_zip():
    upload = request.files.get("file")
    if not upload:
        return jsonify({"error": "missing file"}), 400
    cfg = current_config()
    imports_dir = ROOT / "data" / "imports"
    imports_dir.mkdir(parents=True, exist_ok=True)
    filename = secure_filename(upload.filename or "bundle.zip")
    path = imports_dir / f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{filename}"
    upload.save(path)
    store = open_store(cfg)
    try:
        result = store.import_zip(path)
        return jsonify({"ok": True, "result": result})
    finally:
        store.close()


@app.post("/api/sync")
def api_sync():
    cfg = current_config()
    create_provider_client(cfg, require_config=True)
    job = start_job("sync", "all", sync_worker)
    return jsonify({"job_id": job.id})


@app.post("/api/relogin/<path:email>")
def api_relogin(email):
    email = normalize_email(email)
    if not email:
        return jsonify({"error": "invalid email"}), 400
    job = start_job("relogin", email, relogin_worker(email), total=1)
    return jsonify({"job_id": job.id})


@app.post("/api/relogin-batch")
def api_relogin_batch():
    payload = request.get_json(silent=True) or {}
    emails = normalize_email_list(payload.get("emails") or [])
    if not emails:
        return jsonify({"error": "no emails selected"}), 400
    job = start_job("relogin-batch", f"{len(emails)} emails", relogin_batch_worker(emails), total=len(emails))
    return jsonify({"job_id": job.id, "count": len(emails)})


@app.get("/api/jobs/<job_id>")
def api_job(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job.to_dict())


def main():
    cfg = current_config()
    parser = argparse.ArgumentParser(description="Local relogin mailbox management web UI.")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default=(cfg.get("web") or {}).get("host") or "127.0.0.1")
    serve.add_argument("--port", type=int, default=int((cfg.get("web") or {}).get("port") or 8787))
    args = parser.parse_args()
    if args.cmd != "serve":
        parser.print_help()
        raise SystemExit(2)
    password = str((cfg.get("web") or {}).get("password") or "").strip()
    if not is_local_host(args.host) and not password:
        raise SystemExit("Web 监听非 localhost 时必须先配置 web.password。")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
