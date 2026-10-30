# -*- coding: utf-8 -*-
"""规则层：7 个模块按固定顺序跑，合并成一个 hit 列表写进 work/<id>/hits.json。

ALL 的顺序就是 rules_v1 的类别顺序；每个模块提供 run(ctx) -> list[Hit]（字段见 CONTRACT.md）。
run_all(ctx)：全部跑完 → 清成纯 Python 值 → 按 (回合, tick, 秒, 规则, 主角, 句子) 排序 → 写 hits.json。
任何一个模块出错就整步失败（不吞异常，pipeline 会转成中文报错）。
"""
import json
import math
from pathlib import Path

from . import shot, pos, team, util, timing, post, eco

ALL = [shot, pos, team, util, timing, post, eco]

# Hit 字段顺序（CONTRACT）；输出时按这个顺序排键
HIT_KEYS = ['rule', 'kind', 'round', 't', 'tick', 'scope', 'subject', 'is_user', 'blame', 'death_t', 'death_of',
            'merge_key', 'facts', 'text', 'alt', 'kb', 'evidence']


def plain(o):
    """numpy / pandas 标量、tuple、set → JSON 能写的纯 Python 值；NaN → None；dict 键转成字符串"""
    if isinstance(o, dict):
        return {str(k): plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [plain(v) for v in o]
    if isinstance(o, (set, frozenset)):
        return [plain(v) for v in sorted(o, key=str)]
    if isinstance(o, bool) or o is None or isinstance(o, str):
        return o
    if isinstance(o, int):
        return int(o)
    if isinstance(o, float):
        return None if math.isnan(o) else float(o)
    if hasattr(o, 'item'):                 # numpy 标量
        return plain(o.item())
    if hasattr(o, 'tolist'):               # numpy 数组
        return plain(o.tolist())
    return str(o)


def _order(h):
    """hit 的键按 CONTRACT 顺序排；CONTRACT 以外的键（个别模块附带的）排在后面、按名字"""
    out = {k: h[k] for k in HIT_KEYS if k in h}
    for k in sorted(h):
        if k not in out:
            out[k] = h[k]
    return out


def sort_key(h):
    """确定性排序：回合、tick（无 tick 的排在本回合最前）、秒、规则、主角、句子"""
    tk = h.get('tick')
    t = h.get('t')
    return (int(h.get('round') or 0), -1 if tk is None else int(tk), -1e9 if t is None else float(t),
            str(h.get('rule')), str(h.get('subject')), str(h.get('text')))


def collect(ctx, modules=None):
    """跑规则模块，返回排好序的 hit 列表（不写文件；调试用）"""
    hits = []
    mods = modules or ALL
    for m in mods:
        hits += [_order(plain(h)) for h in m.run(ctx)]
    if team in mods and shot in mods:
        _attach_team_exempt(hits, team.exemptions(ctx))
    if util in mods:
        hits.append(_order(plain(_util_stats_hit(ctx))))
    hits.sort(key=sort_key)
    return hits


def _attach_team_exempt(hits, ex):
    """审核单第 7 / 17 条：TEAM-04 豁免掉的你的阵亡（开局控图 / 隔烟 / 单走一侧 / 进点被换 / eco / 远处 / CT 守位）
    记到这次死的 SHOT-06（对枪动作判定）上：facts.team_exempt = reason。merge 据此不让“一个人出去接枪”这类同义句把
    “一个人先露头”写回来（team.exemptions 只在 rules 阶段的 ctx 上，compose 阶段只读 hits.json）"""
    duels = [h for h in hits if h.get('rule') == 'SHOT-06' and isinstance(h.get('death_t'), (int, float))]
    for e in ex:
        if e.get('rule') != 'TEAM-04' or e.get('describe') != 'duel' or not isinstance(e.get('t'), (int, float)):
            continue
        cand = [h for h in duels if h.get('round') == e['round'] and abs(float(h['death_t']) - float(e['t'])) <= 3.0]
        if cand:
            h = min(cand, key=lambda x: (abs(float(x['death_t']) - float(e['t'])), x.get('tick') or 0))
            h['facts'] = dict(h.get('facts') or {}, team_exempt=e.get('reason'))


def _util_stats_hit(ctx):
    """审核单第 3 条：util.match_stats(ctx)（全场带了几颗闪、扔了几颗、长枪阵亡身上有闪的回合、过了五道门的回合）
    作为一条全场命中写进 hits.json，给 summary 的“闪买了常常没扔”用（round=0，不进任何回合，不成时刻）"""
    st = util.match_stats(ctx)
    from .. import common as C
    return {'rule': 'UTIL-01', 'kind': '判定', 'round': 0, 't': None, 'tick': None, 'scope': 'match',
            'subject': C.name_of(ctx, C.user_sid(ctx)), 'is_user': True, 'blame': '—', 'death_t': None, 'death_of': None,
            'merge_key': None, 'facts': {'carrier': 'util_match_stats', 'display': 'summary', 'match_stats': st},
            'text': '', 'alt': '', 'kb': [], 'evidence': ''}


def run_all(ctx):
    """pipeline 阶段：跑全部规则，写 work/<id>/hits.json，返回 hit 列表"""
    hits = collect(ctx)
    out = Path(ctx.work) / 'hits.json'
    out.write_text(json.dumps(hits, ensure_ascii=False, indent=1), encoding='utf-8')
    return hits
