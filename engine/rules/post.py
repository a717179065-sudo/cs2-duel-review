# -*- coding: utf-8 -*-
"""规则：下包后·回防·保枪（POST-01、POST-02、POST-03、POST-04、POST-G1）。

手册 v1 审核单（2026-10-30）落地：
  - 第 20 条 / ECON-B：T 没下包活到时间结束拿不到败方奖励，保枪净值 = 枪 + 甲 - 败方奖励；
    净值不到 save_net_min、或不是 AWP 且下回合是半场最后一回合：不判（_t_save_value）；T 方句子不写“省下的钱给队友发枪”。
  - 第 21 条 / CT-E：CT 下包后“去回防”按意图认（_retake_intent：往包走 / 回防烟闪 / 一直往包走）；
    少 1 人、离包远去回防被打死只出“思路”（_post01_far）。
  - Q11：CT 包前 1vN 拉出去，只在去拉刚响过枪的角度时记小问题（_known_angle），保枪作还可以。
  - Q12：下包后等着被对面找到 = 合理代价，往远离包点一侧撤作另一种思路（判定 hit 的 alt + 一条 kind=思路）。
  - Q18：POST-03 拿着火往包那边挪 = 去拦拆包（合理代价）；只有走进已知敌人的枪线才照旧记问题。

来源：pro/analysis/match3/rules_proto/下包后_回防_保枪.py（rule_save / rule_retake_solo / rule_t_leave /
rule_t_moving / rule_good_together），按 rules_v1.json 的 trigger / blame / template / alt 改写：
  - 人数、存活、可见性、雷达、停稳、报点、武器名一律用 engine.common（C-*），不再自己算；
  - 阈值全部来自 ctx.th['post']（thresholds/post.json）和 ctx.th['common']；
  - 原型读的 positions_*.csv（装备值）、duels CSV（对枪方式）换成 pp_ticks 的 inventory + 价格表、
    timeline.json 的 user_fights.engage，这样任何一场 demo 都能跑。

用法：
    from engine.rules import post
    hits = post.run(ctx)          # list[Hit]，字段见 engine/CONTRACT.md

句子遵守 P6：没有坐标、速度、角度、百分比；报点用 C.place_cn；不写对方血量、不写语音建议、不引用知识库原文
（知识库条目只放在 kb 字段）。
"""
import math

import numpy as np

from engine import common as C

RULES = ('POST-01', 'POST-02', 'POST-03', 'POST-04', 'POST-G1')

# 道具 / 刀在手上时的说法（POST-03“手里拿着燃烧弹”）
_HAND_CN = {'incgrenade': '燃烧弹', 'molotov': '燃烧瓶', 'flashbang': '闪光弹', 'smokegrenade': '烟雾弹',
            'hegrenade': '手雷', 'decoy': '诱饵弹', 'knife': '刀', 'c4': 'C4', 'taser': '电击枪'}
_NADE_ORDER = ('闪', '烟', '雷', '火')


# ================================================================ 小工具
def _th(ctx):
    return ctx.th['post']


def _tr(ctx):
    return int(ctx.th['common'].get('tickrate', C.TR))


def _s2t(ctx, s):
    """秒 → tick 数"""
    return int(round(float(s) * _tr(ctx)))


def _int(v):
    """四舍五入取整（不用 round 的半数取偶）"""
    return int(math.floor(float(v) + 0.5))


def _at(place):
    """'超市' → '超市的 '；报点没收录时返回空（句子写成“被 X 打死”）"""
    return '' if place in (None, '', '某处') else f'{place}的 '


def _place(ctx, sid, tick):
    """句子用的中文报点；没收录返回 None"""
    p = C.place_cn_at(ctx, sid, tick, default='')
    return p or None


def _timeline_round(ctx, rnd):
    tl = ctx.json('timeline.json')
    for r in tl['rounds']:
        if int(r['round']) == int(rnd):
            return r
    return None


# ---------------------------------------------------------------- 背包（pp_ticks 的 inventory，每 8 tick 一帧）
def _inv_index(ctx):
    """{sid: (ticks 数组, [背包武器键列表], has_defuser 数组)}；没有 pp_ticks 缓存时返回 {}"""
    idx = getattr(ctx, '_post_inv', None)
    if idx is not None:
        return idx
    idx = {}
    try:
        raw = ctx.cache('pp_ticks')
        T = raw['ticks']
    except (FileNotFoundError, KeyError, OSError, TypeError):
        T = None
    if T is not None and 'inventory' in T:
        T = T[['tick', 'steamid', 'inventory'] + (['has_defuser'] if 'has_defuser' in T else [])].copy()
        T['sid'] = T['steamid'].map(C._sid_str)
        T = T.sort_values(['sid', 'tick'], kind='mergesort')
        for sid, g in T.groupby('sid', sort=True):
            inv = [tuple(k for k in (C.wkey(x) for x in (v if v is not None else ())) if k) for v in g['inventory']]
            kit = g['has_defuser'].fillna(False).astype(bool).to_numpy() if 'has_defuser' in g else np.zeros(len(g), bool)
            idx[sid] = (g['tick'].to_numpy(np.int64), inv, kit)
    try:
        setattr(ctx, '_post_inv', idx)
    except AttributeError:
        pass
    return idx


def _inventory(ctx, sid, tick):
    """tick 之前（含）最近一帧的背包（武器键元组）；没有数据返回 ()"""
    e = _inv_index(ctx).get(C._sid_str(sid))
    if e is None:
        return ()
    i = int(np.searchsorted(e[0], int(tick), side='right')) - 1
    return e[1][i] if i >= 0 else ()


def _has_kit(ctx, sid, tick):
    e = _inv_index(ctx).get(C._sid_str(sid))
    if e is None:
        return False
    i = int(np.searchsorted(e[0], int(tick), side='right')) - 1
    return bool(e[2][i]) if i >= 0 else False


def _primary(keys):
    """背包里的主武器（冲锋枪、霰弹、机枪、步枪、狙；取枪档最高的那把）；没有返回 None"""
    best = None
    for k in keys:
        tr = C.gun_tier(k)
        if tr >= 1 and (best is None or tr > C.gun_tier(best)):
            best = k
    return best


def _nades(keys):
    """背包里的道具，中文（闪、烟、雷、火），按固定顺序去重"""
    have = {C.weapon_cn(k) for k in keys if C.wkey(k) in ('flashbang', 'smokegrenade', 'hegrenade', 'molotov', 'incgrenade')}
    return [n for n in _NADE_ORDER if n in have]


def _equip(ctx, rnd, sid, tick):
    """tick 时的装备值 = 背包里每件的价格 + 甲（economy.json 这回合买的头盔/甲）+ 钳子"""
    price = _th(ctx)['price']
    v = sum(int(price.get(k, 0)) for k in _inventory(ctx, sid, tick))
    er = C.econ_round(ctx, rnd)
    name = C.name_of(ctx, sid)
    for blk in (('us', 'them') if er else ()):
        for p in er[blk].get('players', []):
            if p.get('name') == name:
                v += int(price['helmet']) if p.get('helmet') else (int(price['vest']) if p.get('armor') else 0)
    if _has_kit(ctx, sid, tick):
        v += int(price['kit'])
    return v


# ---------------------------------------------------------------- 包（下包 / 拆包事件）
def _bomb_ev(ctx, key):
    """bomb_begindefuse / bomb_defused 事件表（tick、user_steamid 字符串、haskit）：先读 pp_ticks['ev']，没有时读 ev_<key>.csv"""
    memo = getattr(ctx, '_post_bomb', None)
    if memo is None:
        memo = {}
        try:
            setattr(ctx, '_post_bomb', memo)
        except AttributeError:
            pass
    if key in memo:
        return memo[key]
    df = None
    try:
        ev = ctx.cache('pp_ticks').get('ev', {})
        v = ev.get(key)
        if hasattr(v, 'columns') and len(v):
            df = v.copy()
    except (FileNotFoundError, KeyError, OSError, AttributeError):
        df = None
    if df is None:
        try:
            df = ctx.cache('ev_' + key, dtype={'user_steamid': str}, keep_default_na=False)
        except (FileNotFoundError, KeyError, OSError):
            df = None
    if df is None or not len(df):
        import pandas as pd
        df = pd.DataFrame({'tick': pd.Series(dtype=np.int64), 'user_steamid': pd.Series(dtype=str)})
    df = df.copy()
    df['tick'] = df['tick'].astype(np.int64)
    df['user_steamid'] = df['user_steamid'].map(C._sid_str)
    if 'haskit' in df:
        df['haskit'] = df['haskit'].map(lambda x: str(x).strip().lower() in ('true', '1'))
    df = df.sort_values('tick', kind='mergesort').reset_index(drop=True)
    memo[key] = df
    return df


def _site(ctx, pl, planter, ptk):
    """包点 'A' / 'B'：timeline 的 site 是 'A'/'B' 时直接用；有的 demo 里是包点实体编号（'327'），
    这时看下包人的报点（BombsiteA / BombsiteB）；都不行返回 ''"""
    s = str(pl.get('site') or '')
    if s in ('A', 'B'):
        return s
    for p in (pl.get('planter_place'), C.place_at(ctx, planter, ptk) if planter else None):
        if p in ('BombsiteA', 'BombsiteB'):
            return p[-1]
    return ''


