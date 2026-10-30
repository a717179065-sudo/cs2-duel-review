"""
放 demo 的窗口：本地 http 服务，只监听 127.0.0.1:8770（只用标准库）。

用法: py -I app/server.py [--port 8770]
  GET  /                 窗口页 app/index.html
  GET  /report/<文件>     report 目录里的复盘页（不许 ../）
  POST /api/upload       请求体就是 demo 文件，头 X-Filename（encodeURIComponent 过的文件名）、X-User、X-Last-Modified（毫秒）
  POST /api/local        {"path": 本机 demo 路径, "user": steamid}
  POST /api/rerun        {"job": 任务号, "user": steamid}：同一个 demo 换一个人重新排队（用户不在 demo 里时点名单用）
  GET  /api/config       {"default_user", "stages"}
  GET  /api/jobs         任务列表（新的在前）
  GET  /api/jobs/<id>    一个任务：10 步的状态、耗时、说明，完成后有复盘页地址，失败有原因
  GET  /api/reports      已生成的复盘页（读 work/<key>/meta.json）；?user=<steamid> 只列这个人的
一个后台线程按顺序跑任务：子进程 py -I run.py <demo> --json-progress --user <steamid>，逐行读进度。
任务列表存 work/_jobs.json，重启后历史还在；子进程的报错输出存 work/_logs/<任务>.log。
测试用：环境变量 REVIEW_RUNNER=<脚本路径> 换掉 run.py，REVIEW_WORK 换 work 目录，REVIEW_PORT 换端口。
"""
import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
REPORT = ROOT / "report"
WORK = Path(os.environ.get("REVIEW_WORK") or ROOT / "work")   # 测试时可以换到 scratch
UPLOADS = WORK / "_uploads"
LOGS = WORK / "_logs"
JOBS_F = WORK / "_jobs.json"
RUNNER = Path(os.environ.get("REVIEW_RUNNER") or ROOT / "run.py")
sys.path.insert(0, str(ROOT))           # py -I 不带脚本目录；默认用户从 engine/config.py 取，不在这里再写一份
try:
    from engine.config import DEFAULT_USER
except Exception:                       # noqa: BLE001  引擎坏了窗口也要能开（任务会在 run.py 里报错）
    DEFAULT_USER = ""
EXTS = (".dem", ".dem.gz", ".dem.zst", ".dem.bz2")
MAX_UPLOAD = 2 * 1024 ** 3          # 2 GB
CHUNK = 1 << 20
# 和 engine/pipeline.py 的 STAGES 一致；run.py 开头会再发一次，以那个为准
STAGES = [("base", "读取比赛数据"), ("extract", "解析 demo"), ("cache", "准备帧数据"), ("economy", "经济"), ("shooting", "开枪"),
          ("rules", "跑规则"), ("compose", "写复盘"), ("validate", "检查文字"), ("clips", "切 2D 回放"), ("build", "生成页面")]
MIME = {".html": "text/html; charset=utf-8", ".json": "application/json; charset=utf-8", ".csv": "text/csv; charset=utf-8",
        ".txt": "text/plain; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8"}

LOCK = threading.Lock()
JOBS = {}                            # id -> job dict
QUEUE = queue.Queue()


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def allowed(name):
    return name.lower().endswith(EXTS)


def safe_name(raw):
    """上传文件名 → 只留字母数字汉字和 . _ -，去掉目录部分"""
    name = unicodedata.normalize("NFC", raw).replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^\w.\-]", "_", name).lstrip(".")[-150:]
    return name or "demo.dem"


# ---------- 任务 ----------

def save_jobs():
    """调用方持有 LOCK；先写临时文件再替换，避免写一半"""
    WORK.mkdir(parents=True, exist_ok=True)
    tmp = JOBS_F.with_name(JOBS_F.name + ".tmp")
    data = [JOBS[k] for k in sorted(JOBS)]
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, JOBS_F)


