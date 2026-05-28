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
FRONTEND_TEMPLATE_PATH = ROOT / "templates" / "index.html"


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
  <title>邮箱重新登录管理器</title>
  <style>
    :root {
      color-scheme: light;
      --bg:#f5f7fb;
      --panel:#ffffff;
      --panel-soft:#f8fafd;
      --line:#d8e0ec;
      --line-soft:#edf1f6;
      --ink:#111827;
      --muted:#64748b;
      --muted-2:#94a3b8;
      --brand:#0f6ea8;
      --brand-soft:#eaf5ff;
      --accent:#18a957;
      --accent-soft:#eafaf0;
      --bad:#dc2626;
      --bad-soft:#fff1f2;
      --warn:#d97706;
      --warn-soft:#fff7ed;
      --dark:#111827;
      --shadow:0 16px 42px rgba(15, 23, 42, .07);
      --radius:8px;
    }
    * { box-sizing: border-box; }
    html { height:100%; }
    body {
      min-height:100%;
      margin:0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      color:var(--ink);
      background:
        radial-gradient(circle at 18% 0%, rgba(15, 110, 168, .08), transparent 28%),
        linear-gradient(180deg, #fbfcff 0%, var(--bg) 36%, #f2f5f9 100%);
      font-size:14px;
      letter-spacing:0;
    }
    svg { width:16px; height:16px; stroke-width:2; flex:0 0 auto; }
    button svg, .nav-item svg, .top-icon svg, .brand-icon svg { display:block; }
    .app-shell { min-height:100vh; display:grid; grid-template-columns:216px minmax(0, 1fr); }
    .sidebar {
      position:sticky;
      top:0;
      height:100vh;
      padding:18px 14px;
      background:rgba(255, 255, 255, .86);
      border-right:1px solid var(--line-soft);
      backdrop-filter: blur(18px);
      display:flex;
      flex-direction:column;
      gap:22px;
    }
    .brand { display:flex; align-items:center; gap:12px; padding:2px 10px 14px; border-bottom:1px solid var(--line-soft); }
    .brand-icon {
      width:36px;
      height:36px;
      border-radius:7px;
      display:grid;
      place-items:center;
      color:#fff;
      background:linear-gradient(135deg, #0f6ea8, #0b4f7b);
      box-shadow:0 12px 24px rgba(15, 110, 168, .22);
    }
    .brand-title { min-width:0; }
    .brand-title strong { display:block; font-size:15px; line-height:1.2; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .brand-title span { display:block; margin-top:3px; color:var(--muted); font-size:12px; }
    .nav { display:grid; gap:6px; }
    .nav-item {
      height:44px;
      border:0;
      border-radius:7px;
      padding:0 14px;
      color:#334155;
      background:transparent;
      display:flex;
      align-items:center;
      gap:12px;
      font-weight:600;
      text-decoration:none;
    }
    .nav-item.active { color:var(--brand); background:linear-gradient(90deg, #edf6ff, #f8fbff); }
    .nav-spacer { flex:1; }
    .collapse-note { color:var(--muted); display:flex; justify-content:space-between; padding:0 10px 4px; font-size:12px; }
    .workspace { min-width:0; }
    .topbar {
      height:64px;
      padding:0 26px;
      display:flex;
      align-items:center;
      justify-content:space-between;
      background:rgba(255, 255, 255, .78);
      border-bottom:1px solid var(--line-soft);
      backdrop-filter: blur(18px);
      position:sticky;
      top:0;
      z-index:5;
    }
    .page-title { display:flex; align-items:center; gap:12px; min-width:0; }
    .page-title h1 { margin:0; font-size:20px; font-weight:800; letter-spacing:0; }
    .page-title span { color:var(--muted); font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:50vw; }
    .top-actions { display:flex; align-items:center; gap:12px; color:#0f172a; }
    .top-icon { width:34px; height:34px; display:grid; place-items:center; border:0; color:#0f172a; background:transparent; border-radius:7px; }
    .avatar { width:34px; height:34px; border-radius:50%; display:grid; place-items:center; background:#0f6ea8; color:#fff; font-weight:800; }
    .user-name { display:flex; align-items:center; gap:8px; font-weight:700; }
    main { padding:14px 24px 28px; display:grid; gap:10px; max-width:1500px; margin:0 auto; }
    section {
      background:rgba(255, 255, 255, .92);
      border:1px solid var(--line-soft);
      border-radius:var(--radius);
      box-shadow:var(--shadow);
      padding:20px 22px;
    }
    .section-title { display:flex; align-items:center; justify-content:space-between; gap:12px; margin:0 0 18px; }
    .section-title h2 { margin:0; font-size:16px; line-height:1.2; font-weight:800; display:flex; align-items:center; gap:10px; }
    .section-dot { width:8px; height:8px; border-radius:50%; background:#3b82f6; box-shadow:0 0 0 4px rgba(59, 130, 246, .12); }
    .grid { display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap:16px 20px; align-items:end; }
    .field { display:grid; gap:7px; color:#475569; font-size:13px; font-weight:600; }
    input, select, button {
      height:42px;
      border:1px solid var(--line);
      border-radius:7px;
      padding:0 12px;
      background:#fff;
      color:var(--ink);
      font:inherit;
      min-width:0;
      outline:none;
      transition:border-color .16s, box-shadow .16s, background .16s, transform .16s;
    }
    input:focus, select:focus {
      border-color:#8bbcf0;
      box-shadow:0 0 0 3px rgba(59, 130, 246, .13);
    }
    button {
      cursor:pointer;
      background:linear-gradient(180deg, #1f2937, #111827);
      color:#fff;
      border-color:#111827;
      white-space:nowrap;
      display:inline-flex;
      align-items:center;
      justify-content:center;
      gap:8px;
      font-weight:700;
      box-shadow:0 10px 18px rgba(15, 23, 42, .13);
    }
    button:hover { transform:translateY(-1px); }
    button.secondary {
      background:#fff;
      color:#172033;
      border-color:#cfd8e6;
      box-shadow:none;
    }
    button.ghost {
      background:transparent;
      color:#334155;
      border-color:transparent;
      box-shadow:none;
    }
    button.danger { background:var(--bad); border-color:var(--bad); }
    button:disabled { opacity:.5; cursor:not-allowed; transform:none; }
    .toolbar { display:flex; flex-wrap:wrap; gap:12px; align-items:end; }
    .summary { display:flex; flex-wrap:wrap; gap:12px; margin-top:12px; }
    .pill {
      min-height:28px;
      border:1px solid transparent;
      border-radius:999px;
      padding:5px 14px;
      display:inline-flex;
      align-items:center;
      gap:7px;
      font-weight:700;
      background:#f3f6fb;
      color:#475569;
    }
    .pill::before { content:""; width:7px; height:7px; border-radius:50%; background:currentColor; opacity:.8; }
    .pill.normal { color:#0a9f4d; background:var(--accent-soft); }
    .pill.dropped { color:#c46b02; background:var(--warn-soft); }
    .pill.unavailable { color:#dc2626; background:var(--bad-soft); }
    .pill.unknown { color:#475569; background:#eef2f7; }
    .pill.total { color:#2563eb; background:#eff6ff; }
    .upload-zone {
      width:340px;
      flex:0 0 340px;
      min-height:88px;
      border:1px dashed #cbd5e1;
      border-radius:7px;
      background:linear-gradient(180deg, #fbfdff, #f8fafc);
      display:grid;
      grid-template-columns:46px 1fr;
      align-items:center;
      gap:14px;
      padding:14px 18px;
      color:#334155;
      cursor:pointer;
    }
    .upload-zone svg { width:30px; height:30px; color:#64748b; }
    .upload-zone strong { display:block; margin-bottom:5px; font-size:14px; }
    .upload-zone span { color:var(--muted); font-size:13px; }
    .upload-zone input { display:none; }
    #importForm { flex:0 1 auto; flex-wrap:nowrap; align-items:center; }
    .table-toolbar { justify-content:space-between; margin-bottom:8px; }
    .table-toolbar-left, .table-toolbar-right { display:flex; flex-wrap:wrap; align-items:end; gap:12px; }
    .search-field { position:relative; min-width:260px; }
    .search-field input { width:100%; padding-right:38px; }
    .search-field svg { position:absolute; right:12px; bottom:12px; color:#64748b; }
    .selected-count { color:#334155; font-size:13px; font-weight:600; }
    .table-wrap { overflow:auto; border-radius:7px; border:1px solid var(--line-soft); background:#fff; }
    table { width:100%; border-collapse:collapse; table-layout:fixed; min-width:940px; }
    th, td {
      border-bottom:1px solid var(--line-soft);
      padding:12px 12px;
      text-align:left;
      vertical-align:middle;
      overflow:hidden;
      text-overflow:ellipsis;
    }
    th { color:#64748b; font-size:12px; font-weight:800; background:#f8fafc; }
    tbody tr { transition:background .16s; }
    tbody tr:hover { background:#f8fbff; }
    tbody tr.row-selected { background:#f0f6ff; }
    tbody tr:last-child td { border-bottom:0; }
    th.select, td.select { width:46px; text-align:center; }
    td.status-cell { width:130px; }
    td.actions, th.actions { width:112px; }
    .email-cell {
      font-weight:700;
      white-space:normal;
      overflow-wrap:anywhere;
      line-height:1.35;
    }
    input[type="checkbox"] {
      width:18px;
      height:18px;
      padding:0;
      accent-color:#111827;
    }
    .status-badge {
      display:inline-flex;
      align-items:center;
      gap:7px;
      min-height:26px;
      padding:3px 11px;
      border-radius:999px;
      font-weight:800;
      font-size:13px;
      background:#eef2f7;
      color:#475569;
    }
    .status-badge::before { content:""; width:7px; height:7px; border-radius:50%; background:currentColor; }
    .status-badge.normal { color:#0a9f4d; background:var(--accent-soft); }
    .status-badge.dropped, .status-badge.unknown { color:#c46b02; background:var(--warn-soft); }
    .status-badge.unavailable { color:#dc2626; background:var(--bad-soft); }
    .failure-text {
      color:#64748b;
      white-space:normal;
      overflow:visible;
      text-overflow:clip;
      overflow-wrap:anywhere;
      line-height:1.45;
    }
    .table-footer { display:flex; justify-content:space-between; align-items:center; padding:13px 4px 0; color:#334155; font-weight:600; }
    .pagination { display:flex; align-items:center; gap:6px; }
    .page-btn { width:34px; height:34px; padding:0; }
    .page-current { width:34px; height:34px; border-radius:7px; display:grid; place-items:center; background:#111827; color:#fff; font-weight:800; }
    .job-head { display:flex; align-items:flex-end; justify-content:space-between; gap:14px; margin-bottom:10px; }
    .job-meta { display:grid; gap:7px; min-width:0; }
    .job-line { color:#334155; font-weight:700; }
    .job-detail { color:#64748b; font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:70vw; }
    .job-actions { display:flex; align-items:center; gap:8px; }
    .icon-btn { width:38px; height:38px; padding:0; }
    .progress-row { display:grid; grid-template-columns:minmax(0,1fr) 48px; gap:12px; align-items:center; }
    .progress { height:10px; border-radius:999px; background:#e9edf4; overflow:hidden; }
    .bar { height:100%; width:0; background:linear-gradient(90deg, #22c55e, #16a34a); transition:width .2s; }
    .percent { color:#334155; font-weight:800; text-align:right; }
    .terminal {
      margin:12px 0 0;
      background:linear-gradient(135deg, #111827, #182235);
      color:#dbeafe;
      border:1px solid rgba(148, 163, 184, .18);
      border-radius:7px;
      box-shadow:inset 0 1px 0 rgba(255, 255, 255, .04);
      min-height:178px;
      max-height:360px;
      overflow:auto;
      counter-reset: line;
      font:13px/1.55 "JetBrains Mono", "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    }
    .log-line {
      display:grid;
      grid-template-columns:36px minmax(0, 1fr);
      gap:14px;
      min-height:22px;
      padding:0 14px;
      white-space:pre-wrap;
    }
    .log-line:first-child { padding-top:10px; }
    .log-line:last-child { padding-bottom:10px; }
    .log-line::before {
      counter-increment: line;
      content:counter(line);
      color:#94a3b8;
      text-align:right;
      user-select:none;
    }
    .log-ok { color:#86efac; font-weight:700; }
    .log-bad { color:#fca5a5; font-weight:700; }
    .log-info { color:#93c5fd; font-weight:700; }
    .empty-state { color:#64748b; padding:20px; text-align:center; }
    .hidden { display:none !important; }
    @media (max-width: 1100px) {
      .app-shell { grid-template-columns:74px minmax(0, 1fr); }
      .sidebar { padding:16px 10px; }
      .brand-title, .nav-item span, .collapse-note { display:none; }
      .brand { justify-content:center; padding:0 0 14px; }
      .nav-item { justify-content:center; padding:0; }
      .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 760px) {
      .app-shell { display:block; }
      .sidebar { display:none; }
      .topbar { height:auto; padding:14px 16px; align-items:flex-start; gap:12px; }
      .page-title { align-items:flex-start; flex-direction:column; gap:5px; }
      .top-actions { display:none; }
      main { padding:12px; }
      section { padding:16px 14px; }
      .grid { grid-template-columns:1fr; }
      .table-toolbar { align-items:stretch; }
      .table-toolbar-left, .table-toolbar-right { width:100%; }
      .search-field { min-width:100%; }
      #importForm { flex-wrap:wrap; width:100%; }
      .upload-zone { width:100%; flex-basis:100%; }
      button { width:auto; }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-icon" data-icon="mail"></div>
        <div class="brand-title">
          <strong>邮箱重登</strong>
          <span>Relogin Manager</span>
        </div>
      </div>
      <nav class="nav" aria-label="主导航">
        <a class="nav-item active" href="#dashboard"><span data-icon="home"></span><span>控制台</span></a>
        <a class="nav-item" href="#config"><span data-icon="settings"></span><span>配置管理</span></a>
        <a class="nav-item" href="#jobs"><span data-icon="clipboard-list"></span><span>任务日志</span></a>
        <a class="nav-item" href="#accounts"><span data-icon="users"></span><span>账号管理</span></a>
        <a class="nav-item" href="#system"><span data-icon="sliders"></span><span>系统设置</span></a>
      </nav>
      <div class="nav-spacer"></div>
      <div class="collapse-note"><span>收起菜单</span><span>«</span></div>
    </aside>
    <div class="workspace">
      <header class="topbar">
        <div class="page-title">
          <h1>邮箱重新登录管理器 / Relogin Mail Manager</h1>
          <span id="dbPath"></span>
        </div>
        <div class="top-actions" aria-label="用户操作">
          <button class="top-icon" type="button" title="深色模式"><span data-icon="moon"></span></button>
          <button class="top-icon" type="button" title="通知"><span data-icon="bell"></span></button>
          <div class="avatar">A</div>
          <div class="user-name">admin <span data-icon="chevron-down"></span></div>
        </div>
      </header>
      <main id="dashboard">
        <section id="config">
          <div class="section-title">
            <h2><span class="section-dot"></span>1. Provider 配置</h2>
          </div>
          <form id="configForm" class="grid">
            <label class="field">Provider
              <select name="provider">
                <option value="">仅本地保存</option>
                <option value="cpa">CPA</option>
                <option value="sub2api">Sub2API</option>
              </select>
            </label>
            <label class="field">地址<input name="base_url" placeholder="https://example.com"></label>
            <label class="field">管理密钥<input name="management_key" type="password" placeholder="留空则不改"></label>
            <label class="field">Sub2API 并发<input name="sub2api_concurrency" type="number" min="1" max="50"></label>
            <label class="field">Sub2API 优先级<input name="sub2api_priority" type="number" min="1" max="1000"></label>
            <div class="toolbar"><button type="submit"><span data-icon="save"></span>保存配置</button></div>
          </form>
        </section>

        <section>
          <div class="section-title">
            <h2><span class="section-dot"></span>2. 导入与同步</h2>
          </div>
          <div class="toolbar">
            <form id="importForm" class="toolbar">
              <label class="upload-zone">
                <span data-icon="file-archive"></span>
                <span><strong id="uploadText">选择 ZIP 文件或拖拽到此处</strong><span>支持 .zip 格式</span></span>
                <input name="file" id="zipFile" type="file" accept=".zip" required>
              </label>
              <button type="submit"><span data-icon="upload"></span>导入 ZIP</button>
            </form>
            <button class="secondary" id="syncBtn"><span data-icon="refresh-cw"></span>全量更新</button>
          </div>
          <div class="summary" id="summary"></div>
        </section>

        <section id="accounts">
          <div class="section-title">
            <h2><span class="section-dot"></span>3. 邮箱列表</h2>
            <span class="selected-count" id="selectedCount">已选 0 项</span>
          </div>
          <div class="table-toolbar toolbar">
            <div class="table-toolbar-left">
              <label class="field search-field">搜索
                <input id="search" placeholder="搜索 email / phone / reason">
                <span data-icon="search"></span>
              </label>
              <label class="field">状态
                <select id="status">
                  <option value="">全部</option>
                  <option value="unknown">unknown</option>
                  <option value="normal">normal</option>
                  <option value="dropped">dropped</option>
                  <option value="unavailable">unavailable</option>
                </select>
              </label>
            </div>
            <div class="table-toolbar-right">
              <button class="secondary" id="selectVisibleBtn"><span data-icon="check-square"></span>选择当前结果</button>
              <button class="secondary" id="clearSelectionBtn"><span data-icon="x-square"></span>清空选择</button>
              <button id="batchReloginBtn"><span data-icon="rotate-cw"></span>批量重登</button>
              <button class="secondary" id="refreshBtn"><span data-icon="refresh-cw"></span>刷新</button>
            </div>
          </div>
          <div class="table-wrap">
            <table>
              <colgroup>
                <col style="width:46px;">
                <col style="width:230px;">
                <col style="width:150px;">
                <col style="width:130px;">
                <col style="width:190px;">
                <col>
                <col style="width:112px;">
              </colgroup>
              <thead>
                <tr>
                  <th class="select"><input id="selectAllVisible" type="checkbox" title="选择当前结果"></th>
                  <th>Email</th>
                  <th>Phone</th>
                  <th>Status</th>
                  <th>Remote</th>
                  <th>Failure</th>
                  <th class="actions">Action</th>
                </tr>
              </thead>
              <tbody id="accountsBody"></tbody>
            </table>
          </div>
          <div class="table-footer">
            <span id="recordCount">共 0 条记录</span>
            <div class="pagination">
              <button class="secondary page-btn" type="button" disabled><span data-icon="chevron-left"></span></button>
              <span class="page-current">1</span>
              <button class="secondary page-btn" type="button" disabled><span data-icon="chevron-right"></span></button>
              <select id="pageSize" aria-label="每页数量">
                <option>10 条/页</option>
                <option>20 条/页</option>
                <option>50 条/页</option>
              </select>
            </div>
          </div>
        </section>

        <section id="jobs">
          <div class="section-title">
            <h2>4. 任务日志</h2>
            <div class="job-actions">
              <button class="secondary icon-btn" type="button" id="restoreLogBtn" title="恢复最近任务"><span data-icon="clock"></span></button>
              <button class="secondary icon-btn" type="button" id="clearLogBtn" title="清空日志"><span data-icon="trash-2"></span></button>
              <button class="secondary icon-btn" type="button" id="downloadLogBtn" title="下载日志"><span data-icon="download"></span></button>
            </div>
          </div>
          <div class="job-head">
            <div class="job-meta">
              <div class="job-line" id="jobMeta">暂无任务</div>
              <div class="job-detail" id="jobDetail">等待操作</div>
            </div>
          </div>
          <div class="progress-row">
            <div class="progress"><div class="bar" id="jobBar"></div></div>
            <div class="percent" id="jobPercent">0%</div>
          </div>
          <div class="terminal" id="jobLogs" aria-live="polite"></div>
        </section>
      </main>
    </div>
  </div>
  <script>
    let currentJob = "";
    let pollTimer = 0;
    let latestTimer = 0;
    let visibleEmails = [];
    let latestJobSnapshot = null;
    const selectedEmails = new Set();
    const $ = (id) => document.getElementById(id);
    const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const icons = {
      mail: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="3" y="5" width="18" height="14" rx="2"></rect><path d="m3 7 9 6 9-6"></path></svg>',
      home: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m3 11 9-8 9 8"></path><path d="M5 10v10h14V10"></path><path d="M9 20v-6h6v6"></path></svg>',
      settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z"></path><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1A2 2 0 1 1 4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1A2 2 0 1 1 7.1 4.2l.1.1a1.7 1.7 0 0 0 1.9.3h.1a1.7 1.7 0 0 0 1-1.6V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1A2 2 0 1 1 20 7.1l-.1.1a1.7 1.7 0 0 0-.3 1.9v.1a1.7 1.7 0 0 0 1.6 1h.1a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.8.8Z"></path></svg>',
      'clipboard-list': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="8" y="2" width="8" height="4" rx="1"></rect><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"></path><path d="M8 12h8"></path><path d="M8 16h8"></path></svg>',
      users: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M22 21v-2a4 4 0 0 0-3-3.9"></path><path d="M16 3.1a4 4 0 0 1 0 7.8"></path></svg>',
      sliders: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M4 21v-7"></path><path d="M4 10V3"></path><path d="M12 21v-9"></path><path d="M12 8V3"></path><path d="M20 21v-5"></path><path d="M20 12V3"></path><path d="M2 14h4"></path><path d="M10 8h4"></path><path d="M18 16h4"></path></svg>',
      moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M12 3a6 6 0 0 0 9 7.2A9 9 0 1 1 12 3Z"></path></svg>',
      bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M10 21h4"></path><path d="M18 8a6 6 0 1 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"></path></svg>',
      'chevron-down': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m6 9 6 6 6-6"></path></svg>',
      'chevron-left': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m15 18-6-6 6-6"></path></svg>',
      'chevron-right': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="m9 18 6-6-6-6"></path></svg>',
      save: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z"></path><path d="M17 21v-8H7v8"></path><path d="M7 3v5h8"></path></svg>',
      'file-archive': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"></path><path d="M14 2v6h6"></path><path d="M10 12h4"></path><path d="M10 16h4"></path></svg>',
      upload: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><path d="m17 8-5-5-5 5"></path><path d="M12 3v12"></path></svg>',
      'refresh-cw': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 12a9 9 0 0 1-15.5 6.2L3 16"></path><path d="M3 12A9 9 0 0 1 18.5 5.8L21 8"></path><path d="M3 16v5h5"></path><path d="M21 8V3h-5"></path></svg>',
      search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="11" cy="11" r="8"></circle><path d="m21 21-4.3-4.3"></path></svg>',
      'check-square': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="3" y="3" width="18" height="18" rx="2"></rect><path d="m9 12 2 2 4-4"></path></svg>',
      'x-square': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="3" y="3" width="18" height="18" rx="2"></rect><path d="m9 9 6 6"></path><path d="m15 9-6 6"></path></svg>',
      'rotate-cw': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 12a9 9 0 1 1-3-6.7"></path><path d="M21 3v6h-6"></path></svg>',
      clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path></svg>',
      trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M3 6h18"></path><path d="M8 6V4h8v2"></path><path d="m19 6-1 14H6L5 6"></path></svg>',
      'trash-2': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M3 6h18"></path><path d="M8 6V4h8v2"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="m19 6-1 14H6L5 6"></path></svg>',
      download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><path d="M7 10l5 5 5-5"></path><path d="M12 15V3"></path></svg>'
    };

    function renderIcons(root=document) {
      root.querySelectorAll("[data-icon]").forEach(node => {
        const name = node.dataset.icon;
        if (icons[name]) node.innerHTML = icons[name];
      });
    }

    async function api(url, options={}) {
      const res = await fetch(url, options);
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.error) throw new Error(data.error || data.message || res.statusText);
      return data;
    }

    async function loadState({quiet=false}={}) {
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
      $("recordCount").textContent = `共 ${data.accounts.length} 条记录`;
      $("accountsBody").innerHTML = data.accounts.length ? data.accounts.map(row => `
        <tr class="${selectedEmails.has(row.email) ? "row-selected" : ""}">
          <td class="select"><input class="rowCheck" type="checkbox" data-email="${esc(row.email)}" ${selectedEmails.has(row.email) ? "checked" : ""}></td>
          <td class="email-cell" title="${esc(row.email)}">${esc(row.email)}</td>
          <td>${esc(row.phone_number || "-")}</td>
          <td class="status-cell"><span class="status-badge ${esc(row.status)}">${esc(row.status || "unknown")}</span></td>
          <td title="${esc(row.remote_provider + " " + row.remote_name)}">${esc(row.remote_provider || "-")} ${esc(row.remote_name || row.remote_id || "")}</td>
          <td class="failure-text" title="${esc(row.failure_reason)}">${esc(row.failure_reason || "-")}</td>
          <td class="actions"><button class="secondary" data-email="${esc(row.email)}"><span data-icon="rotate-cw"></span>重登</button></td>
        </tr>`).join("") : `<tr><td colspan="7"><div class="empty-state">没有匹配的邮箱</div></td></tr>`;
      document.querySelectorAll("button[data-email]").forEach(btn => btn.onclick = () => startRelogin(btn.dataset.email).catch(showError));
      document.querySelectorAll(".rowCheck").forEach(box => {
        box.onchange = () => {
          if (box.checked) selectedEmails.add(box.dataset.email);
          else selectedEmails.delete(box.dataset.email);
          updateSelectionUi();
        };
      });
      renderIcons($("accountsBody"));
      updateSelectionUi();
      return data;
    }

    function updateSelectionUi() {
      $("selectedCount").textContent = `已选 ${selectedEmails.size} 项`;
      $("batchReloginBtn").disabled = selectedEmails.size === 0;
      const allVisibleSelected = visibleEmails.length > 0 && visibleEmails.every(email => selectedEmails.has(email));
      $("selectAllVisible").checked = allVisibleSelected;
    }

    function renderLogs(lines) {
      if (!lines || !lines.length) {
        $("jobLogs").innerHTML = `<div class="log-line"><span>等待任务输出</span></div>`;
        return;
      }
      $("jobLogs").innerHTML = lines.map(line => {
        const text = esc(line)
          .replace(/(失败|failed|error|ERROR)/gi, '<span class="log-bad">$1</span>')
          .replace(/(成功|完成|ok|OK|normal)/g, '<span class="log-ok">$1</span>')
          .replace(/(开始|进入|等待|INFO|running)/g, '<span class="log-info">$1</span>');
        return `<div class="log-line"><span>${text}</span></div>`;
      }).join("");
    }

    function renderJob(job) {
      if (job) latestJobSnapshot = job;
      const snapshot = latestJobSnapshot;
      if (!snapshot) {
        $("jobMeta").textContent = "暂无任务";
        $("jobDetail").textContent = "等待操作";
        $("jobBar").style.width = "0%";
        $("jobPercent").textContent = "0%";
        renderLogs([]);
        return;
      }
      $("jobMeta").textContent = `${snapshot.kind || "-"} ${snapshot.status || "-"} ${snapshot.done || 0}/${snapshot.total || 0} ok=${snapshot.ok || 0} failed=${snapshot.failed || 0}`;
      $("jobDetail").textContent = `target=${snapshot.target || "-"} started=${snapshot.started_at || "-"} finished=${snapshot.finished_at || "-"}`;
      $("jobBar").style.width = `${snapshot.percent || 0}%`;
      $("jobPercent").textContent = `${snapshot.percent || 0}%`;
      renderLogs(snapshot.logs || []);
      $("jobLogs").scrollTop = $("jobLogs").scrollHeight;
    }

    function showError(err) {
      const message = err?.message || String(err);
      renderLogs([message]);
      alert(message);
    }

    function setCurrentJob(jobId) {
      currentJob = jobId || "";
      try {
        if (currentJob) window.localStorage.setItem("mailReloginCurrentJob", currentJob);
        else window.localStorage.removeItem("mailReloginCurrentJob");
      } catch (_) {}
    }

    function scheduleJobPoll(delay=1500) {
      window.clearTimeout(pollTimer);
      if (currentJob) {
        pollTimer = window.setTimeout(() => pollJob(), delay);
      }
    }

    async function startRelogin(email) {
      const data = await api("/api/relogin/" + encodeURIComponent(email), {method:"POST"});
      setCurrentJob(data.job_id);
      renderJob({id:data.job_id, kind:"relogin", target:email, status:"running", total:1, done:0, ok:0, failed:0, percent:0, logs:[`${email} 重登任务已创建...`]});
      await pollJob();
    }

    async function startBatchRelogin() {
      const emails = Array.from(selectedEmails);
      if (!emails.length) return;
      const data = await api("/api/relogin-batch", {
        method:"POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({emails})
      });
      setCurrentJob(data.job_id);
      renderJob({id:data.job_id, kind:"relogin-batch", target:`${emails.length} emails`, status:"running", total:emails.length, done:0, ok:0, failed:0, percent:0, logs:[`批量重登任务已创建：${emails.length} 个邮箱...`]});
      await pollJob();
    }

    async function pollJob() {
      if (!currentJob) return;
      let job;
      try {
        job = await api("/api/jobs/" + currentJob);
      } catch (err) {
        renderLogs([`任务轮询失败，自动重试：${err?.message || err}`]);
        scheduleJobPoll(3000);
        return;
      }
      renderJob(job);
      if (job.status === "running") {
        scheduleJobPoll(1500);
      } else {
        setCurrentJob("");
        window.clearTimeout(pollTimer);
        await loadState({quiet:true});
      }
    }

    async function restoreLatestJob() {
      const data = await api("/api/jobs/latest");
      if (data.job) {
        renderJob(data.job);
        if (data.job.status === "running") {
          setCurrentJob(data.job.id);
          scheduleJobPoll(300);
        } else if (currentJob === data.job.id) {
          setCurrentJob("");
          window.clearTimeout(pollTimer);
        }
      } else {
        renderJob(null);
      }
    }

    async function reconcileLatestJob() {
      const data = await api("/api/jobs/latest");
      if (!data.job) return;
      const oldLogs = latestJobSnapshot?.logs || [];
      const newLogs = data.job.logs || [];
      const changed = !latestJobSnapshot
        || latestJobSnapshot.id !== data.job.id
        || latestJobSnapshot.status !== data.job.status
        || latestJobSnapshot.done !== data.job.done
        || oldLogs.length !== newLogs.length;
      if (!changed) return;
      renderJob(data.job);
      if (data.job.status === "running") {
        setCurrentJob(data.job.id);
        scheduleJobPoll(300);
      } else {
        if (currentJob === data.job.id) {
          setCurrentJob("");
          window.clearTimeout(pollTimer);
        }
        await loadState({quiet:true});
      }
    }

    function startLatestReconciler() {
      window.clearInterval(latestTimer);
      latestTimer = window.setInterval(() => {
        reconcileLatestJob().catch(() => {});
      }, 3000);
      window.addEventListener("focus", () => reconcileLatestJob().catch(() => {}));
      document.addEventListener("visibilitychange", () => {
        if (!document.hidden) reconcileLatestJob().catch(() => {});
      });
    }

    $("configForm").onsubmit = async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.currentTarget);
      await api("/api/config", {method:"POST", body:fd});
      ev.currentTarget.management_key.value = "";
      await loadState({quiet:true});
    };
    $("importForm").onsubmit = async (ev) => {
      ev.preventDefault();
      await api("/api/import-zip", {method:"POST", body:new FormData(ev.currentTarget)});
      ev.currentTarget.reset();
      $("uploadText").textContent = "选择 ZIP 文件或拖拽到此处";
      await loadState({quiet:true});
    };
    $("syncBtn").onclick = async () => {
      const data = await api("/api/sync", {method:"POST"});
      setCurrentJob(data.job_id);
      renderJob({id:data.job_id, kind:"sync", target:"all", status:"running", total:0, done:0, ok:0, failed:0, percent:0, logs:["全量更新任务已创建，等待日志..."]});
      pollJob();
    };
    $("refreshBtn").onclick = () => loadState({quiet:true}).catch(showError);
    $("selectVisibleBtn").onclick = () => {
      visibleEmails.forEach(email => selectedEmails.add(email));
      document.querySelectorAll(".rowCheck").forEach(box => box.checked = true);
      updateSelectionUi();
    };
    $("clearSelectionBtn").onclick = () => {
      selectedEmails.clear();
      document.querySelectorAll(".rowCheck").forEach(box => box.checked = false);
      updateSelectionUi();
    };
    $("selectAllVisible").onchange = (ev) => {
      if (ev.currentTarget.checked) visibleEmails.forEach(email => selectedEmails.add(email));
      else visibleEmails.forEach(email => selectedEmails.delete(email));
      document.querySelectorAll(".rowCheck").forEach(box => box.checked = ev.currentTarget.checked);
      updateSelectionUi();
    };
    $("batchReloginBtn").onclick = () => startBatchRelogin().catch(showError);
    let searchTimer = 0;
    $("search").oninput = () => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => loadState({quiet:true}).catch(showError), 220);
    };
    $("status").onchange = () => loadState({quiet:true}).catch(showError);
    $("zipFile").onchange = (ev) => {
      const file = ev.currentTarget.files && ev.currentTarget.files[0];
      $("uploadText").textContent = file ? file.name : "选择 ZIP 文件或拖拽到此处";
    };
    $("restoreLogBtn").onclick = () => restoreLatestJob().catch(showError);
    $("clearLogBtn").onclick = () => renderLogs([]);
    $("downloadLogBtn").onclick = () => {
      const text = Array.from($("jobLogs").querySelectorAll(".log-line span")).map(el => el.textContent).join("\\n");
      const blob = new Blob([text], {type:"text/plain;charset=utf-8"});
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `relogin-job-${currentJob || "logs"}.txt`;
      a.click();
      URL.revokeObjectURL(url);
    };
    renderIcons();
    renderLogs([]);
    loadState().then(() => {
      try { currentJob = window.localStorage.getItem("mailReloginCurrentJob") || ""; } catch (_) {}
      startLatestReconciler();
      return restoreLatestJob();
    }).catch(err => { renderLogs([err.message]); });
  </script>
</body>
</html>
"""


@app.get("/")
def index():
    if FRONTEND_TEMPLATE_PATH.exists():
        return render_template_string(FRONTEND_TEMPLATE_PATH.read_text(encoding="utf-8"))
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


@app.get("/api/jobs/latest")
def api_latest_job():
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    if not jobs:
        return jsonify({"job": None})
    latest = max(jobs, key=lambda item: item.started_at)
    return jsonify({"job": latest.to_dict()})


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