class _Round:
    """一回合用到的东西：阵营、下包（tick / 包点 / 包的 3D 位置）、用户阵亡、拆包事件"""

    def __init__(self, ctx, info):
        self.n = int(info['n'])
        self.start, self.end = int(info['start']), int(info['end'])
        self.u = C.user_sid(ctx)
        self.side = C.user_side(ctx, self.n) or info.get('side')
        self.tl = _timeline_round(ctx, self.n) or {}
        self.mates = C.mates(ctx, self.n)
        self.enemies = C.enemies(ctx, self.n)
        self.udeath = C.death_tick(ctx, self.u, self.n)
        self.plant = None
        pl = self.tl.get('plant')
        if pl and pl.get('tick') is not None:
            planter = C.sid_of(ctx, pl.get('planter'))
            ptk = int(pl['tick'])
            bomb = C.pos(ctx, planter, ptk) if planter else None
            if bomb is not None and np.all(np.isfinite(bomb)):
                self.plant = dict(tick=ptk, site=_site(ctx, pl, planter, ptk), bomb=bomb)
        b = _bomb_ev(ctx, 'bomb_begindefuse')
        self.begin = b[(b['tick'] >= self.start) & (b['tick'] <= self.end + _tr(ctx))]
        d = _bomb_ev(ctx, 'bomb_defused')
        self.defused = d[(d['tick'] >= self.start) & (d['tick'] <= self.end + _tr(ctx))]

    def t(self, ctx, tick):
        return C.t_of(ctx, tick, self.n)


def _states(ctx, R, until):
    """击杀栏上的局面变化：[(tick, 我方, 对方)]，从回合开始，每次阵亡 / 下包之后的人数（C-ALIVE，lag 0：这一刻之后）。
    只列 tick < until 的事件。"""
    ev = [R.start]
    for row in C.deaths(ctx, R.n).itertuples():
        if R.start <= int(row.tick) < until:
            ev.append(int(row.tick))
    if R.plant and R.plant['tick'] < until:
        ev.append(R.plant['tick'])
    out = []
    for tk in sorted(set(ev)):
        us, them = C.alive_counts(ctx, tk + 1, sid=R.u, lag_s=0)
        out.append((tk, us, them))
    return out


def _bomb_left(ctx, R, tick):
    """离爆炸还有几秒"""
    return float(_th(ctx)['bomb_s']) - (int(tick) - R.plant['tick']) / _tr(ctx)


def _dist_bomb(ctx, R, sid, tick):
    return C.dist3(C.pos(ctx, sid, tick), R.plant['bomb'])


def _mutual(ctx, a, b, t0, t1):
    """a、b 在 [t0, t1] 里有一方 spotted 了另一方（POST-02 / POST-G1 的“互相 spotted”，和原型一致）"""
    return C.spots(ctx, a, b, t0, t1) is not None or C.spots(ctx, b, a, t0, t1) is not None


def _exempt(ctx, R):
    """保枪没有经济意义的回合：半场最后一回合、全场最后一回合、对面赛点（加时按 MR3）"""
    th = _th(ctx)
    half, ot = int(th['half_rounds']), int(th['ot_half_rounds'])
    n = R.n
    try:
        us, them = (int(x) for x in str(R.tl.get('score_before', '0:0')).split(':'))
    except ValueError:
        us = them = 0
    more = C.round_info(ctx, n + 1) is not None              # 后面还有回合（打进了加时）
    if n <= 2 * half:
        if n == half:
            return '半场最后一回合'
        if n == 2 * half:
            return '常规时间最后一回合' if more else '最后一回合'
        if them == half:
            return '对面赛点'
        return None
    k = (n - 2 * half - 1) // (2 * ot)                       # 第几个加时（0 起）
    m = n - 2 * half - k * 2 * ot                            # 这个加时里的第几回合（1..6）
    if m == 2 * ot:
        return '这个加时的最后一回合' if more else '最后一回合'
    if m == ot:
        return '加时上半场最后一回合'
    if them == half + ot + k * ot:
        return '对面赛点'
    return None


def _same_side_next(ctx, R):
    """下一回合存在、而且用户还在同一边（换边后“下回合重买”没有意义）"""
    nx = C.round_info(ctx, R.n + 1)
    return nx is not None and C.user_side(ctx, R.n + 1) == R.side


def _half_end(ctx, n):
    """第 n 回合是不是一个半场的最后一回合（上半场、常规时间最后一回合、加时每个半场的最后一回合）"""
    th = _th(ctx)
    half, ot = int(th['half_rounds']), int(th['ot_half_rounds'])
    n = int(n)
    if n <= 2 * half:
        return n in (half, 2 * half)
    m = (n - 2 * half - 1) % (2 * ot) + 1
    return m in (ot, 2 * ot)


def _armor_value(ctx, rnd, sid):
    """这回合身上的甲值（economy.json 的头盔 / 甲）"""
    price = _th(ctx)['price']
    er = C.econ_round(ctx, rnd)
    name = C.name_of(ctx, sid)
    for blk in (('us', 'them') if er else ()):
        for p in er[blk].get('players', []):
            if p.get('name') == name:
                return int(price['helmet']) if p.get('helmet') else (int(price['vest']) if p.get('armor') else 0)
    return 0


def _t_save_value(ctx, R, gun):
    """审核单 20：T 没下包、活到时间结束拿不到败方奖励。返回 dict(net, forgone, gun_price, armor, next_end, worth)。
    worth = 保下来值得（净值 >= save_net_min，并且是 AWP 或者下回合不是半场最后一回合、也确实还有下回合）"""
    th = _th(ctx)
    er = C.econ_round(ctx, R.n) or {}
    forgone = ((er.get('us') or {}).get('if_loss_next') or {}).get('reward')
    forgone = int(forgone) if forgone is not None else int(th['t_forgone_default'])
    gp = int(th['price'].get(C.wkey(gun), 0)) if gun else 0
    arm = _armor_value(ctx, R.n, R.u)
    net = gp + arm - forgone
    has_next = C.round_info(ctx, R.n + 1) is not None
    next_end = (not has_next) or _half_end(ctx, R.n + 1)
    keep = C.wkey(gun) in set(th.get('t_net_keep_awp', ['awp']))
    worth = net >= int(th['save_net_min']) and (keep or not next_end)
    return dict(net=net, forgone=forgone, gun_price=gp, armor=arm, next_end=bool(next_end), worth=bool(worth))


def _retreat(ctx, R):
    """CT 保枪往哪边撤：'B 一侧' / 'A 一侧'；包点不明时返回 None"""
    if not R.plant:
        return None
    return (_th(ctx).get('save_retreat') or {}).get(R.plant['site'])


def _site_cn(R):
    return f"{R.plant['site']} 点" if R.plant and R.plant['site'] in ('A', 'B') else '包点'


def _retake_intent(ctx, R, ud):
    """审核单 21：CT 下包后“去回防”按意图认（往包点走 / 扔回防烟或闪 / 一直往包走）。
    返回 dict(kind='shrink'|'nade'|'toward', nade='烟'|'闪', nade_tick, nade_place) 或 None。"""
    th = _th(ctx)
    u = R.u
    pt = R.plant['tick']
    a = max(pt - _s2t(ctx, th['retake_pre_s']), R.start)
    d_end = _dist_bomb(ctx, R, u, ud - 1)
    # ② 下包后扔的烟 / 闪落在包附近（先看道具：句子里写“扔了回防烟”）
    lim = float(th['retake_nade_3d'])
    nades = []
    for s in C.smokes(ctx, R.n):
        if s['by'] == u and pt <= int(s['t0']) < ud:
            nades.append((int(s['t0']), '烟', (s['x'], s['y'], s['z'])))
    fl = _bomb_ev(ctx, 'flashbang_detonate')
    if len(fl) and all(c in fl for c in ('x', 'y', 'z')):
        for r in fl[(fl['user_steamid'] == u) & (fl['tick'] >= pt) & (fl['tick'] < ud)].itertuples():
            nades.append((int(r.tick), '闪', (float(r.x), float(r.y), float(r.z))))
    for tk, nm, xyz in sorted(nades):
        if C.dist3(xyz, R.plant['bomb']) <= lim:
            return dict(kind='nade', nade=nm, nade_tick=tk, nade_place=C.place_cn(C.nearest_place(ctx, xyz, R.n), default=''))
    # ① 从下包前几秒起离包缩短够多，而且下包后还在缩短
    if (_dist_bomb(ctx, R, u, a) - d_end >= float(th['save_went_shrink'])
            and _dist_bomb(ctx, R, u, max(pt, a)) - d_end >= float(th['went_post_min'])):
        return dict(kind='shrink')
    # ③ 一段时间一直往包走，这段在下包后结束
    step = max(1, _s2t(ctx, th['toward_step_s']))
    need = float(th['toward_s'])
    smin = float(th['toward_step_min'])
    tks = list(range(a, ud, step))
    d = [_dist_bomb(ctx, R, u, tk) for tk in tks]
    run0 = 0
    for i in range(1, len(tks)):
        if d[i - 1] - d[i] >= smin:
            if (tks[i] - tks[run0]) / _tr(ctx) >= need and tks[i] > pt:
                return dict(kind='toward')
        else:
            run0 = i
    return None


