# -*- coding: utf-8 -*-
"""整场总结：headline、比赛走势（momentum）、可以改进的地方（weaknesses）、强项（strengths）。

  headline   ECO-09 总评（facts.slot = summary.headline）；没有时用“上半场比分 + 转折点 + 最终比分”模板
  momentum   points 只来自 ECO-06 / ECO-08（bad）和 ECO-07（good），text 用 ECO-09 势头（slot = summary.momentum），
             没有时把各个点按回合串起来
  weaknesses SUM-01 / SUM-02（这里实现，统计 common.first_bursts + shooting.json）
             + 同一条你的问题规则在 ≥3 个回合出现（SHOT-01/02 已由 SUM-01/02 汇总，不重复）
             + 团队：TIME-09（中远开局被狙首杀），以及同一条团队问题规则在 ≥3 个回合出现
  strengths  SUM-03（这里实现）：同一类好操作在 ≥3 个回合出现并且过同口径门槛；
             decisions_v1 #27：“人数劣势下拿关键击杀”这一类删掉，不列强项
写法：可以改进的地方只写“是什么 + 第几回合 + 还可以…”，不写“下一场”、不写百分比、不和队友比数字。
"""
import re

from . import common as C
from . import config
from . import merge as M

# 阈值默认值（ctx.th['summary'] 有同名键时覆盖；rules_v1 现用值）
DEFAULTS = {
    'sum01_min_moving': 3,          # SUM-01：移动首枪至少几次
    'sum01_rate_x_high': 1.5,       # SUM-01：你的步枪首枪移动率 ≥ 高分的几倍
    'sum02_rate_x_high': 0.6,       # SUM-02：你的停稳首枪命中率 ≤ 高分的几倍
    'sum03_min_rounds': 3,          # SUM-03：同一类好操作至少几个回合
    'trade_s': 3.0,                 # SUM-03 补枪：队友倒下后几秒内补掉算补枪
    'trade_share_high': 0.065,      # SUM-03 补枪门槛：高分局补枪比例（rules_v1 SUM-03 bench）
    'repeat_min_rounds': 3,         # 同一条问题规则几个回合以上算反复出现
    'max_user_weak': 4,             # 你的可以改进的地方最多几条（含 SUM-01/02）
    'max_team_weak': 2,             # 团队的最多几条
    'why_s': 1.5,                   # SUM-02：停稳没中的那一串和 SHOT-02/03 命中相差几秒内算“有具体原因”
    'streak_min': 3,                # 比赛走势：连输 / 连赢几回合以上算一次大起落
}
SUM_SKIP = ('SHOT-01', 'SHOT-02', 'SHOT-06', 'UTIL-01')
# SHOT-01/02 已由 SUM-01/02 汇总；SHOT-06（对枪动作判定）是每次阵亡的结论，不按“同一条规则 ≥3 回合”拼缺点；
# UTIL-01 改成全场习惯“闪买了常常没扔”（审核单第 3 条，util.match_stats），不按回合数拼
TEAM_PATTERN_SKIP = ('TEAM-01', 'TEAM-02', 'TIME-09')

# 反复出现的问题：标题 + 一句说明（没有的用规则名 + 规则 plain）
WEAK_TEXT = {
    'SHOT-03': ('一串打空不调整', '对同一个人连开好几枪都没中，没有停下来调整准星'),
    'SHOT-04': ('狙空枪', 'AWP 连空好几枪，或者没开镜、人还在走就开了'),
    'POS-01': ('杀完人没收', '拿到击杀或狙空之后还站在原地，很快被另一个人打中'),
    'POS-04': ('暴露后换位被没露面的人打', '刚交过火、位置已经暴露，还在走动换位，被一直没露过面的人打死'),
    'TEAM-04': ('一个人先接触、身后没人', '在走动中第一个和对面照面，交火时没有队友看得到那个人'),
    'TEAM-03': ('被同一个人逐个打掉', '同一个人在几秒内把我们的人一个接一个打掉，中间没人补闪或烟'),
    'POS-02': ('同一条线重复拉', '刚有人在这条线上被打死，又从同一条线拉出去'),
    # 下面三条的规则 plain 里有“本来该……”“雷达上……”，不能直接上页面（P6），这里换成平实的说法
    'POS-05': ('人多时主动去接五五开的枪', '我方人数占优、对面得来找我们的时候（CT 没下包，或 T 已下包），你走了一大段去接对面架着的正面枪，被打死'),
    'UTIL-05': ('闪扔出去没起作用', '把身上唯一一颗闪扔出去，谁都没闪到，那附近也没看到人，也没人跟着出去'),
    'TIME-03': ('下水道有动静还在 VIP 原地架', 'CT 在 VIP，下水道已经有道具或有人露面，你还在原地架了好几秒、没换位置也没回道具，被下水道出来的人打死'),
    # 规则 plain 后半句是“看谁走得多来分责任”这种判法说明，不上页面（635 高分玩家45）
    'TEAM-06': ('和队友走散补不上', '前几秒还和队友在一起，后来越拉越远，队友倒下时你离得太远、补不上'),
    # 审核单第 28 条：队友第一个进点倒下、你在后面没跟上（TEAM-05 entry_follow，和普通的“能补没补”分开数）
    'TEAM-05:entry_follow': ('队友进点你没跟上', '队友第一个进点倒下时，你在后面没有及时跟上进点'),
}
# 审核单第 3 条（手册 UTIL-G）：道具的问题是全场习惯“闪买了常常没扔”，不拆成每回合的死因
UTIL_FLASH_TITLE = '闪买了常常没扔'
UTIL_FLASH_ALT = '还可以出去接枪之前先扔一颗闪，等闪爆了再出'
# 多个回合合在一起的缺点只给通用的“还可以”（规则 alt 去掉报点、人名的那一支），不拿某一回合带地点的句子
WEAK_ALT = {
    'SHOT-03': '还可以手枪一枪不中先停半拍，等准星回稳、重新对准再点；步枪前两三发没中就松开重新对准',
    'SHOT-04': '还可以停稳开镜再打；一枪不中先收回掩体，换个角度再出',
    'POS-01': '还可以开完枪或拿到击杀先退回刚才的掩体，再决定下一步',
    # 总结里没有具体的对手，不写“他”；还可以只给一个思路（e6b 缺点 2）
    'POS-02': '还可以换个时机或换条线再出；对面刚在这条线上开过枪，正等着下一个人',
    'POS-04': '还可以暴露以后先退回刚才的掩体或往队友那边收，换位前先用闪或烟挡一下还没清过的角度',
    'TEAM-04': '还可以等附近的队友到位一起出，你一交火队友马上补上',
    'TEAM-03': '还可以两个人一组出，前面的人一交火后面的人马上补，或者先往那边补一颗闪或烟再出',
}
LOST_ONLY = ('TEAM-03',)            # 只数输掉的回合（赢下来的回合里被连杀两个不算“输在这里”）
STRENGTH_GROUPS = [
    # (标题, 规则, 说明, 门槛名)
    ('AWP 停稳开镜、拿到人会换位', ('SHOT-G2', 'POS-G2'),
     '拿 AWP 时先到位、停稳开镜再开枪，拿到人以后会马上挪开开枪的位置。', 'awp'),
    ('队友倒下能马上补枪', ('TEAM-G1',),
     '队友一倒，你常常几秒内就把凶手补掉，人数没被拉开。', 'trade'),
    ('步枪、手枪停稳再开枪', ('SHOT-G1',),
     '对枪时先停稳再开第一枪，很少边走边打。', 'still'),
]
STRENGTH_ALT = '还可以把这类打法固定下来，开局就选能这样打的位置。'
# 每类强项自己的“还可以”：SUM-03 的固定句只对“选位置”的打法（AWP）说得通，补枪、停稳套上去读着不通（8d 强项“队友倒下能马上补枪”）
STRENGTH_ALT_BY = {
    'awp': STRENGTH_ALT,
    'trade': '还可以开局就站在离队友不远、和队友各看一个相邻口子的位置，这样的补枪会更多。',
    'still': '还可以把这个节奏带到拉枪、进点里，出去后先停住再开第一枪。',
}