def load_jobs():
    """启动时读历史；上次跑到一半的标成中断，排队的重新排队"""
    try:
        data = json.loads(JOBS_F.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = []
    requeue = []
    with LOCK:
        for j in data:
            JOBS[j["id"]] = j
            if j["status"] == "running":
                j["status"] = "failed"
                j["error"] = "窗口服务重启，这个任务被中断了，可以重新放一次 demo"
                for s in j["stages"]:
                    if s["status"] == "running":
                        s["status"] = "failed"
                        s.pop("t0", None)
                j["finished"] = now_str()
            elif j["status"] == "queued":
                requeue.append(j["id"])
        if data:
            save_jobs()
    for jid in requeue:
        QUEUE.put(jid)


def new_job(dem, name, user, source):
    with LOCK:
        n = max([int(k) for k in JOBS if k.isdigit()] or [0]) + 1
        jid = f"{n:04d}"
        JOBS[jid] = {"id": jid, "name": name, "dem": str(dem), "user": user, "source": source, "status": "queued",
                     "created": now_str(), "started": None, "finished": None, "demo_id": None,
                     "stages": [{"key": k, "label": l, "status": "wait", "message": "", "sec": None} for k, l in STAGES],
                     "report_url": None, "error": None}
        save_jobs()
    QUEUE.put(jid)
    return jid


def report_url(path):
    """report 目录里的文件 → /report/<文件名>"""
    if not path:
        return None
    p = Path(path).resolve()
    if p.parent == REPORT.resolve() and p.is_file():
        return "/report/" + urllib.parse.quote(p.name)
    return None


def apply_event(job, ev):
    """把 run.py 的一行进度写进任务"""
    typ = ev.get("type")
    if typ == "info" and ev.get("stages"):
        old = {s["key"]: s for s in job["stages"]}
        job["stages"] = [old.get(k) or {"key": k, "label": l, "status": "wait", "message": "", "sec": None} for k, l in ev["stages"]]
        job["demo_id"] = ev.get("demo_id") or ev.get("id")
        job["key"] = ev.get("id")
    elif typ == "stage":
        st = next((s for s in job["stages"] if s["key"] == ev.get("key")), None)
        if st is None:
            return
        status = {"start": "running", "done": "done", "skip": "skip", "fail": "failed"}.get(ev.get("status"), "running")
        st["status"] = status
        st["message"] = ev.get("message") or ""
        if status == "running":
            st["t0"] = time.time()
            st["sec"] = None
        else:
            st["sec"] = ev.get("sec") if ev.get("sec") is not None else (round(time.time() - st["t0"], 1) if st.get("t0") else 0.0)
            st.pop("t0", None)
    elif typ == "done":
        job["report_url"] = report_url(ev.get("report"))
        job["report_path"] = ev.get("report")
    elif typ == "error":
        job["error"] = ev.get("message") or "出错了"


def run_job(jid):
    with LOCK:
        job = JOBS[jid]
        job["status"] = "running"
        job["started"] = now_str()
        job["_t0"] = time.time()
        save_jobs()
        cmd = [sys.executable, "-I", str(RUNNER), job["dem"], "--json-progress", "--user", job["user"]]
    LOGS.mkdir(parents=True, exist_ok=True)
    log_f = LOGS / f"{jid}.log"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    done = False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # Windows 下不弹黑窗口
    try:
        with open(log_f, "w", encoding="utf-8") as err:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=err, env=env,
                                    encoding="utf-8", errors="replace", creationflags=flags)
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    err.write(line + "\n")
                    continue
                with LOCK:
                    apply_event(job, ev)
                    done = done or ev.get("type") == "done"
                    save_jobs()
            code = proc.wait()
    except OSError as e:
        code, done = -1, False
        job["error"] = f"启动不了复盘程序：{e}"
    with LOCK:
        job.pop("_t0", None)
        job["finished"] = now_str()
        if done and code == 0:
            job["status"] = "done"
        else:
            job["status"] = "failed"
            if not job.get("error"):
                tail = log_tail(log_f)
                job["error"] = f"复盘程序异常退出（代码 {code}）" + (f"：{tail}" if tail else "")
            for s in job["stages"]:
                if s["status"] == "running":
                    s["status"] = "failed"
                    s.pop("t0", None)
        save_jobs()


