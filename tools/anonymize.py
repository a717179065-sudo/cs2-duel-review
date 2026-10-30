"""
把一场复盘的产出匿名化，生成可以公开的示例页：只保留被复盘的玩家本人，其他人一律换成代号。

用法: py -I tools/anonymize.py <key> [--out examples] [--work work]
  <key>   work/<key>/ 的目录名（默认用户就是 demo id，例如 8d9b0b8f）
产出:
  <out>/<key>.html          用匿名化后的数据重新拼的复盘页（engine.build.build，和正常流程同一个模板）
  <out>/<key>.review.json   匿名化后的 review.json

做法（确定性，同一场每次结果一样）:
  1. 名单来自这一场的底稿 work/<key>/base/positions.csv：每个 steamid 用过的所有名字、所在阵营。
  2. 和被复盘的人同阵营的按 steamid 排序叫 队友A、队友B……；另一边按 steamid 排序叫 对手1、对手2……
     steamid 换成假的 17 位数字（9000000000000001x 队友、9000000000000002x 对手），被复盘的人保持原样。
  3. 只改数据，不改模板：逐个遍历 review / timeline / match_overview / clips 里的字符串，
     名字按整词替换（前后不是字母数字），等于 steamid 的字符串 / 整数换成假 id；个人主页、头像类链接清空。
  4. 拼好页面后再整页扫一遍：还能找到任何原名或原 steamid 就报错，不写出文件。
"""
import argparse
import csv
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

URL = re.compile(r'https?://(?:[\w-]+\.)*(?:steamcommunity\.com|steamstatic\.com|faceit\.com|faceit-cdn\.net)\S*', re.I)
INPUTS = ['review.json', 'timeline.json', 'match_overview.json', 'clips.json']


def roster(work):
    """{steamid: {'names': set, 'side': 首次出现时的阵营, 'round': 那一回合}}，按 base/positions.csv"""
    out = {}
    with open(work / 'base' / 'positions.csv', encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            sid = str(r['steamid']).split('.')[0]
            p = out.setdefault(sid, {'names': set(), 'side': r['side'], 'round': r['round']})
            if r['name']:
                p['names'].add(r['name'])
    return out


def side_in_round(work, sid, rnd):
    with open(work / 'base' / 'positions.csv', encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            if r['round'] == rnd and str(r['steamid']).split('.')[0] == sid:
                return r['side']
    return None


def build_mapping(work, user):
    ros = roster(work)
    if user not in ros:
        raise SystemExit(f'{user} 不在这一场的名单里')
    mates, opps = [], []
    for sid, p in ros.items():
        if sid == user:
            continue
        us = side_in_round(work, user, p['round'])           # 同一回合比较阵营，换边、中途进来的人都不会分错
        (mates if p['side'] == us else opps).append(sid)
    label, fake = {}, {}
    for i, sid in enumerate(sorted(mates)):
        label[sid], fake[sid] = f'队友{chr(65 + i)}', f'9000000000000001{i}'
    for i, sid in enumerate(sorted(opps)):
        label[sid], fake[sid] = f'对手{i + 1}', f'9000000000000002{i}'
    names = {}
    for sid, lab in label.items():
        for n in ros[sid]['names']:
            names[n] = lab
    keep = ros[user]['names']
    names = {n: lab for n, lab in names.items() if n not in keep}
    return names, fake, keep


class Anon:
    def __init__(self, names, fake):
        self.names, self.fake = names, fake
        alts = sorted(names, key=len, reverse=True)
        self.rx = re.compile(r'(?<![A-Za-z0-9])(' + '|'.join(map(re.escape, alts)) + r')(?![A-Za-z0-9])') if alts else None
        self.fake_int = {int(k): int(v) for k, v in fake.items()}

    def text(self, s):
        if s in self.fake:
            return self.fake[s]
        if s in self.names:
            return self.names[s]
        s = URL.sub('', s)
        if self.rx:
            s = self.rx.sub(lambda m: self.names[m.group(1)], s)
        for k, v in self.fake.items():
            if k in s:
                s = s.replace(k, v)
        return s

    def walk(self, o):
        if isinstance(o, dict):
            return {(self.text(k) if isinstance(k, str) else k): self.walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [self.walk(v) for v in o]
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, int) and not isinstance(o, bool) and o in self.fake_int:
            return self.fake_int[o]
        return o


LOCAL = re.compile(r'(?<![A-Za-z])[A-Za-z]:(?:\\|/(?!/))'                    # 带盘符的本机绝对路径
                   r'|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)   # 完整的比赛 uuid


def leaks(text, names, fake):
    """还剩下的原名、原 steamid、本机绝对路径、完整的比赛 id"""
    found = [s for s in fake if s in text] + [m.group(0) for m in LOCAL.finditer(text)]
    for n in names:
        if re.search(r'(?<![A-Za-z0-9])' + re.escape(n) + r'(?![A-Za-z0-9])', text):
            found.append(n)
    return found


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description='匿名化一场复盘的产出')
    ap.add_argument('key')
    ap.add_argument('--work', default=str(ROOT / 'work'))
    ap.add_argument('--out', default=str(ROOT / 'examples'))
    a = ap.parse_args()
    work = Path(a.work) / a.key
    tl = json.loads((work / 'timeline.json').read_text(encoding='utf-8'))
    user = str(tl['meta'].get('user_steamid') or a.key.split('_')[-1])
    names, fake, keep = build_mapping(work, user)
    an = Anon(names, fake)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tw = Path(tmp) / a.key
        tw.mkdir()
        for f in INPUTS:
            src = work / f
            if src.exists():
                d = an.walk(json.loads(src.read_text(encoding='utf-8')))
                if f == 'timeline.json':
                    d['meta'].pop('dem_path', None)          # 本机 demo 路径（文件名里是完整的比赛 id），页面不用
                (tw / f).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
        from engine.build import build
        html = Path(tmp) / f'{a.key}.html'
        build(tw, html, a.key)                      # 不传 demo 路径：页面里不带文件日期
        page = html.read_text(encoding='utf-8')
        rv = (tw / 'review.json').read_text(encoding='utf-8')
        bad = leaks(page, names, fake) + leaks(rv, names, fake)
        if bad:
            raise SystemExit(f'匿名化后还能找到 {len(bad)} 处原名 / steamid / 本机路径，没有写出文件')
        shutil.copyfile(html, out / f'{a.key}.html')
        shutil.copyfile(tw / 'review.json', out / f'{a.key}.review.json')
    print(f'{a.key}: 保留 {"/".join(sorted(keep))}，替换 {len(fake)} 名玩家（{len(names)} 个名字）'
          f' → {out / (a.key + ".html")}（{(out / (a.key + ".html")).stat().st_size // 1024} KB）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