def _th(ctx):
    t = dict(DEFAULTS)
    t.update((ctx.th or {}).get('summary', {}) if hasattr(ctx, 'th') else {})
    return t


def _pct(s):
    """shooting.json 的 '40%' → 40.0；缺失 → None"""
    if s is None:
        return None
    try:
        return float(str(s).rstrip('%'))
    except ValueError:
        return None


def _rlist(rs):
    return '、'.join(str(r) for r in rs)


def _shooting(ctx):
    try:
        return ctx.json('shooting.json')
    except (OSError, KeyError, ValueError):
        return None


# ---------------------------------------------------------------- 比分
def scores(tl):
    """(半场比分 (a, b) 或 None, 最终比分 (a, b))，我方在前"""
    rs = sorted(tl['rounds'], key=lambda r: r['round'])
    fa, fb = (int(x) for x in str(tl['meta']['final_score']).split(':'))
    half = None
    for r in rs[1:]:
        if r['side'] != rs[0]['side']:
            a, b = (int(x) for x in str(r['score_before']).split(':'))
            half = (a, b)
            break
    return half, (fa, fb)


# ---------------------------------------------------------------- 势头
def points(hits):
    out = []
    for h in sorted(hits, key=M.hit_key):
        r = h.get('round')
        if not isinstance(r, int) or r < 1:
            continue
        if h['rule'] in ('ECO-06', 'ECO-07') and (h.get('facts') or {}).get('branch') in ('awp_carry', 'chain'):
            continue                                    # “AWP 被对面捡走”、跨回合经济链只写在那一回合的经济栏
        if h['rule'] == 'ECO-06':
            out.append({'round': r, 'kind': 'bad', 'title': '反 eco 被翻', 'text': M._part(h['text'])})
        elif h['rule'] == 'ECO-08':
            out.append({'round': r, 'kind': 'bad', 'title': eco08_title(h), 'text': M._part(h['text'])})
        elif h['rule'] == 'ECO-07':
            out.append({'round': r, 'kind': 'good', 'title': eco07_title(h), 'text': M._part(h['text'])})
    seen, res = set(), []
    for p in sorted(out, key=lambda p: (p['round'], p['kind'], p['title'])):
        if (p['round'], p['title']) not in seen:
            seen.add((p['round'], p['title']))
            res.append(p)
    return res


def _sc(x):
    a, b = (int(v) for v in str(x).split(':'))
    return a, b


def streak_points(ctx, tl, econ, th):
    """比分走势里的大起落（rules 只管经济势头，这里从时间线比分看）：
    - 坏：全起输给对面强起 / 半起，从这里开始本半场连输 >= streak_min 回合（bd R8：6:1 全起输给强起后连输 5 回合）；
    - 好：连赢 >= streak_min 回合，从落后追成打平或反超（bd R21–23：9:11 追成 12:11）；
    - 加时：常规时间打平进加时（第 24 回合），加时打完的结果（最后一回合）。
    每个点带 n（连着几回合），标题的转折点按它挑最大的那个"""
    rs = sorted(tl['rounds'], key=lambda r: r['round'])
    if not rs:
        return []
    res = {r['round']: r.get('result') == '赢' for r in rs}
    sb = {r['round']: _sc(r['score_before']) for r in rs}
    try:
        final = _sc(tl['meta']['final_score'])
    except (KeyError, ValueError):
        return []
    last = rs[-1]['round']

    def after(r):
        return sb[r + 1] if r + 1 in sb else final

    E = {e['round']: e for e in (econ or {}).get('rounds', [])}
    n_min = int(th['streak_min'])
    out = []
    for r in sorted(res):
        e = E.get(r) or {}
        us_b, them_b = (e.get('us') or {}).get('buy'), (e.get('them') or {}).get('buy')
        if res[r] or us_b != '全起' or them_b not in ('强起', '半起') or (r - 1 in res and not res[r - 1]
                                                                       and C.half(ctx, r - 1) == C.half(ctx, r)):
            continue
        n = 0
        while r + n in res and not res[r + n] and C.half(ctx, r + n) == C.half(ctx, r):
            n += 1
        if n < n_min:
            continue
        a, b = sb[r]
        c, d = after(r + n - 1)
        out.append({'round': r, 'kind': 'bad', 'n': n, 'title': f'{a}:{b} 全起输给对面{them_b}，连输 {n} 回合',
                    'text': f'第 {r} 回合全起输给对面{them_b}，接着连输 {n} 回合，比分从 {a}:{b} 变成 {c}:{d}'})
    r = rs[0]['round']
    while r <= last:
        if not res.get(r):
            r += 1
            continue
        n = 0
        while r + n in res and res[r + n]:
            n += 1
        a, b = sb[r]
        c, d = after(r + n - 1)
        if n >= n_min and a < b and c >= d:
            out.append({'round': r, 'kind': 'good', 'n': n, 'title': f'连赢 {n} 回合，{a}:{b} {"追平" if c == d else "追成"} {c}:{d}',
                        'text': f'从第 {r} 回合起连赢 {n} 回合，比分从 {a}:{b} {"追平到" if c == d else "反超到"} {c}:{d}'})
        r += n
    # 加时
    hr, ohr = config.halves_th(ctx.th)
    reg = 2 * hr
    if last > reg and reg in res:
        a, b = sb[reg]
        c, d = after(reg)
        mp = a == hr and b < hr
        tp = b == hr and a < hr
        if mp:
            out.append({'round': reg, 'kind': 'bad', 'n': 1, 'title': f'{a}:{b} 赛点没拿下，{c}:{d} 进加时',
                        'text': f'第 {reg} 回合 {a}:{b} 是我们的赛点，输掉后 {c}:{d} 进加时'})
        elif tp:
            out.append({'round': reg, 'kind': 'good', 'n': 1, 'title': f'{a}:{b} 对面赛点扳平，{c}:{d} 进加时',
                        'text': f'第 {reg} 回合 {a}:{b} 是对面的赛点，赢下后 {c}:{d} 进加时'})
        else:
            out.append({'round': reg, 'kind': 'good' if res[reg] else 'bad', 'n': 1, 'title': f'常规时间 {c}:{d} 打平，进加时',
                        'text': f'常规时间打成 {c}:{d}，进加时'})
        # 加时的每个半场打了几比几
        x0, y0 = after(reg)
        halves, h0 = [], reg + 1
        while h0 <= last:
            h1 = min(last, h0 + ohr - 1)
            s0 = sb[h0]
            s1 = after(h1)
            halves.append((s1[0] - s0[0], s1[1] - s0[1]))
            h0 = h1 + 1
        fx, fy = final[0] - x0, final[1] - y0
        hs = '、'.join(f'{u}:{v}' for u, v in halves)
        won = final[0] > final[1]
        out.append({'round': last, 'kind': 'good' if won else 'bad', 'n': 1,
                    'title': f'加时 {fx}:{fy} {"赢下" if won else "输掉"}',
                    'text': f'加时打了 {sum(u + v for u, v in halves)} 回合，' + (f'两个半场 {hs}，' if len(halves) > 1 else '')
                            + f'加时 {fx}:{fy}，最终 {final[0]}:{final[1]} {"赢下" if won else "输掉"}'})
    return sorted(out, key=lambda p: (p['round'], p['kind'], p['title']))