def _known_angle(ctx, R, killer, ud, fight=None):
    """凶手在打死你之前刚在同一个位置“响过枪”：known_kill_s 内打死过队友（那次击杀前 known_radar_s 内上过我方雷达），
    或开过你听得到的枪（对枪开始之前）。返回 dict(how='kill'|'shot', tick, mate, place, ago_s) 或 None"""
    th = _th(ctx)
    u = R.u
    if killer is None:
        return None
    w0 = ud - _s2t(ctx, th['known_kill_s'])
    kpos = C.pos(ctx, killer, ud)
    same = float(th['angle_same_3d'])
    dr = C.deaths(ctx, R.n)
    pk = dr[(dr['att'] == killer) & dr['vic'].isin(R.mates) & (dr['tick'] < ud) & (dr['tick'] >= w0)]
    for row in pk.sort_values('tick', ascending=False, kind='mergesort').itertuples():
        ktk = int(row.tick)
        if (C.on_radar(ctx, killer, ktk - _s2t(ctx, th['known_radar_s']), ktk, team_sid=u)
                and C.dist3(C.pos(ctx, killer, ktk), kpos) <= same):
            return dict(how='kill', tick=ktk, mate=C.name_of(ctx, row.vic), place=_place(ctx, killer, ktk),
                        ago_s=C.sec((ud - ktk) / _tr(ctx)))
    st = int(fight['start_tick']) if fight and fight.get('start_tick') is not None else ud - _s2t(ctx, th['fight_match_s'])
    if st - 1 > w0:
        hs = C.heard_shots(ctx, u, w0, st - 1, shooters=[killer])
        for r in hs.sort_values('tick', ascending=False, kind='mergesort').itertuples():
            if C.dist3((r.user_X, r.user_Y, r.user_Z), kpos) <= same:
                tk = int(r.tick)
                return dict(how='shot', tick=tk, mate=None, place=_place(ctx, killer, tk),
                            ago_s=C.sec((ud - tk) / _tr(ctx)))
    return None


def _user_fight(R, death_tick, tol_ticks):
    """timeline.json 里结束于这次阵亡的那次对枪（engage = 拉枪 / 架枪 / 双方都在动 / 双方都没动）"""
    best = None
    for f in R.tl.get('user_fights') or []:
        et = f.get('end_tick')
        if et is None or f.get('result') != '输':
            continue
        if abs(int(et) - int(death_tick)) <= tol_ticks and (best is None or abs(int(et) - death_tick) < abs(int(best['end_tick']) - death_tick)):
            best = f
    return best


def _tidy(ctx, s):
    """句子排版：common.tidy（人名整体保护）"""
    return C.tidy(s, C.player_names(ctx))


def _hit(ctx, R, rule, kind, tick, scope, blame, facts, text, alt, evidence, kb=(), death_tick=None, merge_key=None):
    """按 CONTRACT 组装一条 Hit"""
    u = R.u
    t = R.t(ctx, tick)
    facts = dict(sorted(facts.items()))
    return {
        'rule': rule, 'kind': kind, 'round': R.n, 't': C.sec(t), 'tick': int(tick), 'scope': scope,
        'subject': C.name_of(ctx, u), 'is_user': True, 'blame': blame,
        'death_t': C.sec(R.t(ctx, death_tick)) if death_tick is not None else None,
        'death_of': C.name_of(ctx, u) if death_tick is not None else None,
        'merge_key': merge_key, 'facts': facts, 'text': _tidy(ctx, text), 'alt': _tidy(ctx, alt) if alt else '',
        'kb': list(kb), 'evidence': _tidy(ctx, evidence),
    }


# ================================================================ POST-01 人数劣势该保枪没保
def _hopeless(ctx, R, tick, us, them):
    """打不赢的局面（rules_v1 POST-01）；返回这一局面真正成立的 tick（T 方要等到回合只剩 30 秒），不成立返回 None"""
    th = _th(ctx)
    if us < 1:
        return None
    planted = R.plant is not None and R.plant['tick'] <= tick
    if R.side == 'CT':
        if planted and us <= them - int(th['ct_planted_down']):
            return tick
        if not planted and us == 1 and them >= int(th['ct_solo_vs']):
            return tick
        return None
    if R.side == 'T' and not planted and us <= them - int(th['t_down']):
        return max(tick, C.tick_of(ctx, R.n, float(th['round_s']) - float(th['t_left_s'])))
    return None


def _situ(ctx, R, tick, us, them):
    """局面描述：'1v3、包还没下' / '2v4、包已经下了' / T 没下包时加 '、回合只剩 N 秒'"""
    planted = R.plant is not None and R.plant['tick'] <= tick
    s = f"{C.alive_str(us, them)}、包{'已经下了' if planted else '还没下'}"
    left = None
    if R.side == 'T' and not planted:
        left = max(0, _int(float(_th(ctx)['round_s']) - R.t(ctx, tick)))
        s += f'、回合只剩 {left} 秒'
    return s, planted, left


def _save_idea(ctx, R, gun_cn):
    """CT 下包后的保枪思路（Q12 / 审核单 21）：往远离包点的一侧撤"""
    side = _retreat(ctx, R)
    return (f'还可以往{side}撤，保住 {gun_cn}，下回合不用重买' if side
            else f'还可以往远离包点的一侧撤，保住 {gun_cn}，下回合不用重买')


def _idea_hit(ctx, R, tick, facts, text, alt, evidence, death_tick):
    """“另一种思路”：不记责任，只进 ideas（merge 把 kind=思路 只放进 ideas；branch=保枪 / blame — 不出标签、不当主因）"""
    return _hit(ctx, R, 'POST-01', '思路', tick, 'moment', '—', dict(facts, branch='保枪'), text, alt, evidence,
                death_tick=death_tick)