def log_tail(f, n=3):
    try:
        lines = [x for x in f.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
    except OSError:
        return ""
    return " / ".join(lines[-n:])[-400:]


def worker():
    while True:
        jid = QUEUE.get()
        try:
            run_job(jid)
        except Exception as e:             # noqa: BLE001  一个任务出错不能把线程弄死
            with LOCK:
                JOBS[jid]["status"] = "failed"
                JOBS[jid]["error"] = f"窗口服务内部错误：{type(e).__name__}: {e}"
                save_jobs()


def player_of(job):
    """任务复盘的是谁：解析出 timeline.json 以后取里面的玩家名（记在任务上），之前返回 None"""
    if job.get("player"):
        return job["player"]
    key = job.get("key")
    if not key:
        return None
    tl = WORK / str(key) / "timeline.json"
    try:
        m = json.loads(tl.read_text(encoding="utf-8"))["meta"]
    except (OSError, ValueError, KeyError):
        return None
    if str(m.get("user_steamid")) != str(job.get("user")):
        return None                                   # 上一次跑剩下的、不是这个人的
    if job.get("status") in ("done", "failed"):
        job["player"] = m.get("user")                 # 跑完了不会再变，记下来
    return m.get("user")


def job_view(job, full=True):
    """给页面看的任务；正在跑的那一步现算已用时间"""
    j = {k: v for k, v in job.items() if not k.startswith("_") and k != "dem"}
    j["player"] = player_of(job)
    j["dem_name"] = Path(job["dem"]).name
    j["stages"] = [dict(s) for s in job["stages"]]
    for s in j["stages"]:
        t0 = s.pop("t0", None)
        if s["status"] == "running" and t0:
            s["sec"] = round(time.time() - t0, 1)     # 正在跑：到现在为止的秒数
    if job.get("_t0"):
        j["elapsed"] = round(time.time() - job["_t0"], 1)
    done_n = sum(1 for s in job["stages"] if s["status"] in ("done", "skip"))
    j["progress"] = [done_n, len(job["stages"])]
    if not full:
        j.pop("stages")
    return j


# ---------- 复盘页列表 ----------

def list_reports(user=None):
    """report/<key>.html 且 work/<key>/meta.json 在的；user 给了就只列这个 steamid 的"""
    out = []
    for f in sorted(REPORT.glob("*.html")):
        mf = WORK / f.stem / "meta.json"
        if not mf.is_file():
            continue
        try:
            meta = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if user and str(meta.get("user_steamid")) != user:
            continue
        meta["url"] = "/report/" + urllib.parse.quote(f.name)
        meta["built"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime))
        meta["_m"] = f.stat().st_mtime
        out.append(meta)
    out.sort(key=lambda r: (-r["_m"], str(r.get("id"))))
    for r in out:
        r.pop("_m")
    return out