def eco07_title(h):
    t = h.get('text') or ''
    buy = (h.get('facts') or {}).get('buy') or ('eco' if 'eco' in t[:16] else '强起')
    gap = ' ' if buy[-1:].isascii() else ''
    if '止住连败' in t:
        return f'{buy}{gap}翻盘止住连败'
    if '手枪局输了' in t:
        return f'手枪局输了后{buy}{gap}赢下'           # 前面会加“第 N 回合”，不再写“第二回合”（635 高分玩家50）
    return f'{buy}{gap}赢下对面全起，打断对面经济'


def eco08_title(h):
    s = (h.get('facts') or {}).get('score_before') or ''
    pre = f'{s} ' if s else ''
    return pre + ('最好的一买下了包被拆' if '被拆' in (h.get('text') or '') else '满起下了包被打光')


def _eco09(hits, slot):
    for h in sorted(hits, key=M.hit_key):
        if h['rule'] != 'ECO-09':
            continue
        sl = (h.get('facts') or {}).get('slot')
        if sl == slot or (sl is None and (slot == 'summary.headline') == (h['text'] or '').startswith('上半场')):
            return M._part(h['text'])
    return ''


def headline(hits, tl, pts, ctx_th=None):
    s = _eco09(hits, 'summary.headline')
    if s:
        return s
    half, (fa, fb) = scores(tl)
    hr = int(((ctx_th or {}).get('common') or {}).get('half_rounds', 12)) if ctx_th is not None else 12
    if max(fa, fb) < hr + 1 and not (fa == fb == hr):
        # 比赛没打完（demo 截断，高分玩家04 1:4）：不写“最终……输掉”
        n = len(tl['rounds'])
        return f'这份 demo 只录到第 {n} 回合（{fa}:{fb}），比赛没打完'
    res = '赢下' if fa > fb else ('输掉' if fa < fb else '打平')
    lead = ''
    if half:
        a, b = half
        lead = f"上半场 {a}:{b} {'领先' if a > b else ('落后' if a < b else '打平')}，"
    kind = 'good' if fa > fb else 'bad'
    # 转折点挑起落最大的那个（连着几回合 n 最多的；一样多取最早的），不是第一个点（bd：第 4 回合 2:1 eco 赢下不是转折点，
    # 第 21–23 回合连赢三个把 9:11 追成 12:11 才是）；加时那两个点另外写
    cands = [p for p in pts if p['kind'] == kind and not p.get('ot')]
    turn = sorted(cands, key=lambda p: (-int(p.get('n') or 1), p['round']))[0] if cands else None
    mid = f"{_at(turn)}{turn['title']}，" if turn else ''
    ot = next((p for p in pts if p.get('ot') == 'end'), None)
    if ot is not None:
        rr = {r['round']: r for r in tl['rounds']}
        sc = (rr.get(2 * hr + 1) or {}).get('score_before')
        mid += (f'{sc} 进加时，' if sc else '') + ot['title'].replace('赢下', '').replace('输掉', '').rstrip() + '，'
    return f'{lead}{mid}最终 {fa}:{fb} {res}'


def _at(p):
    """“第 N 回合”；连赢 / 连输开头的标题是从这一回合起的一段，写“第 N 回合起”。"""
    return f"第 {p['round']} 回合{'起' if p['title'].startswith('连') else ''}"


def momentum(hits, pts):
    text = _eco09(hits, 'summary.momentum')
    if not text and pts:
        # 同一个结果（“翻盘止住连败”）只写一次：第 7 回合半起、第 20 回合强起都翻盘止住了连败
        groups, order = {}, []
        for p in pts:
            m = re.match(r'^(.*?)(翻盘止住连败|赢下对面全起，打断对面经济)$', p['title'])
            key = m.group(2) if m and m.group(1) else p['title']
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(p)
        segs = []
        for key in order:
            g = groups[key]
            m = re.match(r'^(.*?)' + re.escape(key) + '$', g[0]['title'])
            if len(g) >= 2 and m and m.group(1):
                lead = '、'.join(f"第 {p['round']} 回合{re.match(r'^(.*?)' + re.escape(key) + '$', p['title']).group(1)}"
                                for p in g)
                segs.append(f"{lead}都{key.replace('翻盘止住', '翻盘止住了')}")
            else:
                segs += [f"{_at(p)}{p['title']}" for p in g]
        text = '；'.join(segs)
    return {'text': text + ('。' if text else ''), 'points': pts}


# ---------------------------------------------------------------- SUM-01 / SUM-02（步枪第一枪）
def rifle_first_bursts(ctx):
    """全场 C-FIGHT 第一串里的步枪开枪（不隔烟）→ [串]；数据不够时返回 None。
    rules_v1 写“没被闪”，但 fires_match3 的 15 次里含第 19 回合 35.3（当时还白着 1 秒），原型也不看闪，这里按原型。"""
    try:
        u = C.user_sid(ctx)
        out = []
        for b in C.first_bursts(ctx):
            if b['wcls'] != '步枪':
                continue
            if b.get('target') and C.smoked(ctx, u, b['target'], b['tick']):
                continue
            out.append(b)
        return out
    except (OSError, KeyError, ValueError, IndexError, TypeError, AttributeError):
        return None