def _post01(ctx, R):
    """返回 (hit 列表, 是否判了“问题”)"""
    th = _th(ctx)
    u = R.u
    end = R.udeath if R.udeath is not None else R.end + 1
    st = _states(ctx, R, end)
    ts = None
    for i, (tk, us, them) in enumerate(st):
        nxt = st[i + 1][0] if i + 1 < len(st) else end
        h = _hopeless(ctx, R, tk, us, them)
        if h is not None and h < min(nxt, end) and h < end:
            ts, s_us, s_them, s_ev = h, us, them, tk
            prev_n = (st[i - 1][1], st[i - 1][2]) if i > 0 else None
            break
    if ts is None:
        return [], False
    inv = _inventory(ctx, u, ts)
    gun = _primary(inv)
    eq = _equip(ctx, R.n, u, ts)
    if eq < int(th['save_equip']) or gun is None:
        return [], False
    gun_cn = C.weapon_cn(gun)
    t_s = R.t(ctx, ts)
    situ, planted_s, left_s = _situ(ctx, R, ts, s_us, s_them)
    # 这一局面是谁造成的（证据句用）：某人阵亡 / 下包 / T 方时间到
    ev_who = None
    if s_ev == ts:
        if R.plant and R.plant['tick'] == ts:
            ev_who = '下包'
        else:
            dd = C.deaths(ctx, R.n)
            dd = dd[dd['tick'] == ts]
            if len(dd):
                ev_who = C.name_of(ctx, sorted(dd['vic'])[0])
    # alive_mode = lag0：句子里的人数是“这次阵亡 / 下包之后那一刻”的击杀栏人数（alive_counts(tick + 1, lag_s=0)），
    # 不是默认的晚 0.25 秒口径（R12：队友D 58.3 倒下、58.4 下包，下包那一刻是 2v4）
    facts = dict(t_s=C.sec(t_s), us=s_us, them=s_them, gun=gun_cn, equip=eq, planted=planted_s, situ=situ,
                 event=ev_who, round_left_s=left_s, alive_mode='lag0')
    # 这一刻只是下了包、人数没变（128 R17：12.7 秒就是 3v5，41.9 秒下包）：写“包下了”，不写“变成 3v5”
    plant_only = ev_who == '下包' and prev_n == (s_us, s_them)
    became = (f'第 {C.fmt_sec(t_s)} 秒包下了，{C.alive_str(s_us, s_them)}' if plant_only else
              f'第 {C.fmt_sec(t_s)} 秒变成 {situ}')
    facts['plant_only'] = plant_only
    ev_txt = ((became if plant_only else
               f'第 {C.fmt_sec(t_s)} 秒' + (f'{ev_who}倒下后' if ev_who and ev_who != '下包' else ('下包后' if ev_who == '下包' else ''))
               + f'变成 {situ}') + f'，你身上还有 {gun_cn}')
    why = _exempt(ctx, R)
    if why:
        facts.update(exempt=why, background=True)
        text = f'{became}，但这是{why}，枪留到下回合没用，去打是对的'
        return [_hit(ctx, R, 'POST-01', '判定', ts, 'moment', '合理代价', facts, text, '', ev_txt + f'；这是{why}',
                     death_tick=R.udeath)], False
    # 审核单 20：T 没下包、活到时间结束拿不到败方奖励——先扣掉这份钱，保下来不值（或者下回合就是半场最后一回合、
    # 不是 AWP）时不判，也不夸“保下了枪”（bd R11：保 AK 拿不到 $2900，而且下回合是半场最后一回合）
    tval = None
    if R.side == 'T':
        tval = _t_save_value(ctx, R, gun)
        facts.update(t_save_net=tval['net'], forgone_bonus=tval['forgone'], t_next_half_end=tval['next_end'])
        if not tval['worth']:
            return [], False
    if R.udeath is None:
        won = R.tl.get('winner') == R.side
        kd = C.deaths(ctx, R.n)
        kills_after = kd[(kd['att'] == u) & (kd['tick'] > ts) & kd['vic'].isin(R.enemies)]
        if won:
            return [], False
        lead, t_hit = became, ts
        if len(kills_after):
            # 之后打掉了人、但打完还是打不赢的局面，没再硬打、活到回合结束（bd R17：54.4 秒拿下一个后 1v3，AWP 带进下回合）
            lk = int(kills_after['tick'].max())
            a_us, a_them = C.alive_counts(ctx, lk + 1, sid=u, lag_s=0)
            if _hopeless(ctx, R, lk + 1, a_us, a_them) is None:
                return [], False
            vn = '、'.join(C.name_of(ctx, v) for v in kills_after['vic'])
            lead = f'第 {C.fmt_sec(R.t(ctx, lk))} 秒打掉 {vn} 后还是 {C.alive_str(a_us, a_them)}'
            facts.update(kill_t=C.sec(R.t(ctx, lk)), after_kill=C.alive_str(a_us, a_them), us_after=a_us, them_after=a_them)
            t_hit = lk
        facts.update(saved=True)
        text = f'{lead}，你没有硬打，{gun_cn} 保了下来'
        # 为什么好只写理由（原来 alt 里带着模板标签“好的操作配：”，上了页面，128 R17）；
        # T 方不写“钱可以留给队友”（审核单 20：T 时间到活下来拿不到败方奖励）
        facts.update(why_good=('这把枪比放弃的败方奖励值钱，下回合不用重买' if R.side == 'T'
                               else '下回合不用重买，钱可以留给队友'))
        return [_hit(ctx, R, 'POST-01', '好', t_hit, 'moment', '—', facts, text, '',
                     ev_txt + '；你活到回合结束，枪保了下来')], False
    # 阵亡：阵亡那一刻还在打不赢的局面里才算
    ud = R.udeath
    d_us, d_them = C.alive_at_death(ctx, u, R.n)         # C-ALIVE 默认口径（阵亡那一刻）
    if _hopeless(ctx, R, ud, d_us, d_them) is None:
        return [], False
    t_die = R.t(ctx, ud)
    situ_d, planted_d, left_d = _situ(ctx, R, ud, d_us, d_them)
    dr = C.deaths(ctx, R.n)
    drow = dr[(dr['tick'] == ud) & (dr['vic'] == u)]
    killer = str(drow.iloc[0]['att']) if len(drow) else None
    if killer not in R.enemies:
        return [], False                                   # 被炸死 / 摔死 / 队友误伤：不判保枪
    kname = C.name_of(ctx, killer)
    fight = _user_fight(R, ud, _s2t(ctx, th['fight_match_s']))
    peeked = bool(fight and fight.get('engage') == '拉枪')
    ct_post = R.side == 'CT' and R.plant is not None and planted_d
    # 审核单 21：CT 下包后“去回防”按意图认（往包点走 / 扔回防烟或闪 / 一直往包走），不只看下包后走近了多少
    intent = _retake_intent(ctx, R, ud) if ct_post else None
    went = intent is not None
    # 句子里的人数是阵亡那一刻的（us / them）；变成打不赢那一刻的人数改记在 us_s / them_s
    facts.update(us=d_us, them=d_them, us_s=s_us, them_s=s_them, alive_mode=None, situ_d=situ_d,
                 death_t=C.sec(t_die), killer=kname, peeked=peeked, went=went, engage=(fight or {}).get('engage'),
                 round_left_d=left_d, retake_intent=(intent or {}).get('kind'))
    ev_txt = (f'第 {C.fmt_sec(t_s)} 秒' + (f'{ev_who}倒下后' if ev_who and ev_who != '下包' else ('下包后' if ev_who == '下包' else ''))
              + (f'回合只剩 {left_s} 秒，' if left_s is not None and not ev_who else '')
              + f'局面已经打不赢，你身上还有 {gun_cn}')
    kplace = _place(ctx, killer, ud) if killer else None
    facts['killer_place'] = kplace
    # 凶手刚在同一个位置打死过队友 / 开过你听得到的枪（不写隔了几秒）
    ka = _known_angle(ctx, R, killer, ud, fight)
    known = ''
    if ka:
        kp = ka['place']
        if ka['how'] == 'kill':
            known = f"{kname} 刚在{kp or ''}打死 {ka['mate']}，"
        else:
            known = f"{kname} 刚在{kp or ''}开过枪，"
        facts.update(known_how=ka['how'], known_mate=ka['mate'], known_ago_s=ka['ago_s'], known_place=kp,
                     known_kill_t=C.sec(R.t(ctx, ka['tick'])))
    # 这一段你一枪没开：不写“和对面打”（e6b R16：整回合没开枪，后半句又说一直没交过火）
    _fu = C.fires(ctx, R.n, sid=u)
    shot_any = bool(((_fu['tick'] >= ts) & (_fu['tick'] <= ud)).any())
    fight_w = '和对面打' if shot_any else ''
    td0 = max([ts] + [tk for tk, _, _ in st if tk < ud])
    t_d0 = R.t(ctx, td0)
    facts.update(situ_d_t=C.sec(t_d0), shot_any=bool(shot_any))
    # Q11：CT 包没下 1vN 拉出去——职业这种局面也只有少数人保枪，真正的问题是去拉刚响过枪的角度：
    # 记小问题，保枪写成还可以；不是刚响过枪的角度就不判（8d R10 52.9）
    if R.side == 'CT' and not planted_d and peeked:
        if not ka:
            return [], False
        what = f"刚在{ka['place'] or ''}打死 {ka['mate']}" if ka['how'] == 'kill' else f"刚在{ka['place'] or ''}开过枪"
        text = (f'{situ_d}，{kname} {what}，你去拉这个刚响过枪的角度，被他打死，丢了 {gun_cn}')
        alt = f'还可以藏好把 {gun_cn} 保下来，下回合不用重买'
        evid = (f"第 {C.fmt_sec(R.t(ctx, ka['tick']))} 秒 {kname} 在{ka['place'] or '那里'}"
                + (f"打死 {ka['mate']}" if ka['how'] == 'kill' else '开枪')
                + f'；第 {C.fmt_sec(t_die)} 秒你从同一条线拉出去，被他打死')
        facts.update(branch='拉响枪角度', main_cause='去拉刚响过枪的角度')
        return [_hit(ctx, R, 'POST-01', '问题', ud, 'moment', '小问题', dict(facts, override_duel=True), text, alt,
                     evid, death_tick=ud)], True
    if peeked or went:
        site_cn = _site_cn(R)
        if peeked:
            how, how_ev = '拉出去', '拉出去'
        elif intent['kind'] == 'nade':
            how = f"扔了回防{intent['nade']}还往{site_cn}走"
            how_ev = f'往{site_cn}走'
            facts.update(retake_nade=intent['nade'], retake_nade_t=C.sec(R.t(ctx, intent['nade_tick'])),
                         retake_nade_place=intent['nade_place'] or None)
        else:
            how = how_ev = f'往{site_cn}走'
        lead_you = '你' if how.startswith('扔了') else '你还'
        # 前半句已经写了凶手在哪：不再重复“被 B 点的 X”（635 高分玩家50 R2）
        by = '被他打死' if known and ka.get('place') and ka['place'] == kplace else f'被{_at(kplace)}{kname} 打死'
        # 不写记账的话（合理代价 / 算你的），只说这时候怎么更值（8d R10）
        text = (f'{situ_d}，{known}{lead_you}{how}{fight_w}，{by}，丢了 {gun_cn}。'
                f'这时候很难赢下来，把 {gun_cn} 保下来更值')
        # 还可以：只写当时就知道的。CT 下包后：往远离包点的一侧撤（审核单 21）；T：时间到活下来拿不到败方奖励，
        # 不写“省下的钱给队友发枪”（审核单 20）
        if R.side == 'T':
            alt = (f'还可以退到远离对面的地方藏好保住 {gun_cn}；时间到活下来拿不到 {C.money(tval["forgone"])} 败方奖励，'
                   f'{gun_cn} 还是比这份奖励值钱')
        elif ct_post:
            alt = _save_idea(ctx, R, gun_cn)
        else:
            alt = f'还可以藏好把 {gun_cn} 保下来，下回合不用重买'
        # 证据写阵亡前最后一次局面变化（句子里的人数），不是更早“打不赢”的那一刻（e6b R16：2v4 是 28.8 秒，1v3 是 44.7 秒）
        ev_d = (f'第 {C.fmt_sec(t_d0)} 秒变成 {situ_d}，你身上还有 {gun_cn}' if td0 > ts else ev_txt)
        if facts.get('retake_nade'):
            ev_d += (f"；第 {facts['retake_nade_t']} 秒你往" + (facts['retake_nade_place'] or '包点')
                     + f"扔了{facts['retake_nade']}")
        evid = ev_d + f'；第 {C.fmt_sec(t_die)} 秒你{how_ev}被 {kname} 打死'
        return [_hit(ctx, R, 'POST-01', '问题', ud, 'moment', '你的责任', dict(facts, override_duel=True), text, alt, evid,
                     death_tick=ud)], True
    # “往后撤想保枪”只在你真的离包越来越远时写
    away = False
    if ct_post:
        t0 = max(ts, R.plant['tick'])
        away = _dist_bomb(ctx, R, u, ud - 1) - _dist_bomb(ctx, R, u, t0) >= float(th['save_away_grow'])
    facts['away'] = away
    alt = ''
    # 复核修正（Q12）：对枪记录是“架枪”，但阵亡前 wait_move_s 秒里你自己挪过一段（51920 R18：在梯子间等了一阵，走进 VIP
    # 时被打死）——不是“没有出去找人、被找到”，写成“先在 X 等着，后来往 Y 挪时被打死”
    w0 = ud - _s2t(ctx, th['wait_move_s'])
    moved_late = C.path_len(ctx, u, w0, ud) >= float(th['wait_move'])
    if facts['engage'] in ('架枪', '双方都没动') and moved_late:
        p0, p1 = C.place_cn_at(ctx, u, w0), C.place_cn_at(ctx, u, ud - 1)
        text = (f'{situ_d}，你先在{p0}等着，后来往{p1}挪时被 {kname} 打死' if p0 != p1 and '某处' not in (p0, p1)
                else f'{situ_d}，你等了一阵后挪位置，被 {kname} 打死')
        facts['moved_late'] = True
        if ct_post:
            alt = _save_idea(ctx, R, gun_cn)
    elif facts['engage'] in ('架枪', '双方都没动'):
        text = f'{situ_d}，你没有出去找人，被 {kname} 找到打死'      # 原地没动，不能说成“往后撤”
        # Q12：下包后等着被对面找到，合理代价；往远离包点的一侧撤作另一种思路（第 5 场 R18）
        if ct_post:
            alt = _save_idea(ctx, R, gun_cn)
    elif away or not ct_post:
        text = f'{situ_d}，你在往后撤想保枪，被 {kname} 追上打死'
    else:
        # 没有去回防的意图（没往包点走、没扔回防道具），也没往远处撤（51920faa R17 以前的写法“还没走到包点”）
        text = f'{situ_d}，你离包还远，在{kplace or "半路"}和 {kname} 碰上，被他打死'
        alt = _save_idea(ctx, R, gun_cn)
    evid = ev_txt + (f'；第 {C.fmt_sec(t_die)} 秒你挪位置时被 {kname} 打死' if facts.get('moved_late') else
                     f'；第 {C.fmt_sec(t_die)} 秒你没有拉出去，也没有往包点走，被 {kname} 打死')
    if '你离包还远' in text:
        evid = ev_txt + f'；第 {C.fmt_sec(t_die)} 秒你离包还远，被{_at(kplace)}{kname} 打死'
    out = [_hit(ctx, R, 'POST-01', '判定', ud, 'moment', '合理代价', facts, text, alt, evid, death_tick=ud)]
    if alt:
        # 合理代价的 POST-01 只当背景（merge.is_post01_background），被别的主因盖住时它的 alt 不进 ideas：
        # 另出一条“思路”，保证“往远离包点一侧撤”能进 ideas（和时刻里写过的同一句 merge 会去重）
        out.append(_idea_hit(ctx, R, ud, dict(facts, idea='撤离保枪'), text, alt, evid, ud))
    return out, False


