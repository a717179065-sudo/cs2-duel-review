# -*- coding: utf-8 -*-
"""事实层：从 demo 算出底稿、时间线、经济、开枪、逐帧缓存（由 pro/analysis/match3 的提取脚本参数化而来，输出格式不变）。

阶段顺序（后面的依赖前面的产出）：
  base     → base/*.csv（facts/base.py：这一场的回合、位置、击杀、道具、交火、经济局面、用户交火，只从本 demo 算）
  extract  → timeline.json、match_overview.json/.csv
  cache    → cache/*.pkl 等（部分缓存的取样 tick 来自 timeline.json 的 key_ticks）
  economy  → economy.json
  shooting → shooting.json

这一场自己的数据一律读 work/<key>/base/（原来要靠批量脚本事先入库的 pro/data/rounds_<id>.csv、duels_<id>.csv、
report/duels_de_mirage_<用户名>.csv、pro/analysis/sit_rounds.csv 里这一场的行）。
其他比赛的对标数据（bench/：groups.csv、duels_*、positions_*、sit_rounds.csv 的 high/pro 行）仍是只读的静态参考，用的时候排除本场。
"""
import json
from pathlib import Path

import pandas as pd

from .. import config


class FactsError(RuntimeError):
    """事实层的可读错误（中文，直接给用户看）。"""


def base_file(ctx, name):
    """work/<key>/base/<name>；没有就报错（底稿阶段没跑）。"""
    f = Path(ctx.work) / 'base' / name
    if not f.exists():
        raise FactsError(f'缺这一场的底稿 base/{name}：先跑"读取比赛数据"这一步（run.py 会按顺序跑）')
    return f


def rounds_csv(ctx):
    """这一场的回合表 work/<key>/base/rounds.csv（格式同 pro/data/rounds_<id>.csv）。"""
    return base_file(ctx, 'rounds.csv')


def positions_refs(ctx):
    """落点报点用的参考位置文件：其他场的静态 bench/data/positions_*.csv(.gz)（去掉本场那份）+ 本场 base/positions.csv，
    按 positions_<id>.csv 的文件名排序（和原来全读对标库时的顺序一样，众数并列时结果不变）。
    对标库里的位置表只留了 X/Y/Z/place 四列并 gzip 压缩（pandas 按后缀自动解压），同名的 .csv 优先。"""
    own = f'positions_{ctx.id}.csv'
    files = {}
    for f in sorted(config.DATA.glob('positions_*.csv')) + sorted(config.DATA.glob('positions_*.csv.gz')):
        key = f.name[:-3] if f.name.endswith('.gz') else f.name
        if key != own:
            files.setdefault(key, f)
    files[own] = base_file(ctx, 'positions.csv')
    return [files[k] for k in sorted(files)]


def user_duels(ctx, **read_kw):
    """用户本场的交火表 work/<key>/base/user_duels.csv（build_report.py 同格式，"场次"列固定 1，不用）。返回 df。
    用户在 demo 里但一次交火都没有时是空表。"""
    return pd.read_csv(base_file(ctx, 'user_duels.csv'), **read_kw)


def match_no(ctx, du=None):
    """旧的多场报告里这一场是"第几场"：只有默认用户的那 4 场有（config.LEGACY_MATCH_NO，静态表，不再读
    report/duels_de_mirage_*.csv）。其他 demo、别的用户返回 None——页面就写 demo id，不写"第 N 场"。"""
    if str(ctx.user) != config.DEFAULT_USER:
        return None
    return config.LEGACY_MATCH_NO.get(ctx.id)


def sit_rows(ctx):
    """这一场每回合每方的买法/局势 work/<key>/base/sit.csv（sit_rounds.csv 同格式）。"""
    return pd.read_csv(base_file(ctx, 'sit.csv'), dtype={'demo': str})


def other_reviews(ctx):
    """同一个用户（steamid）的其他 Mirage 场：[(demo id, 静态交火表 bench/data/duels_<id>.csv)]，按 demo id 排序。
    只查静态库（groups.csv 所有组里 duels 表有这个 steamid 的场，去掉本场），不扫 work/——否则同一个 demo 的
    shooting.json 会随“之前在这里复盘过哪些场”变化（GENERALITY.md U1）。"""
    gp = config.DATA / 'groups.csv'
    if not gp.exists():
        return []
    sid = str(ctx.user)
    out = []
    for d in sorted({str(x) for x in pd.read_csv(gp, dtype={'demo': str})['demo']}):
        f = config.DATA / f'duels_{d}.csv'
        if d == ctx.id or not f.exists():
            continue
        try:
            s = pd.read_csv(f, usecols=['steamid'], dtype={'steamid': str})['steamid']
        except (OSError, ValueError):
            continue
        if (s == sid).any():
            out.append((d, f))
    return out