def _wcls(w):
    """武器名（facts 里的中文名 AK、M4A1-S、沙鹰…或英文名）→ 大类"""
    for k, v in sorted(C._WEAPON_CN.items()):
        if v == w:
            return C.weapon_class(k)
    return C.weapon_class(w)


def _first_hit(b):
    s0 = (b.get('shots') or [{}])[0]
    return bool(s0.get('hit')) if isinstance(s0, dict) else bool(s0)


# SHOT-G1 各分支夸的是什么（SUM-02 写“另一场对枪”时用）
G1_HOW = {'首枪中': '停稳先手拿下', '反杀': '被先打中后站住反杀'}


def sum01_02(ctx, hits, th, shown=None):
    """→ [weakness]（成立的才有）。shown = 页面上真的写出来的 hit（_visible_hits）；SUM-02 的“具体原因”回合只从这里取"""
    eng = rifle_first_bursts(ctx)
    sh = _shooting(ctx)
    if eng is None or not sh:
        return []
    rf = (sh.get('by_class') or {}).get('步枪') or {}
    me, hi = rf.get('user_m3') or {}, rf.get('high') or {}
    out = []
    mv = [b for b in eng if b['moves'][0] != '停稳']
    st = [b for b in eng if b['moves'][0] == '停稳']
    # 门槛和句子里的次数用同一个来源（这一份第一串列表）；shooting.json 只给高分对标（128：句子写 6 枪中 5、门槛却按 shooting.json）
    k_mv = sum(_first_hit(b) for b in mv)
    k_st = sum(_first_hit(b) for b in st)
    r_mv = 100.0 * k_mv / len(mv) if mv else None
    r_st = 100.0 * k_st / len(st) if st else None
    # SUM-01：移动首枪多；而且移动开的第一枪确实比停稳开的打得差——不然“先停稳再开”的建议和旁边
    # “停稳开的第一枪命中低”并排读着矛盾（128：移动 6 中 5、停稳 15 中 1）
    u_mv, h_mv = (100.0 * len(mv) / len(eng) if eng else None), _pct(hi.get('first_shot_moving_rate'))
    if len(mv) >= th['sum01_min_moving'] and u_mv is not None and h_mv is not None and u_mv >= th['sum01_rate_x_high'] * h_mv             and (r_st is None or r_mv < r_st):
        k_hit = k_mv
        rs = sorted({b['round'] for b in mv})
        if shown is not None:
            # 回合链接只放页面上写了“第一枪没停稳 / 没停住就被先打中”的回合（635：第 20、22 回合页面上什么都没写）；
            # 并进别的开枪句子里的 SHOT-01（facts.also）、写了“第一枪还没停稳”的 TIME-05 也算写了
            def said(h):
                f = h.get('facts') or {}
                # SHOT-05 第一枪是停稳开的（facts.opened_still，只是被打中那一刻人还在动）不是“第一枪没停稳”（128 R4、R5）
                if h['rule'] == 'SHOT-05' and f.get('opened_still'):
                    return False
                return (h['rule'] in ('SHOT-01', 'SHOT-05') or bool({'SHOT-01', 'SHOT-05'} & set(f.get('also') or ()))
                        or (h['rule'] == 'TIME-05' and '还没停稳' in (h.get('text') or ''))
                        # 完整性复核：对枪判定成了时刻、判的就是“第一枪是跑着开的”（bd R29 74.9）也算写了
                        or (h['rule'] == 'SHOT-06' and (f.get('duel') or {}).get('key') == 'no_stop'))
            # 标题是“步枪急停”：手枪、沙鹰、狙的“第一枪没停稳”不进这条的回合链接（8d R13 P250、R18 沙鹰；635 高分玩家45 R1 USP、R3 沙鹰）
            def rifle(h):
                w = str((h.get('facts') or {}).get('weapon') or '')
                if w:
                    return _wcls(w) == '步枪'
                t = h.get('t')
                return isinstance(t, (int, float)) and any(
                    b['round'] == h.get('round') and isinstance(b.get('t'), (int, float)) and abs(b['t'] - t) <= 2.0 for b in mv)
            rs = sorted({h['round'] for h in shown if h.get('is_user') and isinstance(h.get('round'), int) and said(h)
                         and rifle(h)})
        # “只中了”只在比高分移动首枪命中还低时写（5 中 4 不是“只中了”）
        h_mvhit = _pct(hi.get('first_shot_hit_rate_moving'))
        low = h_mvhit is None or 100.0 * k_hit / len(mv) < h_mvhit
        hit_s = '一次都没中' if k_hit == 0 else (f'只中了 {k_hit} 次' if low else f'中了 {k_hit} 次')
        text = (f"全场步枪对人开火的 {len(eng)} 次里，有 {len(mv)} 次第一枪是人还在走就开的，这 {len(mv)} 枪{hit_s}。"
                + M.clean_alt(M.rules()['by_id']['SUM-01']['alt']) + '。')
        out.append({'title': '步枪急停不到位', 'text': text, 'who': '你', 'rounds': rs, '_rule': 'SUM-01'})
    # SUM-02
    u_st, h_st = r_st, _pct(hi.get('first_shot_hit_rate_still'))
    if st and u_st is not None and h_st is not None and u_st <= th['sum02_rate_x_high'] * h_st:
        k_hit = k_st
        missed = [b for b in st if not _first_hit(b)]
        miss = sorted({b['round'] for b in missed})
        # 每一串单独看：同一回合 ±why_s 秒内有页面上写出来的 SHOT-02（准星没放到位）/ SHOT-03（一串打空没调整）= 有具体原因；
        # 其余只计数。SHOT-G1 是夸奖（停稳后打死人），不能算成“准星没放到位”的原因（e6b：夸的回合被列进了缺点）
        src = shown if shown is not None else hits
        why = [(h['round'], h.get('t')) for h in src if h['rule'] in ('SHOT-02', 'SHOT-03') and h.get('is_user')
               and isinstance(h.get('round'), int) and isinstance(h.get('t'), (int, float))]
        has_why = [b for b in missed if any(r == b['round'] and abs(t - b['t']) <= th['why_s'] for r, t in why)]
        why_rs = sorted({b['round'] for b in has_why})
        other = sorted({b['round'] for b in missed if not any(b is x for x in has_why)})
        hit_s = '一次都没中' if k_hit == 0 else f'只中了 {k_hit} 次'
        text = f"全场停稳开的第一枪 {len(st)} 次{hit_s}"
        # 同一个链接回合里还夸了一次“停稳……拿下”（SHOT-G1）：写明那是另一场对枪，不然点进去看到的是一夸一批（8d R21：
        # 20.9 秒对 对手3 一串打空 / 39.8 秒对 对手2 停稳修准星拿下）
        g1 = [h for h in (shown or ()) if h['rule'] == 'SHOT-G1' and h.get('is_user') and isinstance(h.get('round'), int)
              and isinstance(h.get('t'), (int, float))]
        other_duel, how_order = {}, []
        for h in sorted(g1, key=M.hit_key):
            if h['round'] in why_rs and not any(r == h['round'] and abs(t - h['t']) <= th['why_s'] for r, t in why):
                how = G1_HOW.get((h.get('facts') or {}).get('variant'), '停稳修准星拿下')
                if how not in other_duel:
                    other_duel[how] = []
                    how_order.append(how)
                other_duel[how].append(f"第 {h['round']} 回合第 {C.fmt_sec(h['t'])} 秒")
        if why_rs:
            # 只说是什么、写在哪几回合；不写“……不算在里面”这种怎么统计的说明（8d 总结：读着绕）
            # 也不写“其中……具体写在”这种指路的话（8d 总结复查：还是在解释怎么数），直接说是哪几回合
            text += f"；准星没放到位、一串打空没调整的在第 {_rlist(why_rs)} 回合"
        # 只靠“停稳但第一枪没中、后面修准星拿下”的夸奖时刻链接的回合：写明那几次是先空一枪、后面修准星拿下的
        g1_only = []
        if shown is not None:
            miss_shown = {h['round'] for h in shown if h['rule'] == 'SHOT-G1' and h.get('is_user')
                          and (h.get('facts') or {}).get('variant') not in ('首枪中', '反杀')}
            g1_only = sorted((miss_shown & set(miss)) - set(why_rs))
        if g1_only:
            text += f"；第 {_rlist(g1_only)} 回合是第一枪没中、后面停着修准星把人拿下的"
        text += '。' + M.clean_alt(M.rules()['by_id']['SUM-02']['alt']) + '。'
        # 回合链接只放页面上写了这件事的回合：有具体原因的，和页面上写了“停稳但第一枪没中”的好操作时刻（635 高分玩家04 R5）
        if shown is not None:
            miss = sorted(set(why_rs) | set(g1_only))
        out.append({'title': '步枪停稳开的第一枪命中低', 'text': text, 'who': '你', 'rounds': miss, '_rule': 'SUM-02'})
    return out