def _post01_far(ctx, R, h02):
    """审核单 21：CT 下包后少 1 人、离包远、身上有长枪，还去回防被打死：只写“离包远时也可以保枪”（思路，不记你的责任）。
    同一次阵亡判了 POST-02（一个人先进，还可以等队友一起进）时不出，免得两条还可以方向相反。"""
    th = _th(ctx)
    u = R.u
    if R.side != 'CT' or not R.plant or R.udeath is None or R.udeath <= R.plant['tick'] or h02 is not None:
        return []
    if _exempt(ctx, R):
        return []
    ud, pt = R.udeath, R.plant['tick']
    # 下包以后出现过的局面：下包那一刻生效的那个 + 下包后到阵亡前每一次变化；取第一次“少 1 人”
    st = _states(ctx, R, ud)
    after = [(pt, us, them) for tk, us, them in st if tk <= pt][-1:] + [s for s in st if pt < s[0] < ud]
    cur = next((s for s in after if s[1] >= 1 and s[1] == s[2] - 1), None)
    d_us, d_them = C.alive_at_death(ctx, u, R.n)
    if cur is None or d_us != d_them - 1:
        return []
    t1, us, them = cur
    if _dist_bomb(ctx, R, u, t1) < float(th['far_bomb_3d']):
        return []
    gun = _primary(_inventory(ctx, u, t1))
    if gun is None or _equip(ctx, R.n, u, t1) < int(th['save_equip']):
        return []
    intent = _retake_intent(ctx, R, ud)
    if intent is None:
        return []
    dr = C.deaths(ctx, R.n)
    drow = dr[(dr['tick'] == ud) & (dr['vic'] == u)]
    killer = str(drow.iloc[0]['att']) if len(drow) else None
    if killer not in R.enemies:
        return []
    kname = C.name_of(ctx, killer)
    gun_cn = C.weapon_cn(gun)
    kplace = _place(ctx, killer, ud)
    site_cn = _site_cn(R)
    t_1, t_die = R.t(ctx, t1), R.t(ctx, ud)
    facts = dict(us=us, them=them, t1=C.sec(t_1), death_t=C.sec(t_die), killer=kname, killer_place=kplace, gun=gun_cn,
                 retake_intent=intent['kind'], site=R.plant['site'], alive_mode='lag0', idea='离包远保枪')
    text = f'下包后 {C.alive_str(us, them)}，你离包还远，往{site_cn}去回防，被{_at(kplace)}{kname} 打死，丢了 {gun_cn}'
    side = _retreat(ctx, R)
    alt = (f'还可以趁离包还远往{side}撤，保住 {gun_cn}' if side else f'还可以趁离包还远往远离包点的一侧撤，保住 {gun_cn}')
    evid = f'第 {C.fmt_sec(t_1)} 秒下包后是 {C.alive_str(us, them)}，你离包还远；第 {C.fmt_sec(t_die)} 秒你往{site_cn}走被 {kname} 打死'
    return [_idea_hit(ctx, R, ud, facts, text, alt, evid, ud)]


