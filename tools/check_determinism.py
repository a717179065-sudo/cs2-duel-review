"""
确定性检查：同一个 demo 用 --force 连跑两次，比较两次产出的 sha1。

用法: py -I tools/check_determinism.py <demo路径> [--user STEAMID]
  逐个比较 work/<key>/ 下的 timeline / match_overview / economy / shooting / hits / review / clips.json
  和 report/<key>.html。全部相同退出码 0，有不同的退出码 1（列出不同的文件）。
  每次要跑完整的 10 步，一场 23 回合的比赛大约 2–3 分钟一次。
"""
import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FILES = ['timeline.json', 'match_overview.json', 'economy.json', 'shooting.json', 'hits.json', 'review.json', 'clips.json']


def digest(key):
    from engine import config
    out = {}
    for f in FILES:
        p = config.WORK / key / f
        out[f] = hashlib.sha1(p.read_bytes()).hexdigest() if p.is_file() else None
    p = config.REPORT / f'{key}.html'
    out['report.html'] = hashlib.sha1(p.read_bytes()).hexdigest() if p.is_file() else None
    return out


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('demo')
    ap.add_argument('--user')
    a = ap.parse_args()
    from engine import config
    user = a.user or config.DEFAULT_USER
    key = config.output_key(config.demo_id(a.demo), user)
    cmd = [sys.executable, '-I', '-B', str(ROOT / 'run.py'), a.demo, '--user', user, '--force']
    runs = []
    for i in (1, 2):
        print(f'第 {i} 次运行 ……', flush=True)
        r = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        if r.returncode != 0:
            print(r.stderr[-2000:])
            raise SystemExit(f'第 {i} 次运行失败（退出码 {r.returncode}）')
        runs.append(digest(key))
    bad = [f for f in runs[0] if runs[0][f] != runs[1][f] or runs[0][f] is None]
    for f, h in runs[0].items():
        print(f"{'相同' if f not in bad else '不同'}  {f}  {h}")
    if bad:
        raise SystemExit(1)
    print(f'{key}：两次运行的产出逐字节相同')


if __name__ == '__main__':
    main()