# ---------------------------------------------------------------- 反复出现的问题
def _visible_hits(merged):
    """页面上真的写出来的问题 hit：时刻的主句和从句（TEAM-04 只作半句时不算，它是最通用的一条）"""
    out = []
    for rn in sorted(merged):
        for u in merged[rn]['units']:
            out.append(u['hit'])
            skip = u.get('skip') or ()
            out += [c for c in u['clauses'] if c['rule'] != 'TEAM-04' and not any(c is x for x in skip)
                    and M._covered(c, u['hit']) != '']        # 页面上没写出来的从句不算
            out += list(u.get('annot') or ())
    return out


def repeated(hits, th, user_side, won=()):
    """同一条问题规则在 ≥N 个回合出现 → [(规则, [回合])]，user_side=True 看你的、False 看团队的；
    LOST_ONLY 里的规则只数输掉的回合（won = 赢下的回合号）"""
    by, major = {}, {}
    for h in hits:
        r = h.get('round')
        if not isinstance(r, int) or r < 1 or h.get('kind') not in ('问题',):
            continue
        if h['rule'] in LOST_ONLY and r in won:
            continue
        f = h.get('facts') or {}
        if h['rule'] == 'TEAM-04' and f.get('variant') == 'entry':
            continue                # 进点后面的人没跟上：记团队，不是你“一个人先露头”（审核单第 28 条）
        rid = h['rule'] + (':entry_follow' if h['rule'] == 'TEAM-05' and f.get('variant') == 'entry_follow' else '')
        mb, contrib = M.blame_parts(h.get('blame'))
        # 页面上写出来的你的问题：你的责任、两边都有、小问题都算（merge.YOUR_BLAMES；8d UTIL-01 第 3、17 回合你的责任，
        # 第 6、19、22 回合小问题，原来只数了前两个）；至少有一回合是你的责任，全是小问题的不当成整场的缺点
        mine = bool(h.get('is_user')) and mb in M.YOUR_BLAMES
        team = not h.get('is_user') and mb == '队友或团队'
        if (user_side and mine) or (not user_side and team):
            by.setdefault(rid, set()).add(r)
            if contrib == 'major' or not user_side:
                major[rid] = True
    out = [(rid, sorted(rs)) for rid, rs in by.items() if len(rs) >= th['repeat_min_rounds'] and major.get(rid)]
    return sorted(out, key=lambda x: (-len(x[1]), M.rule_idx(x[0].split(':')[0]), x[0]))


def _hit_alt(hits, rid, user_side):
    """这条规则实际命中里第一条填好了的“还可以”（规则表里的 alt 是带分支的模板，不能直接用）"""
    for h in sorted(hits, key=M.hit_key):
        if h['rule'] == rid and bool(h.get('is_user')) == user_side:
            a = M.clean_alt(h.get('alt'))
            if a:
                return a
    return ''


def _weak_from_rule(rid, rs, who, hits):
    key, rid = rid, rid.split(':')[0]          # 'TEAM-05:entry_follow' 这种分支单独数，规则还是 TEAM-05
    r = M.rules()['by_id'].get(rid, {})
    # 没有专门写法时只取规则 plain 的第一句、去掉括注：“；”后面和括号里多是判法说明，不上页面
    plain = re.sub(r'（[^）]*）', '', r.get('plain', '')).split('；')[0]
    title, desc = WEAK_TEXT.get(key, WEAK_TEXT.get(rid, (M.rule_name(rid), M._part(plain))))
    # 通用的还可以；没有时只从列出来的回合里取命中的 alt（不拿没列出来的回合、别处的地点）
    alt = WEAK_ALT.get(rid) or _hit_alt([h for h in hits if h.get('round') in rs], rid, who == '你')
    rh = [h for h in hits if h['rule'] == rid and h.get('round') in rs and bool(h.get('is_user')) == (who == '你')
          and not M.is_entry(h)]
    if rid == 'POS-02':
        # 按实际的情况写：队友刚被打死 / 刚被打掉血（e6b R9：高分玩家04 只是被打中过）
        vs = {str((h.get('facts') or {}).get('variant') or '') for h in rh}
        if vs and vs <= {'self'}:
            desc = '刚在这条线上被打掉血，又从同一条线拉出去'
        elif 'self' in vs:
            desc = '刚有人在这条线上被打死或被打掉血，又从同一条线拉出去'
    if rid == 'TEAM-04':
        # 按 facts.near 写：附近其实有队友时不写“身后没人”（e6b R7/R9/R12、128 R1/R4/R23）
        nears = {bool((h.get('facts') or {}).get('near')) for h in rh}
        if nears == {True}:
            title, desc = '比附近的队友先露头、没人看得到对手', '在走动中比附近的队友先和对面照面，交火时没有队友看得到那个人'
        elif nears == {True, False}:
            title, desc = '第一个和对面照面、没人看得到对手', '在走动中第一个和对面照面，交火时没有队友看得到那个人'
    if rid == 'SHOT-04':
        alt = _awp_alt(_awp_reasons(hits, rs))      # 还可以按这几枪实际空的原因选一个（8d：没有没开镜也写“停稳开镜再打”）
    text = f'{desc}（第 {_rlist(rs)} 回合）。' + (alt + '。' if alt else '')
    return {'title': title, 'text': text, 'who': who, 'rounds': rs, '_rule': rid}