# ================================================================ POST-02 回防一个人先进
def _post02(ctx, R, p01_problem):
    th = _th(ctx)
    u = R.u
    if R.side != 'CT' or not R.plant or R.udeath is None or R.udeath <= R.plant['tick'] or p01_problem:
        return None
    ud = R.udeath
    d_us, _ = C.alive_counts(ctx, ud, sid=u, lag_s=0)
    if d_us < 2:
        return None
    pt = R.plant['tick']
    t0 = max(ud - _s2t(ctx, th['tc_look_s']), pt)
    # 开打 tc：第一次被敌人 spotted / 第一次开枪 / 第一次被敌人打中（伤害 > 0）
    cands = []
    for e in R.enemies:
        tk = C.spots(ctx, e, u, t0, ud)
        if tk is not None:
            cands.append(tk)
    fr = C.fires(ctx, R.n, sid=u)
    fr = fr[(fr['tick'] >= t0) & (fr['tick'] <= ud)]
    if len(fr):
        cands.append(int(fr['tick'].iloc[0]))
    hu = C.hurts(ctx, R.n)
    hu = hu[(hu['vic'] == u) & hu['att'].isin(R.enemies) & (hu['dmg'] > 0) & (hu['tick'] >= t0) & (hu['tick'] <= ud)]
    if len(hu):
        cands.append(int(hu['tick'].iloc[0]))
    tc = min(cands) if cands else ud
    left = _bomb_left(ctx, R, tc)
    # (a) 在往包点走
    d_die = _dist_bomb(ctx, R, u, ud - 1)
    pa = max(tc - _s2t(ctx, th['went_pre_s']), pt)
    pb = min(tc + _s2t(ctx, th['went_post_s']), ud - 1)
    went = (d_die <= float(th['near_bomb_3d'])
            or _dist_bomb(ctx, R, u, pt) - d_die >= float(th['went_shrink'])
            or _dist_bomb(ctx, R, u, pa) - _dist_bomb(ctx, R, u, pb) >= float(th['went_step']))
    # (b) 时间还够
    if not went or left < float(th['wait_left_s']):
        return None
    # (c) 没有队友一起进
    dr = C.deaths(ctx, R.n)
    drow = dr[(dr['tick'] == ud) & (dr['vic'] == u)]
    killer = str(drow.iloc[0]['att']) if len(drow) else None
    if killer not in R.enemies:
        return None
    opp = {killer}
    for e in R.enemies:
        if C.spots(ctx, e, u, tc, ud) is not None:
            opp.add(e)
    opp = sorted(opp)
    me = C.pos(ctx, u, tc)
    live = [m for m in R.mates if C.alive(ctx, m, tc, R.n)]
    near = sorted((C.dist3(me, C.pos(ctx, m, tc)), m) for m in live)
    w0, w1 = tc - _s2t(ctx, th['together_pre_s']), ud + _s2t(ctx, th['together_post_s'])
    together = [m for dm, m in near if dm <= float(th['together_3d']) and any(_mutual(ctx, m, e, w0, w1) for e in opp)]
    if together:
        return None
    kname = C.name_of(ctx, killer) if killer else None
    # 开打那一刻的人数：tc 当时（含）之前倒下的都算（bd R13：队友A 39.7 秒刚在你旁边倒下，tc 39.9 秒已经是 2v2，不是 3v2）
    us_c, them_c = C.alive_counts(ctx, tc + 1, sid=u, lag_s=0)
    t_c, t_die = R.t(ctx, tc), R.t(ctx, ud)
    left_i = _int(left)
    facts = dict(tc=C.sec(t_c), death_t=C.sec(t_die), us=us_c, them=them_c, bomb_left_s=left_i, killer=kname,
                 opp=[C.name_of(ctx, e) for e in opp], site=R.plant['site'])
    # 附加：tc 前 3 秒队友在你旁边被跟你交火的人打死
    mate_a = ''
    md = dr[dr['vic'].isin(R.mates) & dr['att'].isin(opp) & (dr['tick'] >= tc - _s2t(ctx, th['mate_died_s'])) & (dr['tick'] <= tc)]
    for row in md.sort_values('tick', ascending=False, kind='mergesort').itertuples():
        if C.dist3(C.pos(ctx, u, int(row.tick)), C.pos(ctx, row.vic, int(row.tick) - 1)) <= float(th['mate_died_3d']):
            a_name, a_k = C.name_of(ctx, row.vic), C.name_of(ctx, row.att)
            mate_a = f'{a_name} 刚在你旁边被 {a_k} 打死，'
            facts.update(mate_died=a_name, mate_died_by=a_k, mate_died_t=C.sec(R.t(ctx, int(row.tick))))
            break
    mate_b = ''
    ahead = []
    if near:
        dm, m = near[0]
        mb = C.name_of(ctx, m)
        # 离包比你还近的队友已经在包点那边（另一个方向），不是“还在后面”（e6b R17：高分玩家33、高分玩家79 已经在 A 点包旁边）
        d_me = _dist_bomb(ctx, R, u, tc)
        ahead = [x for _, x in near if _dist_bomb(ctx, R, x, tc) < d_me
                 and _dist_bomb(ctx, R, x, tc) <= float(th['near_bomb_3d']) * th['ahead_bomb_x']]
        if ahead:
            an = '、'.join(C.name_of(ctx, x) for x in ahead)
            mate_b = f'{an} 已经从另一边到了包点附近，'
            facts.update(mates_ahead=[C.name_of(ctx, x) for x in ahead])
        else:
            mate_b = f"{mb}{'就在身后几步' if dm <= float(th['near_mate_3d']) else '还在后面'}，"
        facts.update(near_mate=mb, near_mate_close=bool(dm <= float(th['near_mate_3d'])))
    hp_c = C.hp(ctx, u, tc)
    half = hp_c <= float(th['half_hp'])
    facts['half_hp'] = bool(half)
    inv = _inventory(ctx, u, ud - 1)
    gun = _primary(inv)
    hand = C.wkey(C.weapon_at(ctx, u, ud - 1))
    pistol_switch = hand is not None and C.gun_tier(hand) == 0 and gun is not None
    nades = _nades(inv)
    facts.update(gun=C.weapon_cn(gun) if gun else None, in_hand=C.weapon_cn(hand) if hand else None,
                 pistol_switch=pistol_switch, unused_nades=nades)
    site_cn = f"{R.plant['site']} 点" if R.plant['site'] in ('A', 'B') else '包点'
    dplace = _place(ctx, u, ud - 1)
    facts['death_place'] = dplace
    text = (f'下包后 {C.alive_str(us_c, them_c)}，离爆炸还有 {left_i} 秒，{mate_a}{mate_b}'
            + ('你只剩半血，' if half else '') + (f'你一个人从这边往{site_cn}走，' if ahead else f'你一个人先往{site_cn}走，')
            + (f'在{dplace}' if dplace else '') + f'被 {kname} 打死')
    # 你的部分太长时用的短句：只留一个人先进、在哪被谁打死（8d R12）
    facts['short'] = (f'下包后 {C.alive_str(us_c, them_c)}，' + ('你一个人从这边往' if ahead else '你一个人先往') + f'{site_cn}走，'
                      + (f'在{dplace}' if dplace else '') + f'被 {kname} 打死')
    if pistol_switch:
        text += f'；手里拿的是 {C.weapon_cn(hand)}（{C.weapon_cn(gun)} 还在身上）'
    if nades:
        text += f"{'，' if pistol_switch else '；'}{'、'.join(nades)}都没用"
    # 附加：你倒下后同一个凶手在同一个地方又打死一名队友（原 RETAKE-02 用户先死的情况）
    chain = None
    if killer:
        nx = dr[(dr['att'] == killer) & dr['vic'].isin(R.mates) & (dr['tick'] > ud) & (dr['tick'] <= ud + _s2t(ctx, th['after_s']))]
        pd_ = C.pos(ctx, u, ud - 1)
        for row in nx.itertuples():
            if C.dist3(C.pos(ctx, row.vic, int(row.tick) - 1), pd_) <= float(th['after_3d']):
                gap = C.sec((int(row.tick) - ud) / _tr(ctx))
                nm = C.name_of(ctx, row.vic)
                # 不写隔了几秒（用户写作规则：不写延迟秒数）
                text += f'；你倒下后 {nm} 也从同一个地方过去，被 {kname} 打死'
                facts.update(after_mate=nm, after_gap_s=gap, after_t=C.sec(R.t(ctx, int(row.tick))))
                chain = f'chain:{R.n}:{killer}'
                break
    if facts.get('mate_died') and facts.get('mate_died_by') == kname:
        chain = f'chain:{R.n}:{killer}'
    if us_c <= them_c - 2:
        blame = '合理代价'
        facts['your_part'] = '小问题'
        text += f'。{C.alive_str(us_c, them_c)} 回防很难赢下来，你先进去，身后的人补不上'     # 审核单第 36 条
    else:
        blame = '你的责任'
    # 还可以
    mate_w = facts.get('near_mate') or '队友'
    # 句子里只出现一次“还可以”（页面自己加“还可以：”，128 / e6b R17“还可以：时间还够，还可以等……”）
    if ahead:
        alt = f"还可以趁时间还够，等两边都到位，{'先扔闪再和' if '闪' in nades else '和'} {'、'.join(C.name_of(ctx, x) for x in ahead)} 同时进"
    else:
        alt = f"还可以趁时间还够，等 {mate_w} 到身边，{'先扔闪再一起进' if '闪' in nades else '再一起进'}"
    kb = []
    # “还可以”只给一个思路；身上没有闪时不写“先撇闪”（e6b R17：唯一一颗闪 42 秒就扔了）
    if R.plant['site'] == 'B':
        kb = []
    elif R.plant['site'] == 'A':
        kb = []
    evid = (f'第 {C.fmt_sec(t_c)} 秒开始交火时离爆炸还有 {left_i} 秒，' + ('包点那边的队友' if ahead else '身边的队友')
            + '没有和对面互相看见；'
            f'第 {C.fmt_sec(t_die)} 秒你被 {kname} 打死')
    return _hit(ctx, R, 'POST-02', '问题', tc, 'moment', blame, facts, text, alt, evid, kb=kb, death_tick=ud,
                merge_key=chain)


# ================================================================ POST-03 人多守包时走出去找人
def _angdiff(a, b):
    return abs((a - b + 180) % 360 - 180)


def _bearing(p, q):
    return math.degrees(math.atan2(q[1] - p[1], q[0] - p[0]))