# ---------- http ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "cs2-review/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):     # 只记出错的请求，正常请求不刷屏
        if not fmt.startswith('"%s"') or (len(args) > 1 and str(args[1]).startswith(("4", "5"))):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, code, msg):
        self.send_json({"error": msg}, code)

    def host_ok(self):
        """只认 127.0.0.1 / localhost 的 Host，防 DNS 重绑定"""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def send_file(self, f, ctype):
        size = f.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with open(f, "rb") as fh:
            for b in iter(lambda: fh.read(CHUNK), b""):
                self.wfile.write(b)

    # ----- GET -----
    def do_GET(self):
        if not self.host_ok():
            return self.fail(403, "只接受本机访问")
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            return self.send_file(APP / "index.html", MIME[".html"])
        if path.startswith("/report/"):
            return self.get_report(path[len("/report/"):])
        if path == "/api/jobs":
            with LOCK:
                jobs = [job_view(JOBS[k], full=False) for k in sorted(JOBS, reverse=True)]
            return self.send_json({"jobs": jobs})
        m = re.fullmatch(r"/api/jobs/(\d{1,8})", path)
        if m:
            with LOCK:
                job = JOBS.get(m.group(1))
                view = job_view(job) if job else None
            return self.send_json(view) if view else self.fail(404, "没有这个任务")
        if path == "/api/reports":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            u = (q.get("user") or [""])[0].strip()
            return self.send_json({"reports": list_reports(u if re.fullmatch(r"\d{17}", u) else None)})
        if path == "/api/config":
            return self.send_json({"default_user": DEFAULT_USER, "stages": [list(s) for s in STAGES]})
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self.fail(404, "没有这个地址")

    def get_report(self, raw):
        name = urllib.parse.unquote(raw)
        # 只许 report 目录下一层的普通文件
        if not name or "/" in name or "\\" in name or ".." in name or name.startswith(".") or ":" in name:
            return self.fail(403, "不允许的路径")
        f = (REPORT / name).resolve()
        if f.parent != REPORT.resolve() or not f.is_file():
            return self.fail(404, "没有这个文件")
        return self.send_file(f, MIME.get(f.suffix.lower(), "application/octet-stream"))

    # ----- POST -----
    def do_POST(self):
        if not self.host_ok():
            return self.fail(403, "只接受本机访问")
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/upload":
            return self.post_upload()
        if path == "/api/local":
            return self.post_local()
        if path == "/api/rerun":
            return self.post_rerun()
        return self.fail(404, "没有这个地址")

    def user_of(self, v):
        v = (v or DEFAULT_USER).strip()
        return v if re.fullmatch(r"\d{17}", v) else None

    def post_upload(self):
        self.close_connection = True       # 出错时请求体可能没读完，这个连接不再复用
        raw_name = self.headers.get("X-Filename")
        if not raw_name:
            return self.fail(400, "缺少文件名")
        name = safe_name(urllib.parse.unquote(raw_name))
        if not allowed(name):
            self.close_connection = True
            return self.fail(400, f"只收 {' / '.join(EXTS)} 文件：{name}")
        user = self.user_of(self.headers.get("X-User"))
        if not user:
            self.close_connection = True
            return self.fail(400, "steamid 应该是 17 位数字")
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked" or self.headers.get("Content-Length") is None:
            self.close_connection = True
            return self.fail(411, "需要 Content-Length")
        try:
            size = int(self.headers["Content-Length"])
        except ValueError:
            self.close_connection = True
            return self.fail(400, "Content-Length 不对")
        if size <= 0:
            return self.fail(400, "文件是空的")
        if size > MAX_UPLOAD:
            self.close_connection = True
            return self.fail(413, "文件超过 2 GB")
        UPLOADS.mkdir(parents=True, exist_ok=True)
        part = UPLOADS / f"{name}.{threading.get_ident()}.part"
        got = 0
        try:
            with open(part, "wb") as out:
                while got < size:
                    b = self.rfile.read(min(CHUNK, size - got))
                    if not b:
                        break
                    out.write(b)
                    got += len(b)
        except OSError as e:
            part.unlink(missing_ok=True)
            self.close_connection = True
            return self.fail(500, f"保存失败：{e}")
        if got != size:
            part.unlink(missing_ok=True)
            self.close_connection = True
            return self.fail(400, "上传中断了")
        # 保留浏览器给的原文件修改时间（历史列表里的日期用得上）
        try:
            lm = int(self.headers.get("X-Last-Modified") or 0) / 1000
            if lm > 0:
                os.utime(part, (lm, lm))
        except (ValueError, OSError):
            pass
        dst = UPLOADS / name
        try:
            os.replace(part, dst)
        except OSError:                    # 同名文件正在被用：换个名字
            stem, ext = name.split(".", 1)
            i = 2
            while (UPLOADS / f"{stem}_{i}.{ext}").exists():
                i += 1
            dst = UPLOADS / f"{stem}_{i}.{ext}"
            os.replace(part, dst)
        jid = new_job(dst, name, user, "upload")
        return self.send_json({"job": jid, "file": dst.name, "size": got})

    def read_json_body(self):
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return None
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if 0 < n < 65536 else {}
        except (ValueError, UnicodeDecodeError):
            return None

    def post_rerun(self):
        """同一个 demo 换一个 steamid 再排一次（只认窗口里已有的任务，不接受任意路径）"""
        body = self.read_json_body()
        if body is None:
            return self.fail(400, "请求格式不对")
        user = self.user_of(body.get("user"))
        if not user:
            return self.fail(400, "steamid 应该是 17 位数字")
        with LOCK:
            job = JOBS.get(str(body.get("job") or ""))
            dem, name, source = (job["dem"], job["name"], job["source"]) if job else (None, None, None)
        if not job:
            return self.fail(404, "没有这个任务")
        if not Path(dem).is_file():
            return self.fail(404, "这个任务的 demo 文件已经不在了，重新放一次")
        jid = new_job(Path(dem), name, user, source)
        return self.send_json({"job": jid, "file": name})

    def post_local(self):
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self.fail(415, "需要 JSON")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8")) if 0 < n < 65536 else {}
        except (ValueError, UnicodeDecodeError):
            return self.fail(400, "请求格式不对")
        raw = str(body.get("path") or "").strip().strip('"').strip("'")
        if not raw:
            return self.fail(400, "路径是空的")
        user = self.user_of(body.get("user"))
        if not user:
            return self.fail(400, "steamid 应该是 17 位数字")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            return self.fail(400, "请填完整路径，比如 D:\\demos\\xxx.dem")
        if not allowed(p.name):
            return self.fail(400, f"只收 {' / '.join(EXTS)} 文件：{p.name}")
        try:
            ok = p.is_file()
        except OSError:
            ok = False
        if not ok:
            return self.fail(404, f"找不到这个文件：{p}")
        jid = new_job(p.resolve(), p.name, user, "local")
        return self.send_json({"job": jid, "file": p.name})


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("REVIEW_PORT") or 8770))
    a = ap.parse_args()
    load_jobs()
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.daemon_threads = True
    print(f"复盘窗口：http://127.0.0.1:{a.port}/  （关掉这个窗口就停止）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