def util_flash(hits, vis):
    """审核单第 3 条（手册 UTIL-G）：“闪买了常常没扔”。数据是 util.match_stats（rules 阶段写进 hits 的全场一条，
    facts.carrier='util_match_stats'）。成立：长枪局阵亡时身上还有闪的比例高于高分局，并且每回合扔闪少于高分局（比例只做判断，
    页面写次数）；回合链接只放过了 UTIL-01 五道门、页面上也写出了 UTIL-01 的回合"""
    st = next((((h.get('facts') or {}).get('match_stats')) for h in hits
               if (h.get('facts') or {}).get('carrier') == 'util_match_stats'), None)
    if not isinstance(st, dict):
        return None
    b = st.get('bench') or {}
    ld, ldf = list(st.get('long_deaths') or []), list(st.get('long_deaths_flash') or [])
    n, carried, thrown = int(st.get('rounds') or 0), int(st.get('flash_carried') or 0), int(st.get('flash_thrown') or 0)
    hi_hold, hi_rate = b.get('long_death_flash_high'), b.get('flash_per_round_high')
    if not (ld and n and carried and isinstance(hi_hold, (int, float)) and isinstance(hi_rate, (int, float))):
        return None
    if not (len(ldf) / len(ld) > hi_hold and thrown / n < hi_rate):
        return None
    passed = {int(r) for r in st.get('u01_pass_rounds') or []}
    rs = sorted({h['round'] for h in vis if h['rule'] == 'UTIL-01' and h.get('is_user') and h.get('round') in passed})
    thr = f'只扔出去 {thrown} 颗' if thrown else '一颗都没扔出去'
    text = (f'全场 {n} 个回合一共带了 {carried} 颗闪，{thr}；长枪局 {len(ld)} 次阵亡里有 {len(ldf)} 次身上还有闪'
            + (f'（第 {_rlist(rs)} 回合是出去接枪前闪还在身上的）' if rs else '') + '。' + UTIL_FLASH_ALT + '。')
    return {'title': UTIL_FLASH_TITLE, 'text': text, 'who': '你', 'rounds': rs, '_rule': 'UTIL-01'}


def weaknesses(ctx, hits, th, merged, won=()):
    vis = _visible_hits(merged)
    shown = vis + [a for rn in sorted(merged) for u in merged[rn]['units'] for a in u['annot']]   # 并进好操作时刻的小问题也写出来了
    out = sum01_02(ctx, hits, th, shown)
    uf = util_flash(hits, shown)
    if uf is not None:
        out.append(uf)
    s02 = next((w for w in out if w.get('_rule') == 'SUM-02'), None)
    for rid, rs in repeated(vis, th, True, won):
        if rid in SUM_SKIP:
            continue
        # SUM-02 已经写了“一串打空没调整的，具体写在第 … 回合”：SHOT-03 的回合全在里面时不再单列一条（8d 总结两条重了一半）
        if rid == 'SHOT-03' and s02 is not None and set(rs) <= set(s02.get('rounds') or ()):
            continue
        out.append(_weak_from_rule(rid, rs, '你', hits))
    # 经济：同一个人本半场几个长枪局都拿冲锋枪（ECO-04 facts.slot = summary.team，回合那边已经并掉了，只在这里写）
    eco_team = []
    for h in sorted(hits, key=M.hit_key):
        f = h.get('facts') or {}
        if h['rule'] == 'ECO-04' and f.get('slot') == 'summary.team' and isinstance(f.get('rounds'), list):
            who = '你' if h.get('is_user') else '团队'
            alt = M.clean_alt(h.get('alt'))
            w = {'title': (f"{f.get('name')} 长枪局常拿冲锋枪" if who == '团队' else '长枪局常拿冲锋枪'), 'who': who,
                 'rounds': sorted(int(x) for x in f['rounds']), 'text': M.sp(M._part(h['text']) + '。' + (alt + '。' if alt else '')),
                 '_rule': 'ECO-04'}
            (out if who == '你' else eco_team).append(w)
    out = out[:th['max_user_weak']]
    team = []
    # 团队：TIME-09（整场一条）+ 反复出现的团队问题
    for h in sorted(hits, key=M.hit_key):
        if h['rule'] == 'TIME-09':
            rs = (h.get('facts') or {}).get('rounds')
            if not isinstance(rs, list):
                rs = sorted({int(x) for x in re.findall(r'(\d+)', (h['text'] or '').split('回合')[0])})
            alt = M.clean_alt(h.get('alt'))
            team.append({'title': '中远开局常被对面 AWP 拿首杀', 'who': '团队', 'rounds': sorted(rs),
                         'text': M._part(h['text']) + '。' + (alt + '。' if alt else ''), '_rule': 'TIME-09'})
            break
    for rid, rs in repeated(vis, th, False, won):
        if rid not in TEAM_PATTERN_SKIP:
            team.append(_weak_from_rule(rid, rs, '团队', hits))
    team += eco_team
    out += team[:th['max_team_weak']]
    return out


# ---------------------------------------------------------------- SUM-03 强项
def _trade_share(ctx, tl, trade_s):
    """本场你还活着时队友倒下、trade_s 秒内你打死凶手的比例（时间线击杀栏；第 3 场 4/48，和 rules_v1 SUM-03 bench 一致）"""
    # 认用户用击杀栏里的 victim_is_user / killer_is_user（按 steamid 算的），不按名字
    user = tl['meta']['user']
    n = k = 0
    for r in tl['rounds']:
        ks = [x for x in r.get('kills') or [] if not x.get('post_round') and not x.get('teamkill')]
        ud = next((x['t'] for x in ks if x.get('victim_is_user', x['victim'] == user)), None)
        for x in ks:
            if x.get('victim_is_mate') and not x.get('victim_is_user') and (ud is None or x['t'] < ud):
                n += 1
                if any(y.get('killer_is_user', y['killer'] == user) and y['victim'] == x['killer'] and 0 <= y['t'] - x['t'] <= trade_s
                       for y in ks):
                    k += 1
    return (k / n) if n else None