def _post03(ctx, R):
    th = _th(ctx)
    u = R.u
    if R.side != 'T' or not R.plant or R.udeath is None or R.udeath <= R.plant['tick']:
        return None
    ud, pt = R.udeath, R.plant['tick']
    step = int(th['frame_step_ticks'])
    tks = list(range(pt, ud, step))
    if len(tks) < 2:
        return None
    P = np.array([C.pos(ctx, u, tk) for tk in tks])
    ok = np.all(np.isfinite(P), axis=1)
    tks = [tk for tk, o in zip(tks, ok) if o]
    P = P[ok]
    n = len(tks)
    if n < 2:
        return None
    dt = step / _tr(ctx)
    r, dz, hold_s = float(th['hold_r']), float(th['hold_dz']), float(th['hold_s'])

    def member(c):
        return (np.hypot(P[:, 0] - c[0], P[:, 1] - c[1]) <= r) & (np.abs(P[:, 2] - c[2]) <= dz)
    # 最后一个守位：从后往前找第一帧 i，到 i 为止在它周围（平面 96、高差 40）已经累计停留 ≥ 1.5 秒；
    # 守位中心取这些帧的中位。只数 i 之前的帧：慢慢走过一片地方不会被当成守位（第 3 场 R21 死前在 A 点边走边切火）
    center = None
    upto = np.arange(n)
    for i in range(n - 1, -1, -1):
        m = member(P[i]) & (upto <= i)
        if m.sum() * dt >= hold_s:
            c = np.median(P[m], axis=0)
            if member(c).sum() * dt < hold_s:
                c = P[i]
            center = c
            break
    if center is None:
        return None
    mem = member(center)
    last = int(np.nonzero(mem)[0][-1])
    if last >= n - 1:
        return None                                        # 一直站到死，没走开
    end_p = C.pos(ctx, u, ud - 1)
    far = max([C.dist3(P[j], center) for j in range(last + 1, n)] + [C.dist3(end_p, center)])
    if far < float(th['leave_3d']):
        return None
    # 离开时刻：最后一个在范围里的帧和下一帧之间逐 tick 找第一次走出范围
    tl = tks[last + 1]
    for tk in range(tks[last] + 1, tks[last + 1]):
        p = C.pos(ctx, u, tk)
        if math.hypot(p[0] - center[0], p[1] - center[1]) > r or abs(p[2] - center[2]) > dz:
            tl = tk
            break
    us, them = C.alive_counts(ctx, tl, sid=u)
    defused_before = bool(len(R.begin[R.begin['tick'] <= tl]))
    left = _bomb_left(ctx, R, tl)
    if not (us > them >= 1) or defused_before or left < float(th['leave_left_s']) \
            or (ud - tl) / _tr(ctx) > float(th['leave_die_s']):
        return None
    t_l, t_die = R.t(ctx, tl), R.t(ctx, ud)
    left_i = _int(left)
    dr = C.deaths(ctx, R.n)
    drow = dr[(dr['tick'] == ud) & (dr['vic'] == u)]
    killer = str(drow.iloc[0]['att']) if len(drow) else None
    if killer not in R.enemies:
        return None
    kname = C.name_of(ctx, killer)
    hold_tk = tks[last]
    hold_pl = _place(ctx, u, hold_tk)
    to_pl = _place(ctx, u, ud - 1)
    k_pl = _place(ctx, killer, ud) if killer else None
    hand = C.wkey(C.weapon_at(ctx, u, ud - 1))
    hand_cn = _HAND_CN.get(hand) if hand and not C.is_gun(hand) else None
    hidden = bool(killer) and not C.on_radar(ctx, killer, tl - _s2t(ctx, th['hidden_pre_s']),
                                             ud - _s2t(ctx, th['hidden_post_s']), team_sid=u)
    # P1：“没露过面”按整回合的全队雷达 + 击杀栏写；窗口里没上雷达但之前露过面的，写他最后露面是第几秒
    hid_ph, rev_tk = C.hidden_phrase(ctx, killer, ud - _s2t(ctx, th['hidden_post_s']), team_sid=u)
    hid_txt = ('他' + hid_ph) if hidden else ''
    won = R.tl.get('winner') == R.side
    gun = _primary(_inventory(ctx, u, ud - 1))
    gun_cn = C.weapon_cn(gun) if gun else None
    cost = int(_th(ctx)['price'].get(gun, 0)) if gun else 0
    facts = dict(tl=C.sec(t_l), death_t=C.sec(t_die), us=us, them=them, bomb_left_s=left_i, killer=kname,
                 hold_place=hold_pl, to_place=to_pl, killer_place=k_pl, in_hand=hand_cn, killer_hidden=hidden,
                 round_won=won, gun_lost=gun_cn, override_duel=True,
                 last_revealed_t=None if rev_tk is None else C.sec(C.t_of(ctx, rev_tk, R.n)))
    # Q18：拿着火往包那边挪 = 去拦拆包（按意图算合理）；只有凶手在你离开前后上过我方雷达（走进已知敌人的枪线）才记问题
    toward = _dist_bomb(ctx, R, u, tl) - _dist_bomb(ctx, R, u, ud - 1) >= float(th['stop_defuse_shrink'])
    stop_defuse = bool(hand in set(th.get('stop_defuse_hand') or ()) and toward)
    facts.update(stop_defuse=stop_defuse)
    if stop_defuse and hidden:
        frm = f'从{hold_pl}' if hold_pl else ''
        text = (f'下包后 {C.alive_str(us, them)}，离爆炸还有 {left_i} 秒，你拿着{hand_cn}{frm}往包那边挪，去拦拆包，'
                f'被{_at(k_pl)}{kname} 打死' + (f'（{hid_txt}）' if hid_txt else ''))
        evid = (f"第 {C.fmt_sec(t_l)} 秒你拿着{hand_cn}离开守位往包那边走，之前没有人开始拆包；"
                f"第 {C.fmt_sec(t_die)} 秒被 {kname} 打死" + (f'，{hid_txt}' if hid_txt else ''))
        facts.update(override_duel=False, branch='拦拆')
        return _hit(ctx, R, 'POST-03', '判定', tl, 'moment', '合理代价', facts, text, '', evid, death_tick=ud)
    # 审核单第 36 条：“走出去找人”→“挪位置时走进了 X 的枪线”，写明手里拿的是什么
    if hold_pl and to_pl and hold_pl != to_pl:
        move = f'你从{hold_pl}挪到{to_pl}，'
    elif hold_pl:
        move = f'你离开{hold_pl}守的位置挪了出去，'
    else:
        move = '你离开守的位置挪了出去，'
    line_of = f'{_at(k_pl)}{kname} 的枪线'
    text = (f'下包后 {C.alive_str(us, them)}，离爆炸还有 {left_i} 秒，{move}'
            + (f'手里拿着{hand_cn}，' if hand_cn else '') + f'挪位置时走进了{line_of}，被他打死'
            + (f'（{hid_txt}）' if hid_txt else '') + ('；没影响输赢' if won else ''))
    if gun_cn:
        # 重买的钱只对你这边能买、又是你自己带进这回合的枪写（635 高分玩家50 R2：T 捡来的 M4A1-S，T 买不到）
        start = ((R.tl.get('econ') or {}).get('user') or {})
        own = {C.wkey(x) for x in [start.get('primary')] + list(start.get('inventory_at_freeze_end') or []) if x}
        buyable = C.wkey(gun) not in set(th.get('side_only', {}).get('CT' if R.side == 'T' else 'T', ()))
        picked = C.wkey(gun) not in own
        facts.update(gun_picked=picked, gun_buyable=buyable)
        if _same_side_next(ctx, R) and cost and buyable and not picked:
            text += f'，但丢了 {gun_cn}，下回合要花 {C.money(cost)} 重买'
            facts['rebuy'] = cost
        elif picked:
            text += f'，但丢了这把捡来的 {gun_cn}'
        else:
            text += f'，但丢了 {gun_cn}'
    # 回防路线：你守位时朝着哪条，活着的队友朝着哪条（只看位置 + 朝向，写“朝着”不写“看得到”）
    lanes = (th.get('lanes') or {}).get(R.plant['site'], {})
    lane_txt = ''
    if lanes:
        deg = float(th['lane_deg'])
        hp_ = C.pos(ctx, u, hold_tk)
        _, yaw_u = C.view(ctx, u, hold_tk)
        mine = [ln for ln, q in lanes.items() if _angdiff(yaw_u, _bearing(hp_, q)) <= deg]
        cov = {ln: [] for ln in lanes}
        for m in R.mates:
            if not C.alive(ctx, m, tl, R.n):
                continue
            mp = C.pos(ctx, m, tl)
            _, yw = C.view(ctx, m, tl)
            for ln, q in lanes.items():
                if _angdiff(yw, _bearing(mp, q)) <= deg:
                    cov[ln].append(C.name_of(ctx, m))
        parts = []
        if mine:
            parts.append(f"你在{hold_pl or '守位'}朝着{'、'.join(mine)}")
        for ln in lanes:
            if cov[ln]:
                parts.append(f"{'、'.join(cov[ln])}{' 都' if len(cov[ln]) > 1 else ' '}朝着{ln}")
        free = [ln for ln in lanes if ln not in mine and not cov[ln]]
        parts.append(f"{'、'.join(free)}方向没人朝着" if free else '两条路都有人朝着')
        lane_txt = '，'.join(parts)
        facts.update(lanes_user=mine, lanes_mates={k: v for k, v in sorted(cov.items()) if v}, lanes_free=free)
    # 朝向观察是事实，放证据；“还可以”只放另一种打法
    alt = (f"还可以留在{hold_pl or '守位'}拿枪等，" + (f'{hand_cn}留着，' if hand_cn and hand != 'knife' else '')
           + '听到拆包声再出来或者往包上扔；CT 要来拆包，人多时让他来找你')
    kb = []
    if R.plant['site'] == 'B':
        alt += '；道具可以往回防路线丢'
        kb = []
    evid = (f"第 {C.fmt_sec(t_l)} 秒你离开守位，之前没有人开始拆包；第 {C.fmt_sec(t_die)} 秒被 {kname} 打死"
            + (f'，{hid_txt}' if hid_txt else '') + (f'；{lane_txt}' if lane_txt else ''))
    return _hit(ctx, R, 'POST-03', '问题', tl, 'moment', '你的责任', facts, text, alt, evid, kb=kb, death_tick=ud)


