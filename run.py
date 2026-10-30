"""
复盘引擎命令行入口：一个 Mirage demo → report/<key>.html（任何 Mirage demo 都行，不用先入库）。

用法: py -I run.py <demo路径> [--user STEAMID] [--force] [--json-progress]
  demo 可以是 .dem，也可以是 .dem.gz / .dem.zst / .dem.bz2（engine.ctx.make_ctx 解压成 work/<id>/match.dem）
  --user           要复盘的玩家 steamid，默认 engine/config.py 的 DEFAULT_USER
  --force          不用缓存，10 步全部重跑
  --json-progress  每行输出一个 JSON（给 app/server.py 读进度用）
<id>：engine.config.demo_id（FACEIT 文件名里的 8 位 hex，否则文件 sha1 前 8 位）。
<key>：默认用户就是 <id>；别的用户 <id>_<steamid>（同一个 demo 换人复盘不会覆盖）。
退出码：0 成功，1 某一步失败，2 参数或文件不对（含：用户不在 demo 里、不是 Mirage）。
"""
import argparse
import contextlib
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))            # py -I 不会把脚本目录放进 sys.path，engine 包要手动加
EXTS = (".dem", ".dem.gz", ".dem.zst", ".dem.bz2")


class UsageError(Exception):
    pass


def main():
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="CS2 Mirage demo 复盘")
    ap.add_argument("demo")
    ap.add_argument("--user", default=None, help="steamid，默认 engine/config.py 的 DEFAULT_USER")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--json-progress", action="store_true")
    a = ap.parse_args()
    js = a.json_progress
    out = sys.stdout                      # 进度只写这里；--json-progress 时各步骤自己的打印改到 stderr，stdout 只有 JSON

    def emit(obj, text):
        print(json.dumps(obj, ensure_ascii=False) if js else text, file=out, flush=True)

    t_start = {}

    def progress(key, label, i, n, status, message):
        sec = None
        if status == "start":
            t_start[key] = time.perf_counter()
        elif key in t_start:
            sec = round(time.perf_counter() - t_start[key], 1)
        word = {"start": "开始", "done": "完成", "skip": "沿用缓存", "fail": "失败"}[status]
        tail = (f"（{sec} 秒）" if sec is not None and status != "start" else "") + (f" {message}" if message else "")
        emit({"type": "stage", "key": key, "label": label, "i": i, "n": n, "status": status, "message": message, "sec": sec},
             f"[{i}/{n}] {label} …… {word}{tail}")

    try:
        src = Path(a.demo).expanduser().resolve()
        if not src.is_file():
            raise UsageError(f"找不到 demo 文件：{src}")
        if not src.name.lower().endswith(EXTS):
            raise UsageError(f"不是 demo 文件（支持 {' '.join(EXTS)}）：{src.name}")
        if a.user is None:
            from engine.config import DEFAULT_USER
            a.user = DEFAULT_USER
        if not re.fullmatch(r"\d{17}", a.user):
            raise UsageError(f"steamid 应该是 17 位数字：{a.user}")
        from engine import pipeline
        from engine.ctx import make_ctx
        if not src.name.lower().endswith(".dem"):
            emit({"type": "info", "message": "解压 demo"}, "解压 demo ……")
        with contextlib.redirect_stdout(sys.stderr if js else out):
            ctx = make_ctx(src, user=a.user, force=a.force)      # 算 id/key、建 work/<key>/、压缩包解压成 work/<id>/match.dem
        dem, rid = Path(ctx.dem), ctx.key
    except UsageError as e:
        emit({"type": "error", "stage": None, "message": str(e)}, f"出错：{e}")
        return 2
    except Exception as e:                 # noqa: BLE001  引擎还没装好、解压失败等
        emit({"type": "error", "stage": None, "message": f"准备失败：{type(e).__name__}: {e}"}, f"准备失败：{type(e).__name__}: {e}")
        return 2

    emit({"type": "info", "id": rid, "demo_id": ctx.id, "demo": str(dem), "user": a.user, "stages": [list(s) for s in pipeline.STAGES]},
         f"demo {dem.name}  id {ctx.id}  用户 {a.user}  输出 {rid}")
    try:
        with contextlib.redirect_stdout(sys.stderr if js else out):
            res = pipeline.run(ctx, progress)
    except pipeline.PipelineError as e:
        emit({"type": "error", "stage": e.stage, "message": e.message}, f"停在这一步：{e.message}")
        return 2 if getattr(e, "usage", False) else 1      # 用户不在 demo 里、不是 Mirage：输入的问题
    emit({"type": "done", "id": rid, "report": res["report"]}, f"复盘页：{res['report']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