STILL_CLASSES = ('步枪', '手枪')


def strengths(ctx, hits, tl, th, shown=None):
    sh = _shooting(ctx) or {}
    bc = sh.get('by_class') or {}
    gates = {}
    awp_me = _pct(((bc.get('狙') or {}).get('user_m3') or {}).get('first_shot_hit_rate'))
    awp_hi = _pct(((bc.get('狙') or {}).get('high') or {}).get('first_shot_hit_rate'))
    gates['awp'] = awp_me is not None and awp_hi is not None and awp_me >= awp_hi
    ts = _trade_share(ctx, tl, th['trade_s'])
    gates['trade'] = ts is not None and ts >= th['trade_share_high']
    mv_me = _pct(((bc.get('步枪') or {}).get('user_m3') or {}).get('first_shot_moving_rate'))
    mv_hi = _pct(((bc.get('步枪') or {}).get('high') or {}).get('first_shot_moving_rate'))
    gates['still'] = mv_me is not None and mv_hi is not None and mv_me <= mv_hi
    out = []
    for title, rules, text, gate in STRENGTH_GROUPS:
        if not gates.get(gate):
            continue
        rs = set()
        for h in hits:
            if h['rule'] not in rules or not h.get('is_user') or not isinstance(h.get('round'), int):
                continue
            f = h.get('facts') or {}
            if f.get('econ_tag'):                       # 带“对面 eco/强起”经济标签的不算
                continue
            if h['rule'] == 'TEAM-G1':
                ts_ = f.get('trade_s', f.get('dt'))
                v = str(f.get('variant') or '')
                if not ((isinstance(ts_, (int, float)) and ts_ <= th['trade_s']) or v == '补枪'):
                    continue
            if h['rule'] == 'POS-G2' and f.get('quick') is False:
                continue                                # 拿到人后过了好几秒才走的不算“马上换位”（8d R7）
            if gate == 'still':
                # 只算步枪、手枪（128 R24 MP9）；同一回合页面上写了同一类枪“第一枪没停稳 / 没停住”的不算（128 R13 USP）
                wc = _wcls(f.get('weapon'))
                if wc not in STILL_CLASSES:
                    continue
                if any(x['rule'] in ('SHOT-01', 'SHOT-05') and x.get('is_user') and x.get('round') == h['round']
                       and _wcls((x.get('facts') or {}).get('weapon')) == wc for x in hits):
                    continue
            rs.add(h['round'])
        if len(rs) >= th['sum03_min_rounds']:
            if gate == 'awp':
                text = _awp_strength_text(hits, shown, sorted(rs), text)
            out.append({'title': title, 'text': text + STRENGTH_ALT_BY.get(gate, STRENGTH_ALT), 'rounds': sorted(rs), '_gate': gate})
    return out


def _awp_strength_text(hits, shown, rs, text):
    """AWP 强项的说明按链接回合的实际情况写（8d：第 9、12 回合标题就是“你狙空枪”，第 7 回合约 5 秒后才挪开）：
    链接回合里页面上写了你的 SHOT-04（狙空枪）时，只夸拿到人的那几枪，并说明那几回合也有狙空的；
    链接回合里有过了好几秒才挪开的 POS-G2 时不写“马上”"""
    src = shown if shown is not None else hits
    miss = sorted({h['round'] for h in src if h['rule'] == 'SHOT-04' and h.get('is_user') and h.get('round') in rs
                   and M.blame_parts(h.get('blame'))[1] != 'none'})
    slow = any(h['rule'] == 'POS-G2' and h.get('is_user') and h.get('round') in rs
               and (h.get('facts') or {}).get('quick') is False for h in hits)
    move = '拿到人以后会挪开开枪的位置' if slow else '拿到人以后会马上挪开开枪的位置'
    if not miss:
        return f'拿 AWP 时先到位、停稳开镜再开枪，{move}。'
    return f'拿 AWP 拿到人的那几枪都是先到位、停稳开镜打的，{move}；第 {_rlist(miss)} 回合也有狙空的几枪，写在回合里。'


# ---------------------------------------------------------------- 强项和缺点说的是同一把枪、同几个回合：写清分界
AWP_REASON_CN = {'没开镜': '没开镜', '人还在走': '人还在走', '他横着跑': '对面横着跑', '距离很远': '距离很远'}   # 总结里没有具体的人，不写“他”


def _awp_reasons(hits, rounds):
    reasons = []
    for h in sorted(hits, key=M.hit_key):
        if h['rule'] != 'SHOT-04' or h.get('round') not in rounds or not h.get('is_user'):
            continue
        for x in (h.get('facts') or {}).get('shots') or []:
            for r in x.get('reasons') or ['准星没在人身上']:
                r = AWP_REASON_CN.get(r, r)
                if r not in reasons:
                    reasons.append(r)
    return reasons


def _awp_alt(reasons):
    """狙空枪的“还可以”只给一个，按实际原因选（没开镜 / 人还在走 > 他横着跑 > 其他）"""
    if '没开镜' in reasons or '人还在走' in reasons:
        return '还可以停稳开镜再打'
    if '他横着跑' in reasons or '对面横着跑' in reasons:
        return '还可以准星先放在对面要经过的口子，等人走进来再开'
    return '还可以一枪不中先收回掩体，换个角度再出'


def _split_awp(weak, strong, hits):
    """AWP 强项（停稳开镜）和“狙空枪”回合重叠：按情况分开写，各说各的那一类枪，不互相引用
    （8d：优点里又把缺点说了一遍，缺点的“还可以停稳开镜再打”正是优点夸的那件事）"""
    sw = next((w for w in weak if w.get('_rule') == 'SHOT-04'), None)
    st = next((x for x in strong if x.get('_gate') == 'awp'), None)
    if sw is None or st is None:
        return
    both = sorted(set(sw['rounds']) & set(st['rounds']))
    if not both:
        return
    reasons = _awp_reasons(hits, sw['rounds'])
    why = '、'.join(reasons[:3]) if reasons else '准星没在人身上'      # 最多写三种原因
    sw['title'] = 'AWP 没准备好就开枪'
    sw['text'] = f"AWP 空的那几枪多是{why}的时候开的（第 {_rlist(sw['rounds'])} 回合）。" + _awp_alt(reasons) + '。'
    st['title'] = 'AWP 停稳开镜的枪打得好'
    st['text'] = '拿 AWP 先到位、停稳开镜再开的那几枪基本都拿下了，拿到人以后会挪开开枪的位置。' + STRENGTH_ALT