# ================================================================ POST-04 残局守包边走边开枪
def _post04(ctx, R):
    th = _th(ctx)
    u = R.u
    if R.side != 'T' or not R.plant:
        return None
    end = R.udeath if R.udeath is not None else R.end
    st = [s for s in _states(ctx, R, end) if s[0] >= R.plant['tick']]
    first = next((s for s in st if s[1] == 1 and s[2] >= 2), None)
    if first is None:
        return None
    t0k, _, n_them = first
    fr = C.fires(ctx, R.n, sid=u)
    fr = fr[(fr['tick'] >= t0k) & (fr['tick'] <= end)]
    shots, moving = 0, 0
    for row in fr.itertuples():
        if not C.alive(ctx, u, int(row.tick), R.n):
            continue
        sc = getattr(row, 'user_is_scoped', None)
        sc = C.scoped_at(ctx, u, int(row.tick)) if sc is None or sc != sc else bool(sc)
        mw = C.move_word(ctx, C.fire_speed(ctx, row), row.wk, sc)
        shots += 1
        moving += mw != '停稳'
    if moving < int(th['moving_n']) or moving < float(th['moving_share']) * shots:
        return None
    hu = C.hurts(ctx, R.n)
    hits = int(((hu['att'] == u) & hu['vic'].isin(R.enemies) & (hu['tick'] >= t0k) & (hu['tick'] <= end)).sum())
    # 两个人同时在你视野里（同一帧有两个活着的敌人 spotted_by 里有你），或者两个人都打中过你
    two_seen = False
    step = int(th['frame_step_ticks'])
    for tk in range(t0k, end + 1, step):
        k = sum(1 for e in R.enemies if C.alive(ctx, e, tk, R.n) and C.spots(ctx, u, e, tk, tk) is not None)
        if k >= 2:
            two_seen = True
            break
    hurt_by = sorted(set(hu[(hu['vic'] == u) & hu['att'].isin(R.enemies) & (hu['tick'] >= t0k) & (hu['tick'] <= end)]['att']))
    two_hurt = len(hurt_by) >= 2
    t_0 = R.t(ctx, t0k)
    left_i = _int(_bomb_left(ctx, R, t0k))
    pl = _place(ctx, u, t0k)
    ebuy = C.buy(ctx, R.n, 'them')
    gun = _primary(_inventory(ctx, u, t0k))
    if n_them >= 3:
        blame = '合理代价'
    elif gun is None and ebuy == '全起':
        blame = '合理代价'
    else:
        blame = '你的责任'
    few = hits * int(th['few_hits_div']) < shots
    hit_txt = '一枪都没打中' if hits == 0 else f"{'只' if few else ''}打中 {hits} 枪"
    text = (f'第 {C.fmt_sec(t_0)} 秒变成 1v{n_them} 守包，离爆炸还有 {left_i} 秒；之后你'
            + (f'在{pl}' if pl else '') + f'一边走一边开枪，{shots} 枪里 {moving} 枪是走着开的，{hit_txt}'
            + ('，而且两个人同时出现在你视野里' if two_seen else ('，而且两个人都打到过你' if two_hurt else '')))
    no_kit = {'手枪局': '对面是手枪局多半没钳子，', 'eco': '对面是 eco 局多半没钳子，'}.get(ebuy, '')
    alt = '还可以在包附近找一个一次只会被一个人看到的掩体站定再开枪，等他们来找你；' + no_kit + '听到拆包声再出来也来得及'
    end_t = R.t(ctx, end)
    facts = dict(alive_mode='lag0', t0=C.sec(t_0), t_end=C.sec(end_t), us=1, them=n_them, bomb_left_s=left_i, shots=shots, moving=moving,
                 hits=hits, two_seen=two_seen, two_hurt=two_hurt, enemy_buy=ebuy, place=pl,
                 hurt_by=[C.name_of(ctx, s) for s in hurt_by])
    evid = f'第 {C.fmt_sec(t_0)} 秒到第 {C.fmt_sec(end_t)} 秒你开了 {shots} 枪，其中 {moving} 枪开枪时没停稳'
    return _hit(ctx, R, 'POST-04', '问题', t0k, 'moment', blame, facts, text, alt, evid,
                death_tick=R.udeath if R.udeath is not None and R.udeath <= end else None)


# ================================================================ POST-G1 回防多打少一起进点先清人
def _post_g1(ctx, R):
    th = _th(ctx)
    u = R.u
    if R.side != 'CT' or not R.plant:
        return None
    pt = R.plant['tick']
    dr = C.deaths(ctx, R.n)
    # ① 下包后你打死敌人 E：击杀前一刻我方人多，身边有队友和 E（或看见你的敌人）互相 spotted
    k1 = None
    for row in dr[(dr['att'] == u) & dr['vic'].isin(R.enemies) & (dr['tick'] > pt)].itertuples():
        kt = int(row.tick)
        us, them = C.alive_counts(ctx, kt, sid=u, lag_s=0)
        if us <= them:
            continue
        w0, w1 = kt - _s2t(ctx, th['g1_pre_s']), kt + _s2t(ctx, th['g1_post_s'])
        opp = {row.vic} | {e for e in R.enemies if C.spots(ctx, e, u, w0, w1) is not None}
        me = C.pos(ctx, u, kt)
        tog = []
        for m in R.mates:
            if not C.alive(ctx, m, kt, R.n):
                continue
            dm = C.dist3(me, C.pos(ctx, m, kt))
            if dm <= float(th['g1_together_3d']) and any(_mutual(ctx, m, e, w0, w1) for e in sorted(opp)):
                tog.append((dm, m))
        if tog:
            k1 = dict(tick=kt, vic=row.vic, us=us, them=them, mates=[m for _, m in sorted(tog)])
            break
    # ② 包最后被拆：你第一次进包点时对方还有活人、800 内有活着的队友，开始拆包不早于最后一个敌人死亡
    k2 = None
    defuser = str(R.defused.iloc[-1]['user_steamid']) if len(R.defused) else None
    if defuser:
        site_place = 'Bombsite' + R.plant['site']
        end = R.udeath if R.udeath is not None else R.end
        entry = next((tk for tk in range(pt, end) if C.place_at(ctx, u, tk) == site_place), None)   # 逐 tick 找
        if entry is not None:
            e_alive = C.alive_list(ctx, R.n, entry, enemy=True)
            me = C.pos(ctx, u, entry)
            near = sorted((C.dist3(me, C.pos(ctx, m, entry)), m) for m in R.mates if C.alive(ctx, m, entry, R.n))
            near = [(dm, m) for dm, m in near if dm <= float(th['g1_entry_mate_3d'])]
            us, them = C.alive_counts(ctx, entry, sid=u)
            ed = dr[dr['vic'].isin(R.enemies)]
            last_e = int(ed['tick'].max()) if len(ed) else None
            b_after = R.begin[R.begin['tick'] > pt]
            first_b = int(b_after['tick'].iloc[0]) if len(b_after) else None
            if e_alive and near and us > them and last_e is not None and first_b is not None and first_b >= last_e:
                bb = R.begin[R.begin['user_steamid'] == defuser]
                kit = bool(bb.iloc[-1]['haskit']) if len(bb) and 'haskit' in bb else _has_kit(ctx, defuser, int(R.defused.iloc[-1]['tick']))
                k2 = dict(tick=entry, us=us, them=them, mates=[m for _, m in near], defuser=defuser, kit=kit)
    if k1 is None and k2 is None:
        return None
    main = k1 is not None or defuser == u
    base = k2 or k1
    mates = list(dict.fromkeys((k1['mates'] if k1 else []) + (k2['mates'] if k2 else [])))
    names = [C.name_of(ctx, m) for m in mates]
    site_cn = f"{R.plant['site']} 点" if R.plant['site'] in ('A', 'B') else '包点'
    text = f"下包后 {C.alive_str(base['us'], base['them'])}，你和 {'、'.join(names)} 一起进{site_cn}"
    facts = dict(us=base['us'], them=base['them'], mates=names, site=R.plant['site'], main=main,
                 alive_mode=None if k2 else 'lag0',
                 entry_t=C.sec(R.t(ctx, k2['tick'])) if k2 else None, defuser=C.name_of(ctx, defuser) if defuser else None)
    if k1:
        kt = R.t(ctx, k1['tick'])
        text += f"，第 {C.fmt_sec(kt)} 秒打死 {C.name_of(ctx, k1['vic'])}"
        facts.update(kill_t=C.sec(kt), victim=C.name_of(ctx, k1['vic']))
    if k2:
        # 清人、拆包只记给真做了的人：你和一起进点的队友在下包后打死过人、或拆了包，才写“先把人清干净再拆包”（8d R1）
        group = {u} | set(mates)
        cleared = {str(a) for a in dr[(dr['tick'] >= k2['tick']) & dr['vic'].isin(R.enemies)]['att']}   # 进点之后的击杀
        if (cleared & group) or defuser in group:
            text += '，先把人清干净再拆包'
            if not k2['kit']:
                text += '（没钳子拆得慢，先清人更稳）'
        elif defuser:
            text += f"回防，最后的人是 {'、'.join(C.name_of(ctx, x) for x in sorted(cleared))} 清掉的，{C.name_of(ctx, defuser)} 拆了包" if cleared                 else f"回防，{C.name_of(ctx, defuser)} 拆了包"
        facts['kit'] = k2['kit']
    facts['why_good'] = '多打少一起进、身后有人补，对面一次只能打一个人'
    alt = '还可以进点前先把火和闪扔进守包常见的位置，再和队友一起拉'
    t_hit = base['tick']
    evid = (f"下包后你和 {'、'.join(names)} 一起进{site_cn}"
            + (f"，第 {C.fmt_sec(R.t(ctx, k1['tick']))} 秒打死 {C.name_of(ctx, k1['vic'])}" if k1 else '')
            + (f"，{'你' if defuser == u else C.name_of(ctx, defuser)}在对面全部倒下之后才开始拆包" if k2 else ''))
    return _hit(ctx, R, 'POST-G1', '好', t_hit, 'moment' if main else 'round', '—', facts, text, alt, evid)


# ================================================================ 入口
def run(ctx):
    """跑这一类的全部规则，返回 Hit 列表（按回合、时刻、规则排）"""
    hits = []
    for info in C.rounds(ctx):
        R = _Round(ctx, info)
        if R.side not in ('T', 'CT') or C.team_of(ctx, R.u, R.n) == 0:
            continue
        h01, p01_problem = _post01(ctx, R)
        h02 = _post02(ctx, R, p01_problem)
        if not h01:
            h01 = _post01_far(ctx, R, h02)
        for h in tuple(h01) + (h02, _post03(ctx, R), _post04(ctx, R), _post_g1(ctx, R)):
            if h is not None:
                hits.append(h)
    hits.sort(key=lambda h: (h['round'], h['t'], h['rule']))
    return hits