def _split_trade(weak, strong):
    """“队友倒下能马上补枪”和“和队友走散补不上”同时出现：写清分界——补得上的是你就在旁边的时候；
    两边都有的回合不放进强项的链接（128：第 9 回合两边都有）"""
    sw = next((w for w in weak if w.get('_rule') == 'TEAM-06'), None)
    st = next((x for x in strong if x.get('_gate') == 'trade'), None)
    if sw is None or st is None:
        return
    st['text'] = ('你就在队友旁边、看着同一个口子的时候，队友一倒你常常几秒内就把凶手补掉；换位走远以后就补不上了，写在缺点里。'
                  + STRENGTH_ALT_BY['trade'])
    rest = [r for r in st['rounds'] if r not in set(sw['rounds'])]
    if rest:
        st['rounds'] = rest


def _split_pos01(weak, strong):
    """“杀完人没收”和 AWP 强项“拿到人以后会挪开开枪的位置”同时出现：强项不再写挪开位置这半句，
    两边都有的回合不放进强项的链接（bd：第 19、22 回合一边夸会挪开、一边批没收）"""
    sw = next((w for w in weak if w.get('_rule') == 'POS-01'), None)
    st = next((x for x in strong if x.get('_gate') == 'awp'), None)
    if sw is None or st is None:
        return
    st['text'] = re.sub(r'[，；]拿到人以后会(?:马上)?挪开开枪的位置', '', st['text'])
    st['title'] = re.sub(r'[、，]拿到人会换位', '', st.get('title') or '')
    rest = [r for r in st['rounds'] if r not in set(sw['rounds'])]
    if rest:
        st['rounds'] = rest


def _awp_vs_stay(strong, shown):
    """完整性复核：AWP 强项写“拿到人以后会挪开开枪的位置”，但链接的回合里页面上写着你“还站在原地”（POS-01，或狙空后
    还站在原地）：两条读着矛盾（bd 第 19、22 回合），强项不再写挪开位置这半句（_split_pos01 只管成了全场缺点的情况）"""
    st = next((x for x in strong if x.get('_gate') == 'awp'), None)
    if st is None or not re.search(r'挪开开枪的位置', st.get('text') or ''):
        return
    rs = set(st.get('rounds') or ())
    stay = [h for h in shown or () if h.get('is_user') and h.get('round') in rs
            and (h['rule'] == 'POS-01' or '还站在原地' in (h.get('text') or ''))]
    if stay:
        st['text'] = re.sub(r'[，；]拿到人以后会(?:马上)?挪开开枪的位置', '', st['text'])
        st['title'] = re.sub(r'[、，]拿到人会换位', '', st.get('title') or '')


def _split_still(weak, strong):
    """SUM-02（停稳开的第一枪命中低）和 SUM-03“停稳再开枪”同时出现时，强项只说“对枪前会先停稳”这一面，
    不再写“先打中的一方基本就赢了”——和旁边“停稳第一枪只中了 N 次”并排读着矛盾（用户规则：看着矛盾的两条要分开写）"""
    sw = next((w for w in weak if w.get('_rule') == 'SUM-02'), None)
    st = next((x for x in strong if x.get('_gate') == 'still'), None)
    if sw is None or st is None:
        return
    st['title'] = '步枪、手枪对枪前会先停稳'
    st['text'] = ('对枪时一般先停稳再开第一枪，很少边走边打；停稳以后第一枪打不打得中是另一回事，写在缺点里。'
                  + STRENGTH_ALT_BY['still'])


# ---------------------------------------------------------------- 入口
# 总结里一条缺点 / 强项引用的回合，页面上要看得到的规则（第六轮拍板 2：compose 筛时刻时保住这些）
CITE_RULES = {'SUM-01': ('SHOT-01', 'SHOT-05', 'TIME-05'), 'SUM-02': ('SHOT-02', 'SHOT-03', 'SHOT-G1')}


def build(ctx, hits, rounds, merged, cited=None):
    """→ review['summary']。cited 传 dict 时填上 {回合: {(规则, ...)}}：总结的每一条引用了这一回合的哪一组规则（compose 筛时刻时每组保住一个）"""
    th = _th(ctx)
    tl = ctx.json('timeline.json')
    hits = sorted((M.norm_hit(h) for h in hits), key=M.hit_key)
    pts = points(hits)
    # 比分走势里的大起落 + 加时（同一回合已经有经济势头的点就不再加）
    have = {p['round'] for p in pts}
    extra = streak_points(ctx, tl, ctx.json('economy.json'), th)
    hr = config.halves_th(ctx.th)[0]
    for p in extra:
        if p['round'] > 2 * hr and p['title'].startswith('加时'):
            p['ot'] = 'end'
        if p['round'] in have and not p.get('ot'):
            continue
        pts.append(p)
    pts = sorted(pts, key=lambda p: (p['round'], p['kind'], p['title']))
    won = {r['round'] for r in tl['rounds'] if r.get('result') == '赢'}
    shown = _visible_hits(merged)
    weak, strong = weaknesses(ctx, hits, th, merged, won), strengths(ctx, hits, tl, th, shown)
    _split_awp(weak, strong, hits)
    _split_pos01(weak, strong)
    _awp_vs_stay(strong, shown)
    _split_still(weak, strong)
    _split_trade(weak, strong)
    if cited is not None:
        groups = {g: rules for _, rules, _, g in STRENGTH_GROUPS}
        # 记成（回合, 一组规则）：compose 筛时刻时每组只保住最强的一个（不是这组规则的每一个时刻）
        for w in weak:
            for r in w.get('rounds') or ():
                cited.setdefault(r, set()).add(tuple(CITE_RULES.get(w.get('_rule'), (w.get('_rule'),))))
        for x in strong:
            g = tuple(groups.get(x.get('_gate'), ()))
            for r in (x.get('rounds') or ()) if g else ():
                cited.setdefault(r, set()).add(g)
    mom = momentum(hits, [{k: v for k, v in p.items() if k not in ('n', 'ot')} for p in pts])
    out = {'headline': headline(hits, tl, pts, getattr(ctx, 'th', None)), 'momentum': mom,
           'weaknesses': [{k: w[k] for k in ('title', 'text', 'who', 'rounds')} for w in weak],
           'strengths': [{k: x[k] for k in ('title', 'text', 'rounds')} for x in strong]}
    # 中文和英文/数字之间补空格（只动空格）
    out['headline'] = M.sp(out['headline'])
    out['momentum']['text'] = M.sp(out['momentum']['text'])
    for p in out['momentum']['points']:
        p['title'], p['text'] = M.sp(p['title']), M.sp(p['text'])
    for w in out['weaknesses'] + out['strengths']:
        w['title'], w['text'] = M.sp(w['title']), M.sp(w['text'])
    return out
