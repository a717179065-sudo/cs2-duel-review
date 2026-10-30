# -*- coding: utf-8 -*-
"""团队配合类规则：TEAM-01..07（判定 / 问题）、TEAM-G1..G3（好的操作），以及 decisions #24 的 eco "思路"。

入口：run(ctx) -> list[Hit]（Hit 字段见 CONTRACT.md）。同一个 demo 每次结果一样（不用随机数和当前时间，遍历都显式排序）。
口径：rules_v1.json 的 trigger / blame / template / alt，decisions_v1.md 覆盖：
  #3  TEAM-07 不按凶手距离判，判"一个人推太深、没有配合"：推进 = 报点不在本方常规位 + 离开局位置越来越远；
      配合位用固定报点对照表（thresholds/team.json coord_places）；3D + spotted 只用来判"有没有队友能补"。
  #24 eco 被一个人短时间连杀 = 合理代价；对面已经在一个地方杀过人、我们还去找他 → TEAM-03 的"思路"（回合时间够才提），
      建议按凶手武器写：找对面拿步枪 / AWP 的人，打掉能捡长枪；凶手是冲锋枪（P90 除外）才写赏金对比。
审核单（手册 v1，2026-10-30）：第 7/17 条 TEAM-04 七种豁免（_t04_exempt_reason，记在 exemptions(ctx)，这次阵亡交给对枪动作写）；
      第 28 条进点没人跟上（_entry_no_follow：TEAM-04 variant=entry 记团队 / TEAM-05 variant=entry_follow 记后面的人）；
      第 8 条 TEAM-06 先判意图（_move_intent，T、CT 都做）；第 16 条删 TEAM-05 只听得到枪声的分支；第 34 条 TEAM-G3 加门；
      Q19 单走一侧 CT 也适用、Q20 窗口 5 秒、Q21 对面半起不算反 eco（thresholds/team.json）。
C-* 口径（人数、存活、可见、雷达、听声、买法、报点、秒数）一律用 engine.common；阈值只从 ctx.th['team'] / ['common'] 取。
句子遵守 P6：不写坐标、速度、角度、百分比；报点用中文；不写对方血量、不写语音建议、不引用知识库原文（kb 只放 id）。

原型：pro/analysis/match3/rules_proto/团队配合.py（TEAM-02..10 旧编号）+ 好的操作.py（GOOD-03/05/10/13）。
"""
from collections import defaultdict

import numpy as np

from engine import common as C
from engine.rules import pos as _pos

# pos.move_intent 的意图 → TEAM-06 豁免记录里的中文
_POS_INTENT_CN = {'plant': '下包', 'retake': '回防', 'stop_defuse': '拦拆', 'pickup': '捡枪', 'refill': '补位',
                  'anchor': '补防', 'block': '堵路'}

RULE_IDS = ('TEAM-01', 'TEAM-02', 'TEAM-03', 'TEAM-04', 'TEAM-05', 'TEAM-06', 'TEAM-07', 'TEAM-G1', 'TEAM-G2', 'TEAM-G3')

# 报点 → 区域（只用来给"还可以"挂知识库条目：阵营、区域对不上就不挂，M6）
_AREA = {
    'BombsiteA': 'A', 'TRamp': 'A', 'Palace': 'A', 'PalaceInterior': 'A', 'PalaceAlley': 'A', 'Scaffolding': 'A',
    'Stairs': 'A', 'Jungle': 'A', 'Ticketbooth': 'A', 'CTSpawn': 'A',
    'BombsiteB': 'B', 'Apartments': 'B', 'Truck': 'B', 'Shop': 'B', 'Market': 'B', 'BackAlley': 'B', 'Kitchen': 'B',
    'Middle': 'MID', 'TopofMid': 'MID', 'Connector': 'MID', 'Underpass': 'MID', 'Catwalk': 'MID', 'SnipersNest': 'MID',
    'Ladder': 'MID',
}


# ================================================================ 小工具
def _th(ctx):
    return ctx.th['team']


def _tr(ctx):
    return int(ctx.th['common'].get('tickrate', C.TR))


def _memo(ctx):
    """本模块在 ctx 上的内存缓存（同一个 ctx 只建一次）"""
    m = getattr(ctx, '_team_memo', None)
    if m is None:
        m = {}
        setattr(ctx, '_team_memo', m)
    return m


def _won(ctx, rnd):
    r = C.tl_round(ctx, rnd)
    return bool(r) and r.get('result') == '赢'


def _plant(ctx, rnd):
    """(下包 tick, 包点报点原文, 包的 3D 位置) 或 None；包的位置取下包人那一刻的脚下"""
    r = C.tl_round(ctx, rnd)
    p = (r or {}).get('plant')
    if not p:
        return None
    tick = int(p['tick'])
    sid = C.sid_of(ctx, p.get('planter'))
    xyz = C.pos(ctx, sid, tick) if sid else None
    site = {'A': 'BombsiteA', 'B': 'BombsiteB'}.get(p.get('site'), p.get('planter_place'))
    return tick, site, xyz


def _defuse(ctx, rnd):
    """(拆包 tick, 拆包人 sid) 或 None"""
    r = C.tl_round(ctx, rnd)
    d = (r or {}).get('defuse')
    if not d or d.get('t') is None:
        return None
    return C.tick_of(ctx, rnd, d['t']), C.sid_of(ctx, d.get('defuser'))


def _nm(ctx, sid, U):
    """句子里的人名：用户写"你"，其他人写游戏名"""
    return '你' if sid == U else C.name_of(ctx, sid)


def _t(ctx, tick, rnd):
    return C.sec(C.t_of(ctx, tick, rnd))


def _ts(ctx, tick, rnd):
    return C.fmt_sec(C.t_of(ctx, tick, rnd))


def _kills(ctx, rnd):
    """本回合（回合结束前）敌对击杀，按 tick 排：dict(tick, att, vic, wk, vplace, aplace, vpos, apos)"""
    m = _memo(ctx)
    key = ('kills', int(rnd))
    if key in m:
        return m[key]
    end = C.round_info(ctx, rnd)['end']
    out = []
    for x in C.deaths(ctx, rnd).itertuples():
        if int(x.tick) > end or not C.is_enemy(ctx, x.att, x.vic, rnd):
            continue
        out.append(dict(tick=int(x.tick), att=x.att, vic=x.vic, wk=x.wk,
                        vplace=getattr(x, 'user_last_place_name', None), aplace=getattr(x, 'attacker_last_place_name', None),
                        vpos=np.array([x.user_X, x.user_Y, x.user_Z], float),
                        apos=np.array([x.attacker_X, x.attacker_Y, x.attacker_Z], float)))
    out.sort(key=lambda k: (k['tick'], k['vic']))
    m[key] = out
    return out


def _all_deaths(ctx, rnd):
    """本回合名单里所有人的阵亡（含自杀、摔死），按 tick 排：[(tick, vic)]"""
    end = C.round_info(ctx, rnd)['end']
    roster = set(C.mates(ctx, rnd, include_self=True)) | set(C.enemies(ctx, rnd))
    out = sorted({(int(x.tick), x.vic) for x in C.deaths(ctx, rnd).itertuples() if x.vic in roster and int(x.tick) <= end})
    return out


def _after(ctx, tick, sid):
    """这次阵亡之后（含同一 tick）的人数 (sid 一方, 对方)"""
    return C.alive_counts(ctx, int(tick) + 1, sid=sid, lag_s=0)


def _anti_eco(ctx, rnd):
    th = _th(ctx)
    return C.buy(ctx, rnd, 'us') in th['anti_eco_us'] and C.buy(ctx, rnd, 'them') in th['anti_eco_them']


def _my_eco(ctx, rnd):
    th = _th(ctx)
    return C.buy(ctx, rnd, 'us') in th['eco_us'] and C.buy(ctx, rnd, 'them') not in th['eco_us'] + ['手枪局']


def _killed_by_team(ctx, rnd, K, team_of_sid, t0, t1):
    """t0 < tick <= t1 里 K 被 team_of_sid 那一队打死的 tick（没有 None）"""
    tm = C.team_of(ctx, team_of_sid, rnd)
    for k in _kills(ctx, rnd):
        if k['vic'] == K and t0 < k['tick'] <= t1 and C.team_of(ctx, k['att'], rnd) == tm:
            return k['tick']
    return None


def _cells(ctx, t0, t1):
    """[t0, t1] 按 contact_step_ticks 切格：[(格起点, 格终点)]"""
    st = int(_th(ctx)['contact_step_ticks'])
    return [(a, min(a + st - 1, int(t1))) for a in range(int(t0), int(t1) + 1, st)]


def _sees_run(ctx, viewer, target, t0, t1, min_s):
    """viewer 在 [t0, t1] 里连续 min_s 秒看见 target（C.spots 逐格；spotted 每 1/8 秒更新）→ 连续段第一格 tick 或 None"""
    st = int(_th(ctx)['contact_step_ticks'])
    need = max(1, int(round(min_s * _tr(ctx) / st)))
    run, first = 0, None
    for a, b in _cells(ctx, t0, t1):
        if C.spots(ctx, viewer, target, a, b) is not None:
            if run == 0:
                first = a
            run += 1
            if run >= need:
                return first
        else:
            run = 0
    return None


def _contact_start(ctx, V, K, T):
    """TEAM-04 接触开始：从阵亡往回找 V、K 任一方看见对方的连续一段的开头，最多回找 contact_back_s；没有可见记录用死前一格"""
    th = _th(ctx)
    st = int(th['contact_step_ticks'])
    lo = int(T - th['contact_back_s'] * _tr(ctx))
    cur, first = int(T) - 1, None
    while cur - st + 1 >= lo:
        a = cur - st + 1
        hit = [x for x in (C.spots(ctx, V, K, a, cur), C.spots(ctx, K, V, a, cur)) if x is not None]
        if not hit:
            break
        first = min(hit)                              # 这一格里第一次看见的那一 tick
        cur -= st
    return first if first is not None else int(T) - st


def _moving(ctx, V, tc):
    """TEAM-04 第 2 条：接触开始前两段 1 秒窗口（截止在 tc 和 tc−0.5 秒）任意一段平均路程速度 > moving_speed（晃身也算）"""
    th = _th(ctx)
    tr = _tr(ctx)
    win = th['move_win_s']
    best = 0.0
    for lag in (0.0, th['move_win_lag_s']):
        end = int(tc - lag * tr)
        best = max(best, C.path_len(ctx, V, end - int(win * tr), end) / win)
    return best > th['moving_speed'], best


def _alive_mates(ctx, rnd, V, tick):
    return [m for m in C.mates(ctx, rnd, V) if C.alive(ctx, m, tick, rnd)]


def _near_mates(ctx, rnd, V, tick, r):
    """tick 时离 V 3D <= r 的活着的队友"""
    p = C.pos(ctx, V, tick)
    return [m for m in _alive_mates(ctx, rnd, V, tick) if C.dist3(C.pos(ctx, m, tick), p) <= r]


def _kb(ids, side, place):
    """M6：知识库条目按阵营和区域配，对不上就不挂（GEN-/ALL 通用的总带上）"""
    area = _AREA.get(place)
    out = []
    for i in ids:
        parts = i.split('-')
        if len(parts) < 3:
            continue
        s, a = parts[0], parts[1]
        if s not in ('GEN', side):
            continue
        if a != 'ALL' and a != area:
            continue
        out.append(i)
    return out


def _inv_frames(ctx):
    """每 8 tick 的背包（daoju / pp_ticks 缓存的 inventory 列）：{sid: (ticks, [背包列表])}；没有返回 {}"""
    m = _memo(ctx)
    if 'inv' in m:
        return m['inv']
    out = {}
    for name in ('daoju', 'pp_ticks'):
        try:
            raw = ctx.cache(name)
        except (FileNotFoundError, KeyError, OSError):
            continue
        T = raw.get('ticks') if isinstance(raw, dict) else None
        if T is None or 'inventory' not in T:
            continue
        T = T[['tick', 'steamid', 'inventory']].copy()
        T['sid'] = T['steamid'].map(lambda s: str(int(s)) if isinstance(s, float) else str(s))
        T = T.sort_values(['sid', 'tick'], kind='mergesort')
        for sid, g in T.groupby('sid', sort=True):
            out[sid] = (g['tick'].to_numpy(np.int64), list(g['inventory']))
        break
    m['inv'] = out
    return out


def _nades_at(ctx, sid, tick):
    """tick 前最近一帧背包里的闪 + 烟颗数（没有背包数据返回 0）"""
    fr = _inv_frames(ctx).get(sid)
    if not fr:
        return 0
    tk, inv = fr
    i = int(np.searchsorted(tk, int(tick), side='right')) - 1
    if i < 0:
        return 0
    items = inv[i] if isinstance(inv[i], (list, tuple, np.ndarray)) else []
    return sum(1 for w in items if isinstance(w, str) and (('flash' in w.lower()) or ('smoke' in w.lower())))


def _util_dets(ctx):
    """闪 + 烟起爆：[(tick, 扔的人 sid, xyz)]，按 tick 排（烟用 C.smokes，闪从缓存事件里取）"""
    m = _memo(ctx)
    if 'util' in m:
        return m['util']
    out = []
    for s in C.smokes(ctx):
        out.append((int(s['t0']), str(s.get('by')), np.array([s['x'], s['y'], s['z']], float)))
    fl = None
    for name in ('daoju', 'sight', 'sight_s4', 'ticks'):
        try:
            raw = ctx.cache(name)
        except (FileNotFoundError, KeyError, OSError):
            continue
        ev = raw.get('ev') if isinstance(raw, dict) else None
        if isinstance(ev, dict) and 'flashbang_detonate' in ev and len(ev['flashbang_detonate']):
            fl = ev['flashbang_detonate']
            break
    if fl is None:
        try:
            fl = ctx.cache('ev_flashbang_detonate', dtype={'user_steamid': str})
        except (FileNotFoundError, KeyError, OSError):
            fl = None
    if fl is not None:
        for r in fl.itertuples():
            sid = str(int(r.user_steamid)) if isinstance(r.user_steamid, float) else str(r.user_steamid)
            out.append((int(r.tick), sid, np.array([r.x, r.y, r.z], float)))
    out.sort(key=lambda u: (u[0], u[1]))
    m['util'] = out
    return out


def _pickups(ctx):
    """item_pickup（带 3D 位置时更准）：DataFrame tick, sid, item, user_X/Y/Z"""
    m = _memo(ctx)
    if 'pick' in m:
        return m['pick']
    pk = None
    try:
        ev = ctx.cache('team_ev')
        pk = ev.get('pick') if isinstance(ev, dict) else None
    except (FileNotFoundError, KeyError, OSError):
        pk = None
    if pk is None or not len(pk):
        try:
            pk = ctx.cache('ev_item_pickup', dtype={'user_steamid': str})
        except (FileNotFoundError, KeyError, OSError):
            pk = None
    if pk is not None and len(pk):
        pk = pk.copy()
        # 捡起事件偶尔没有 steamid（NaN，比如回合切换时的系统发枪）：记空串，后面按人找时自然对不上
        pk['sid'] = pk['user_steamid'].map(lambda s: ('' if s != s else str(int(s))) if isinstance(s, float) else str(s))
        pk['tick'] = pk['tick'].astype(np.int64)
        pk = pk.sort_values(['tick', 'sid'], kind='mergesort').reset_index(drop=True)
    m['pick'] = pk
    return pk




def _tidy(ctx, s):
    """句子排版：common.tidy（人名整体保护）"""
    return C.tidy(s, C.player_names(ctx))


def _hit(rule, kind, rnd, tick, ctx, U, subject, blame, text, alt='', kb=None, evidence='', facts=None,
         scope='moment', death_t=None, death_of=None, merge_key=None, t=None):
    """拼一个 Hit（字段顺序与 CONTRACT 一致）"""
    if t is None:
        t = _t(ctx, tick, rnd) if tick is not None else 0.0
    return {
        'rule': rule, 'kind': kind, 'round': int(rnd), 't': float(t), 'tick': None if tick is None else int(tick),
        'scope': scope, 'subject': C.name_of(ctx, subject) if subject else '团队', 'is_user': bool(subject == U),
        'blame': blame, 'death_t': death_t, 'death_of': death_of, 'merge_key': merge_key,
        'facts': C.plain(facts or {}), 'text': _tidy(ctx, text), 'alt': _tidy(ctx, alt or ''), 'kb': list(kb or []), 'evidence': _tidy(ctx, evidence),
    }


# ================================================================ TEAM-01 赢的回合功劳归属
def _wp(ctx, side, us, them, planted):
    """我方赢的概率（对标人数状态表，+0.5/+1 平滑）"""
    th = _th(ctx)
    ct, t = (us, them) if side == 'CT' else (them, us)
    if ct <= 0:
        return 0.0 if side == 'CT' else 1.0
    if t <= 0 and not planted:
        return 1.0 if side == 'CT' else 0.0
    key = f'{int(bool(planted))}|{ct}|{t}'
    w, n = th['wp_table'].get(key, [0, 0])
    own = (th.get('wp_by_demo') or {}).get(str(ctx.id), {}).get(key)     # 本场在对标库里：先减掉本场自己的计数
    if own:
        w, n = w - own[0], n - own[1]
    p = (w + th['wp_smooth_win']) / (n + th['wp_smooth_n'])
    return p if side == 'CT' else 1 - p


def _team01(ctx, U, rnd):
    th = _th(ctx)
    tr = _tr(ctx)
    side = C.user_side(ctx, rnd)
    pl = _plant(ctx, rnd)
    ptick = pl[0] if pl else None
    ours = set(C.mates(ctx, rnd, include_self=True))
    contrib = defaultdict(float)
    swings = []                                         # (ΔWP, kill)
    roster = C.mates(ctx, rnd, include_self=True), C.enemies(ctx, rnd)
    us, them = len(roster[0]), len(roster[1])
    hurt = C.hurts(ctx, rnd)
    kills = {(k['tick'], k['vic']): k for k in _kills(ctx, rnd)}
    one_v = None                                        # 变成 1vN 时：(sid, N, 胜率)
    last_enemy = None
    for tick, vic in _all_deaths(ctx, rnd):
        planted = ptick is not None and tick >= ptick
        before = _wp(ctx, side, us, them, planted)
        if vic in ours:
            us -= 1
        else:
            them -= 1
        after = _wp(ctx, side, us, them, planted)
        if vic in ours and us == 1 and them >= th['clutch_min_enemies'] and one_v is None:
            last = [s for s in roster[0] if C.alive(ctx, s, tick + 1, rnd)]
            if last:
                one_v = (last[0], them, after)
        k = kills.get((tick, vic))
        if k is None or vic in ours:
            continue
        last_enemy = (k, us)
        dv = after - before
        if dv <= 0 or k['att'] not in ours:
            continue
        swings.append((dv, k))
        # 一半给凶手；另一半按本回合此前我方对他打的血（含道具）分
        h = hurt[(hurt['vic'] == vic) & (hurt['tick'] <= tick) & (hurt['att'].isin(sorted(ours)))]
        tot = float(h['dmg'].sum())
        if tot > 0:
            for who, dmg in sorted(h.groupby('att')['dmg'].sum().items()):
                contrib[who] += dv * 0.5 * float(dmg) / tot
            contrib[k['att']] += dv * 0.5
        else:
            contrib[k['att']] += dv
    # 拆包、1vN 收尾额外记给那个人：写进句子（"最后由某人 1vN 收尾、拆包"），不改占比——
    # 占比只按击杀和伤害分（rules_v1 bench 的 R6 0.26 / 队友C 0.41 就是这个口径），否则 R6 的"你和 队友C"会被收尾的人吃掉
    extra = {}
    df = _defuse(ctx, rnd)
    if df and df[1] in ours and side == 'CT':
        extra['defuser'] = df[1]
    if one_v and last_enemy and last_enemy[1] == 1 and last_enemy[0]['att'] == one_v[0]:
        extra['clutcher'] = one_v[0]
        extra['clutch_n'] = one_v[1]
    tot = sum(contrib.values())
    if tot <= 0:
        return []
    share = {s: v / tot for s, v in contrib.items()}
    order = sorted(share, key=lambda s: (-share[s], s))
    top = order[0]
    big = max(swings, key=lambda x: (x[0], -x[1]['tick'])) if swings else None
    ud = C.death_tick(ctx, U, rnd)
    my = share.get(U, 0.0)
    ukills = [k for k in _kills(ctx, rnd) if k['att'] == U]

    def klist(sid):
        ks = [k for k in _kills(ctx, rnd) if k['att'] == sid]
        return '、'.join(f"第 {_ts(ctx, k['tick'], rnd)} 秒打死 {C.name_of(ctx, k['vic'])}" for k in ks)

    kc = defaultdict(int)
    first_k = {}
    for k in _kills(ctx, rnd):
        if k['att'] in ours and k['vic'] not in ours:
            kc[k['att']] += 1
            first_k.setdefault(k['att'], k['tick'])

    def also_by_kills(named):
        # 击杀栏上拿的人头不比已经写出来的人少的队友也写上（8d R1 队友C 两杀、R2 队友A 两杀、R6 队友A 两杀没写）：
        # 份额会被“他后来被补掉 / 别人先打了血”压低，读者看到的是击杀栏
        lo = min([kc[x] for x in named] or [0])
        return [x for x in sorted(kc, key=lambda x: (first_k[x], x))
                if x not in named and x != U and kc[x] >= max(1, lo)]

    facts = dict(share={C.name_of(ctx, s): round(share[s], 2) for s in order},
                 kills=[dict(t=_t(ctx, k['tick'], rnd), killer=C.name_of(ctx, k['att']), victim=C.name_of(ctx, k['vic']))
                        for k in _kills(ctx, rnd)],
                 biggest=None if big is None else dict(swing=round(big[0], 2), killer=C.name_of(ctx, big[1]['att']),
                                                       victim=C.name_of(ctx, big[1]['vic']), t=_t(ctx, big[1]['tick'], rnd)),
                 defuser=C.name_of(ctx, extra['defuser']) if 'defuser' in extra else None,
                 clutcher=C.name_of(ctx, extra['clutcher']) if 'clutcher' in extra else None,
                 clutch_n=extra.get('clutch_n'))
    tick0 = ukills[0]['tick'] if ukills else (big[1]['tick'] if big else None)
    # 和你几乎一样多的队友（>= 你 × tie_ratio）：不算“主要是你打出来的”，写成你和他们一起打出来的（128 R23 三人 0.32/0.32/0.31）
    ties = [s for s in order if s != U and share[s] >= my * th['tie_ratio']] if top == U else []
    if top == U and my >= th['credit_share'] and ties:
        # 击杀栏上人头不比已写出来的人少的队友也写上（e6b R5：高分玩家04、高分玩家79 各一杀，和 高分玩家33 一样）
        ties = ties + [x for x in also_by_kills([U] + ties) if x not in ties]
        parts = []
        if ukills:
            parts.append('你' + klist(U))
        for s in ties:
            if any(k['att'] == s for k in _kills(ctx, rnd)):
                parts.append(f'{C.name_of(ctx, s)} ' + klist(s))
        tn = '、'.join(C.name_of(ctx, s) for s in ties)
        txt = f'这回合赢是你和 {tn} 一起打出来的' + (('：' + '；'.join(parts)) if parts else '')
        facts.update(credit='你和队友', mate=C.name_of(ctx, ties[0]), ties=[C.name_of(ctx, s) for s in ties])
        ev = f'这回合的人数优势是你和 {tn} 差不多一样多打出来的'
        top = ties[0]
    elif top == U and my >= th['credit_share'] and len(ukills) < th['credit_min_kills'] \
            and extra.get('clutcher') != U and extra.get('defuser') != U:
        # 份额最高但只拿了一杀、也没有收残局 / 拆包（635 R7：首杀之后队友打了 4 个）：不写“主要是你打出来的”
        mate = next((s for s in order if s != U), None)
        k = ukills[0] if ukills else None
        first = k is not None and min(x['tick'] for x in _kills(ctx, rnd)) == k['tick']
        lead = (f"这回合你拿下了首杀（第 {_ts(ctx, k['tick'], rnd)} 秒打死 {C.name_of(ctx, k['vic'])}）" if first else
                f"这回合你第 {_ts(ctx, k['tick'], rnd)} 秒打死 {C.name_of(ctx, k['vic'])}" if k is not None else '这回合')
        # 后面份额差不多的队友一起写，按击杀栏先后（128 R6：高分玩家15、高分玩家13 各一杀，只写了 高分玩家13）
        grp = []
        if mate:
            grp = [mate] + [x for x in order if x not in (U, mate) and share[x] >= share[mate] * th['mate_also_ratio']]
            grp += [x for x in also_by_kills(grp) if x not in grp]
            grp = sorted(grp, key=lambda x: (first_k.get(x, 10 ** 12), x))
        mn = '、'.join(C.name_of(ctx, x) for x in grp)
        txt = lead + (f"，后面的人数主要靠 {mn} 打出来" if grp else '')
        facts.update(credit='你和队友', mate=C.name_of(ctx, grp[0]) if grp else None, single=True,
                     mates=[C.name_of(ctx, x) for x in grp])
        ev = '你打出了第一份人数优势，后面的击杀是队友拿的'
        top = mate or U
    elif top == U and my >= th['credit_share']:
        second = order[1] if len(order) > 1 else None
        mate = None
        if second and share[second] >= my * th['mate_also_ratio']:
            mate = second
        if big and big[1]['att'] != U:
            mate = mate or big[1]['att']
        # 页面标签已经是“主要原因 · 你”，句子不再说一遍“主要是你”（635 高分玩家45 R1）
        txt = ('你打出了这回合的人数优势：' + klist(U)) if ukills else '这回合的人数优势是你打出来的'
        if mate:
            txt += f'；{C.name_of(ctx, mate)} 也拿下了关键的一杀'
        facts.update(credit='你', mate=C.name_of(ctx, mate) if mate else None)
        ev = f'这回合的人数优势里，你打出来的份额最多'
    elif U in order and order.index(U) == 1 and my >= th['co_share']:
        parts = []
        if ukills:
            parts.append('你' + klist(U))
        if any(k['att'] == top for k in _kills(ctx, rnd)):
            parts.append(f'{C.name_of(ctx, top)} ' + klist(top))
        more = also_by_kills([U, top])
        for x in more:
            parts.append(f'{C.name_of(ctx, x)} ' + klist(x))
        tn = '、'.join(C.name_of(ctx, x) for x in [top] + more)
        txt = f'这回合赢是你和 {tn} 一起打出来的' + (('：' + '；'.join(parts)) if parts else '')
        facts.update(credit='你和队友', mate=C.name_of(ctx, top), ties=[C.name_of(ctx, x) for x in [top] + more])
        ev = (f'这回合的人数优势主要是你和 {C.name_of(ctx, top)} 两个人打出来的' if not more else
              f'这回合的人数优势主要是你和 {tn} 打出来的')
    elif (big and big[1]['att'] == U and big[0] >= th['key_swing']
          and not (ud is not None and 0 <= ud - big[1]['tick'] <= th['key_survive_s'] * tr)):
        k = big[1]
        txt = f"这回合的关键一枪是你打的：第 {_ts(ctx, k['tick'], rnd)} 秒打死 {C.name_of(ctx, k['vic'])}，之后局面基本定了"
        facts.update(credit='关键一枪', key_t=_t(ctx, k['tick'], rnd), key_victim=C.name_of(ctx, k['vic']))
        ev = '这一杀让回合的胜算涨得最多'
        tick0 = k['tick']
    else:
        mates = [s for s in order if s != U]
        if not mates:
            return []
        mate = mates[0]
        # 和第一名差不多的队友一起写（8d R1：0.46 / 0.43）
        co = [s for s in mates[1:] if share[s] >= share[mate] * th['mate_also_ratio']]
        # 份额差不多的几个人按击杀栏先后写，读着和击杀栏对得上（8d R1：队友A 先拿了两杀，句子却先写 队友B）
        fk = {s: min((k['tick'] for k in _kills(ctx, rnd) if k['att'] == s), default=10 ** 12) for s in [mate] + co}
        grp = sorted([mate] + co, key=lambda s: (fk[s], s)) if co else [mate]
        more = also_by_kills(grp)
        if more:
            grp = sorted(grp + more, key=lambda x: (first_k.get(x, 10 ** 12), x))
        mn = '、'.join(C.name_of(ctx, s) for s in grp)
        txt = f'这回合主要靠 {mn} 打出人数'
        # 写出他们打了谁、在哪、第几秒（8d R14：只写了“主要靠 队友B”，没写他三杀在哪拿的）；
        # 你自己份额不小时也写上你的击杀（8d R14 你 85.8 秒打死 对手3 打成 2v1）
        bits = []
        for x in grp:
            ks = [k for k in _kills(ctx, rnd) if k['att'] == x][:int(th['t01_list_max'])]
            if ks:
                bits.append(f'{C.name_of(ctx, x)} ' + '、'.join(
                    f"第 {_ts(ctx, k['tick'], rnd)} 秒在{C.place_cn_at(ctx, x, k['tick'])}打死 {C.name_of(ctx, k['vic'])}" for k in ks))
        if bits and len(grp) <= int(th['t01_list_mates']):
            txt += '：' + '；'.join(bits)
        if ukills and my >= th['t01_mine_share']:
            txt += '；你' + klist(U)
        facts.update(credit='队友', mate=C.name_of(ctx, mate), mates=[C.name_of(ctx, s) for s in grp])
        ev = f'这回合的人数优势主要是 {mn} 打出来的'
        top = mate
    # 拆包、1vN 收尾的那个人
    tail = []
    if 'clutcher' in extra:
        # 只有残局的人活到最后（或拆了包）才是“收尾”；拿下最后一个人前后自己倒了、最后是包炸赢下的，照实写（bd R29）
        end = C.round_info(ctx, rnd)['end']
        cl = extra['clutcher']
        if C.alive(ctx, cl, end, rnd) or extra.get('defuser') == cl:
            tail.append(f"{_nm(ctx, cl, U)} 1v{extra['clutch_n']} 收尾")
        else:
            lk = last_enemy[0]
            boom = ((C.tl_round(ctx, rnd) or {}).get('reason') or '') == 'bomb_exploded'
            tail.append(f"{_nm(ctx, cl, U)} 1v{extra['clutch_n']} 拿下 {C.name_of(ctx, lk['vic'])} 后倒下"
                        + ('，包炸了赢下' if boom else ''))
            facts.update(clutcher_died=True)
    if 'defuser' in extra:
        d = _nm(ctx, extra['defuser'], U)
        tail.append('拆包' if tail and extra.get('clutcher') == extra['defuser'] else f'{d}拆包')
    if tail:
        lead = '；最后' if facts.get('clutcher_died') else ('；最后由' if not tail[0].startswith('你') else '；最后是')
        txt += lead + '、'.join(tail)
    if ukills:
        facts['mine'] = '你' + klist(U)          # 你的部分只写你自己的击杀（不和主要原因同一句）
    who = {'你': U, '你和队友': U, '关键一枪': U}.get(facts['credit'], top)
    return [_hit('TEAM-01', '判定', rnd, tick0, ctx, U, who, '—', txt, '', [], ev, facts, scope='round')]


# ================================================================ TEAM-03 被同一个人逐个打掉（先算，TEAM-02 要用）
def _same_spot(ctx, k1, k2):
    th = _th(ctx)
    d = C.dist3(k1['vpos'], k2['vpos'])
    return d <= th['same_spot'] or (k1['vplace'] and k1['vplace'] == k2['vplace'] and d <= th['same_spot_place'])


def _streaks(ctx, U, rnd):
    """同一个敌人连着打掉我方：返回事件列表 dict(killer, kills, variants)，按第一杀 tick 排"""
    th = _th(ctx)
    tr = _tr(ctx)
    tm = C.team_of(ctx, U, rnd)
    ours = [k for k in _kills(ctx, rnd) if C.team_of(ctx, k['vic'], rnd) == tm]
    by_k = defaultdict(list)
    for k in ours:
        by_k[k['att']].append(k)
    pl = _plant(ctx, rnd)
    events = []
    for K in sorted(by_k):
        ks = by_k[K]
        groups = []                                   # (variant, [kill])
        # (a) streak_s 秒内连杀 >= streak_n
        for i in range(len(ks)):
            j = i
            while j + 1 < len(ks) and ks[j + 1]['tick'] - ks[i]['tick'] <= th['streak_s'] * tr:
                j += 1
            if j - i + 1 >= th['streak_n']:
                groups.append(('a', ks[i:j + 1]))
                break
        # (b) 同一处接连出去
        chain = []
        for k1, k2 in zip(ks, ks[1:]):
            ok = k2['tick'] - k1['tick'] <= th['same_s'] * tr and _same_spot(ctx, k1, k2)
            if ok:
                ours_util = [u for u in _util_dets(ctx) if k1['tick'] < u[0] <= k2['tick'] and C.team_of(ctx, u[1], rnd) == tm
                             and C.dist3(u[2], k1['vpos']) <= th['util_r']]
                walked = C.path_len(ctx, k2['vic'], k2['tick'] - int(th['walk_s'] * tr), k2['tick'])
                ok = not ours_util and walked >= th['walk_min']
            if ok:
                if not chain:
                    chain = [k1]
                chain.append(k2)
            elif chain:
                groups.append(('b', chain))
                chain = []
        if chain:
            groups.append(('b', chain))
        groups = [g for g in groups if g[0] != 'b' or sum(_nades_at(ctx, k['vic'], k['tick'] - 1) for k in g[1]) >= 1]
        # (c) 回防逐个过去（我方 CT、已下包）
        if pl and C.side_of(ctx, U, rnd) == 'CT' and pl[2] is not None:
            after = [k for k in ks if k['tick'] > pl[0]]
            lo, hi = th['retake_gap_s']
            for k1, k2 in zip(after, after[1:]):
                gap = (k2['tick'] - k1['tick']) / tr
                if not (lo <= gap <= hi and _same_spot(ctx, k1, k2)):
                    continue
                closer = all(C.dist3(k['vpos'], pl[2]) < C.dist3(C.pos(ctx, k['vic'], pl[0]), pl[2]) for k in (k1, k2))
                if closer:
                    groups.append(('c', [k1, k2]))
        # 同一个凶手、有共同击杀的几种情况合成一件事
        merged = []
        for var, g in sorted(groups, key=lambda x: (x[1][0]['tick'], x[0])):
            ticks = {k['tick'] for k in g}
            for e in merged:
                if ticks & {k['tick'] for k in e['kills']}:
                    e['variants'].add(var)
                    e['kills'] = sorted({k['tick']: k for k in e['kills'] + g}.values(), key=lambda k: k['tick'])
                    break
            else:
                merged.append(dict(killer=K, kills=list(g), variants={var}))
        events += merged
    events.sort(key=lambda e: (e['kills'][0]['tick'], e['killer']))
    return events


def _team03(ctx, U, rnd, events):
    th = _th(ctx)
    tr = _tr(ctx)
    side = C.user_side(ctx, rnd)
    hits = []
    eco = _my_eco(ctx, rnd)
    for e in events:
        K, ks, var = e['killer'], e['kills'], e['variants']
        t0, t1 = ks[0]['tick'], ks[-1]['tick']
        kn = C.name_of(ctx, K)
        vics = [_nm(ctx, k['vic'], U) for k in ks]
        user_in = U in {k['vic'] for k in ks}
        dur = (t1 - t0) / tr
        fixed = _killed_by_team(ctx, rnd, K, U, t1, t1 + th['trade_s'] * tr)
        later = _killed_by_team(ctx, rnd, K, U, t1, C.round_info(ctx, rnd)['end'])
        burst = eco and 'a' in var and dur <= th['eco_burst_s']
        # 复核修正（审核单第 7 条 / Q15）：你第一个进点倒下、凶手只连杀两个就在 fast_trade_s 秒内被补掉——进点被换回来了，
        # 不写“被同一个人逐个打掉”（bd R29：对手3（第4场） 打死你和 队友B，0.7 秒后被 队友A 补掉）
        if side == 'T' and len(ks) <= 2 and ks[0]['vic'] == U and _killed_by_team(ctx, rnd, K, U, t1, t1 + th['fast_trade_s'] * tr)                 and _site_of(ctx, C.place_at(ctx, U, t0 - 1)) is not None and not (_plant(ctx, rnd) and _plant(ctx, rnd)[0] <= t0):
            _exempt(ctx, 'TEAM-03', rnd, t0, 'entry_traded', killer=kn)
            continue
        facts = dict(killer=kn, victims=vics, n=len(ks), variants=sorted(var), t0=_t(ctx, t0, rnd), t1=_t(ctx, t1, rnd),
                     dur=C.sec(dur), user_in=user_in, eco=eco, burst=burst,
                     kill_ts=[_t(ctx, k['tick'], rnd) for k in ks])
        if 'a' in var:
            kp = C.place_cn(ks[0]['aplace'])
            end = ('最后才被补掉' if fixed else ('过了好一会儿才被打掉' if later else '之后也没被打掉'))
            # 不写间隔几秒（用户写作规则：不写秒数间隔）：短时间内冲出来连杀写“几秒里”，否则写“接连”
            quick = dur <= th['eco_burst_s']
            txt = f"{kn}在{kp}一个人{'几秒里连杀' if quick else '接连打死'}{'、'.join(vics)}，{end}"
            facts.update(killer_place=kp, ending=end)
        elif 'c' in var:
            vp = C.place_cn(ks[0]['vplace'])
            kp = C.place_cn(ks[-1]['aplace'])
            txt = f"下包后回防，{'、'.join(vics)}先后从{vp}一个个过去，都被{kp}的 {kn} 打死"   # 名字两边的空格由 _tidy 补
            if C.spots(ctx, ks[-1]['vic'], K, ks[-1]['tick'] - int(th['c_saw_s'] * tr), ks[-1]['tick']) is None:
                txt += '，第二个人倒下前没看到他'
            facts.update(victim_place=vp, killer_place=kp)
        else:
            vp = C.place_cn(ks[0]['vplace'])
            nades = sum(_nades_at(ctx, k['vic'], k['tick'] - 1) for k in ks)
            vps = [C.place_cn(k['vplace']) for k in ks]
            if all(x == vp for x in vps):
                # 凶手写出名字（主要原因要有谁，128 R15 / bd R13：“同一个人”是谁页面上哪里都没写）
                txt = (f"第 {_ts(ctx, t0, rnd)} 秒{vics[0]}在{vp}倒下后，{'、'.join(vics[1:])}又从同一个地方出去，"
                       f"几秒内都被 {kn} 一个人打死，中间没人往那边扔闪或烟")
            else:
                # 报点不一样（635 R9：下水道和 B 小）：不写“同一个地方”，各写各的倒下位置
                rest = '、'.join(f'{v}在{p}' for v, p in zip(vics[1:], vps[1:]))
                txt = (f"第 {_ts(ctx, t0, rnd)} 秒{vics[0]}在{vp}倒下后，{rest}也被 {kn} 打死，"
                       f"几秒内连着倒下，中间没人往那边扔闪或烟")
            if nades:
                txt += f'；倒下的人身上还有 {nades} 颗闪或烟没扔出去'
            facts.update(victim_place=vp, nades=nades, victim_places=vps)
        place = ks[0]['vplace']
        if 'c' in var or (side == 'CT' and _plant(ctx, rnd) and t0 > _plant(ctx, rnd)[0]):
            alt = '还可以两个人一起出，或者一个人先扔闪、另一个跟着进，先补烟再补人'
            kb = _kb([], 'CT', place)
        elif side == 'T':
            alt = '还可以两个人一组进，前面的人一交火后面的人马上补，或者先往那边补一颗闪或烟再出'
            kb = _kb([], 'T', place)
        else:
            # CT 下包前：rules_v1 只给了 T 方和 CT 回防两支；CT 一样是一个接一个出去被打掉，取 CT 那一支，
            # 不然时刻是“问题”却没有“更好的做法”（bd R15、128 R15、461 R18 等）
            alt = '还可以两个人一起出，或者一个人先扔闪、另一个跟着进，先补烟再补人'
            kb = _kb([], 'CT', place)
            facts['alt_ct_pre'] = True                # merge：这句只是补上的团队建议，不拿它去挤掉你自己的“还可以”（d5a R12）
        if burst:
            alt, kb = '', []                          # 对面冲出来连杀：合理代价，不给建议
        blame = '合理代价' if burst else '队友或团队'
        ev = f'{kn} 这一串连着打死 {len(ks)} 个，' + ('都发生在他冲出来的几秒里' if burst else
                                                 ('中间我方没把他补掉' if not fixed else '最后一杀之后才被补掉'))
        facts['ev_n'] = len(ks)
        ud = C.death_tick(ctx, U, rnd)
        hits.append(_hit('TEAM-03', '问题', rnd, t0, ctx, U, None, blame, txt, alt, kb, ev, facts,
                         death_t=_t(ctx, ud, rnd) if user_in and ud else None,
                         death_of=C.name_of(ctx, U) if user_in else None,
                         merge_key=f'streak:{K}:{t0}'))
    return hits


def _eco_hunt(ctx, U, rnd):
    """decisions #24：eco 局对面已经在一个地方杀过人，你还去找他被打死 → TEAM-03 的"思路"（回合时间够才提）"""
    th = _th(ctx)
    tr = _tr(ctx)
    if not _my_eco(ctx, rnd):
        return []
    T = C.death_tick(ctx, U, rnd)
    if T is None:
        return []
    mk = next((k for k in _kills(ctx, rnd) if k['vic'] == U and k['tick'] == T), None)
    if mk is None:
        return []
    K = mk['att']
    tm = C.team_of(ctx, U, rnd)
    before = [k for k in _kills(ctx, rnd) if k['att'] == K and C.team_of(ctx, k['vic'], rnd) == tm
              and k['tick'] <= T - th['eco_hunt_gap_s'] * tr]
    if not before:
        return []
    kpos = C.pos(ctx, K, T)
    if min(C.dist3(kpos, k['apos']) for k in before) > th['eco_hunt_r']:
        return []
    tc = _contact_start(ctx, U, K, T)
    mv, _ = _moving(ctx, U, tc)
    t_now = C.t_of(ctx, T, rnd)
    left = th['round_time_s'] - t_now
    if not mv or left < th['eco_hunt_min_left_s']:
        return []
    wcls = C.weapon_class(mk['wk'])
    if C.gun_tier(mk['wk']) >= 2:                     # 凶手本来就拿长枪：这条建议不适用
        return []
    kn = C.name_of(ctx, K)
    kp = C.place_cn(before[0]['aplace'])
    myp = C.place_cn(mk['vplace'])
    vics = [_nm(ctx, k['vic'], U) for k in before]
    txt = (f"{kn} 第 {_ts(ctx, before[0]['tick'], rnd)} 秒起已经在{kp}打死了{'、'.join(vics)}，"
           f"第 {_ts(ctx, T, rnd)} 秒你在{myp}又碰上他，被他打死")
    rw = th['kill_reward']
    kw = C.weapon_cn(mk['wk'])
    alt = '还可以在 eco 感觉没机会时，换个方向去找对面拿步枪或 AWP 的人：打掉一个就能捡起一把长枪'
    facts = dict(killer=kn, killer_weapon=C.weapon_cn(mk['wk']), killer_place=kp, victims=vics, n=len(before),
                 t0=_t(ctx, before[0]['tick'], rnd), death_t=_t(ctx, T, rnd), place=myp, time_left=C.sec(left))
    # decisions #24：句子按凶手的武器写，带上赏金的理由
    if C.wkey(mk['wk']) == 'p90':
        alt += (f"；{kn} 拿的是 {kw}，被它打死对面拿 {C.money(rw['p90'])}，和被步枪打死一样，"
                f"打掉他却只能捡到一把 {kw}")
        facts.update(reward_p90=rw['p90'], reward_rifle=rw['步枪'])
    elif wcls == '冲锋枪':
        alt += (f"；{kn} 拿的是 {kw}，被冲锋枪打死对面拿 {C.money(rw['冲锋枪'])}，"
                f"被步枪打死只拿 {C.money(rw['步枪'])}、被 AWP 只拿 {C.money(rw['狙'])}")
        facts.update(reward_smg=rw['冲锋枪'], reward_rifle=rw['步枪'], reward_awp=rw['狙'])
    # 证据按凶手实际的移动写：杀完人后往你这边推过来 / 一直守在那一片
    kp_now = C.place_cn_at(ctx, K, T)
    moved = C.path_len(ctx, K, before[-1]['tick'], T) > th['eco_hunt_move']
    if moved and kp_now != kp:
        ev = f"{kn} 在{kp}杀完人后往{kp_now}推了过来，你碰上他时回合时间还很多"
        facts.update(killer_place_now=kp_now)
    else:
        ev = f"{kn} 一直守在{kp}这一片，你碰上他时回合时间还很多"
    return [_hit('TEAM-03', '思路', rnd, T, ctx, U, U, '合理代价', txt, alt, [], ev, facts,
                 death_t=_t(ctx, T, rnd), death_of=C.name_of(ctx, U), merge_key=None)]


# ================================================================ TEAM-07 反 eco 单走送枪（decisions #3）
def _team07(ctx, U, rnd):
    th = _th(ctx)
    tr = _tr(ctx)
    if not _anti_eco(ctx, rnd):
        return []
    tm = C.team_of(ctx, U, rnd)
    side = C.user_side(ctx, rnd)
    pl = _plant(ctx, rnd)
    start = C.round_info(ctx, rnd)['start']
    hits = []
    for k in _kills(ctx, rnd):
        V, K, T = k['vic'], k['att'], k['tick']
        if C.team_of(ctx, V, rnd) != tm or C.weapon_class(k['wk']) != '手枪':
            continue
        w = C.weapon_at(ctx, V, T - 1)
        if not C.is_gun(w) or C.weapon_class(w) == '手枪':
            continue
        us, them = C.alive_at_death(ctx, V, rnd)
        mates = _alive_mates(ctx, rnd, V, T)
        if us < them or not mates:
            continue
        # (1) 单走：在动 + 推进（离开本方常规位、越走越远）+ 没有队友在配合位
        tc = _contact_start(ctx, V, K, T)
        mv, spd = _moving(ctx, V, tc)
        place = C.place_at(ctx, V, T - 1)
        own = set(th['own_places'][side])
        if side == 'T' and pl and pl[0] < T and pl[1]:
            own.add(pl[1])
        if not mv or place is None or place in own or C.place_cn(place, None) is None:
            continue
        spawn = C.pos(ctx, V, start)
        p_now, p_then = C.pos(ctx, V, T - 1), C.pos(ctx, V, T - 1 - int(th['push_window_s'] * tr))
        adv = C.dist2(p_now, spawn) - C.dist2(p_then, spawn)
        if adv < th['push_advance']:
            continue
        coord = set(th['coord_places'].get(place, [place])) | {place}
        # 用户拍板（R15）：守在他刚走过来的那片、看着出口的队友不算配合——他在里面打起来，这些人补不到
        back = {C.place_at(ctx, V, T - int(s * tr)) for s in range(1, int(th['coord_back_s']) + 1)} - {place, None}
        mplace = {m: C.place_at(ctx, m, T - int(th['coord_lag_ticks'])) for m in mates}
        partners = [m for m in mates if mplace[m] in coord and mplace[m] not in back]
        if partners:
            continue
        behind = sorted({C.place_cn(mplace[m]) for m in mates if mplace[m] in coord and mplace[m] in back})
        # (2) 没人能补：凶手死前 2 秒到死后 3 秒没被活着的队友看见，2 秒内没被补掉
        lo, hi = T - int(th['cover_before_s'] * tr), T + int(th['cover_after_s'] * tr)
        if any(C.spots(ctx, m, K, lo, hi) is not None for m in mates):
            continue
        if _killed_by_team(ctx, rnd, K, V, T, T + th['fast_trade_s'] * tr):
            continue
        # 后果：枪被对面捡走、后来又杀了几个
        picker, later, ptick = None, 0, None
        pk = _pickups(ctx)
        if pk is not None and len(pk):
            wk = C.wkey(w)
            cand = pk[(pk['tick'] > T) & (pk['tick'] <= T + int(th['pickup_s'] * tr)) & (pk['item'].map(C.wkey) == wk)]
            cand = cand[[C.is_enemy(ctx, s, V, rnd) for s in cand['sid']]] if len(cand) else cand
            if len(cand) and 'user_X' in cand:
                d = np.sqrt((cand['user_X'] - p_now[0]) ** 2 + (cand['user_Y'] - p_now[1]) ** 2 + (cand['user_Z'] - p_now[2]) ** 2)
                cand = cand[d.to_numpy() <= th['pickup_r']]
            if len(cand):
                picker, ptick = cand['sid'].iloc[0], int(cand['tick'].iloc[0])
                later = sum(1 for x in _kills(ctx, rnd) if x['att'] == picker and x['tick'] > ptick and x['wk'] == wk)
        vn = _nm(ctx, V, U)
        moved_in = C.place_at(ctx, V, T - int(th['moved_in_s'] * tr)) != place
        pcn = C.place_cn(place)
        txt = (f"对面 eco，{vn}拿着{C.weapon_cn(w)}一个人{'走进' if moved_in else '在'}{pcn}，被手枪打死，"
               f"身边没有队友，也没有队友看得到凶手")
        if picker and later:
            txt += f'；这把枪被{C.name_of(ctx, picker)}捡走，后来又用它杀了 {later} 个人'
        elif picker:
            txt += f'；这把枪被{C.name_of(ctx, picker)}捡走了'
        alt = '还可以反 eco 时跟队友站在一起，憋在队友能补到的位置，等手枪自己走进长枪的距离，不单独找人头；架不住的口子可以和队友一蹲一站一起守'
        kb = _kb([], side, place)
        mate_places = sorted({C.place_cn(C.place_at(ctx, m, T - int(th['coord_lag_ticks']))) for m in mates})
        if behind:
            ev = (f"那时队友在{'、'.join(mate_places)}；{'、'.join(behind)}是{'你' if V == U else '他'}刚走过来的地方，"
                  f"守在那里只看得到出口，{pcn}里打起来补不到；{C.name_of(ctx, K)} 用")
        else:
            ev = f"那时队友在{'、'.join(mate_places)}，没有人在{pcn}旁边的口子；{C.name_of(ctx, K)} 用"
        ev += f"{C.weapon_cn(k['wk'])}打死他前后，没有队友看到{C.name_of(ctx, K)}"
        facts = dict(victim=C.name_of(ctx, V), weapon=C.weapon_cn(w), killer=C.name_of(ctx, K), killer_weapon=C.weapon_cn(k['wk']),
                     place=pcn, moved_in=moved_in, us=us, them=them, mate_places=mate_places, mates_behind=behind,
                     picker=C.name_of(ctx, picker) if picker else None, later_kills=later,
                     contact_t=_t(ctx, tc, rnd), death_t=_t(ctx, T, rnd))
        blame = '你的责任' if V == U else '队友或团队'
        hits.append(_hit('TEAM-07', '问题', rnd, T, ctx, U, V, blame, txt, alt, kb, ev, facts,
                         death_t=_t(ctx, T, rnd), death_of=C.name_of(ctx, V), merge_key=f'gift:{V}:{T}'))
    return hits


# ================================================================ TEAM-02 输的回合人数先丢在谁身上
def _team02(ctx, U, rnd, gifts, events):
    th = _th(ctx)
    tm = C.team_of(ctx, U, rnd)
    seq = []                                          # (tick, vic, us, them) 每次阵亡后的人数
    for tick, vic in _all_deaths(ctx, rnd):
        us, them = _after(ctx, tick, U)
        seq.append((tick, vic, us, them))
    if not seq:
        return []
    def _seg(start):
        fd, le = None, None
        for i in range(start, len(seq)):
            us, them = seq[i][2], seq[i][3]
            if us >= them:
                le = i
            if them - us >= th['deficit_n']:
                fd = i
                break
        if fd is not None and seq[fd][2] == 0:
            fd = None                                 # 第 4 条：落后 2 个要到我方全灭才出现，不算人数先丢
        return fd, le
    first_def, last_even = _seg(0)
    # 落后之后又打回平局、后来再落后（8d R17：3v5 → 打回 3v3 → 被拱门连杀成 1v3）：输在最后那一段，不是一开始那段
    rebound = None
    while first_def is not None:
        back = next((j for j in range(first_def + 1, len(seq)) if seq[j][2] >= seq[j][3] and seq[j][2] > 0), None)
        if back is None:
            break
        fd2, le2 = _seg(back)
        if fd2 is None:
            break
        rebound, first_def, last_even = back, fd2, le2
    lo = seq[last_even][0] if last_even is not None else -1
    # “最后一次不少于对面”那一刻如果正是你倒下、把领先打成平（5v4 → 4v4），这一段从你倒下算起：
    # 领先是在你这次死上丢的，不能从平局开始数、把你漏掉（e6b R16）
    gave_lead = (last_even is not None and seq[last_even][1] == U and last_even > 0
                 and seq[last_even - 1][2] > seq[last_even - 1][3])
    if gave_lead:
        lo = seq[last_even][0] - 1
    hi = seq[first_def][0] if first_def is not None else None
    lost = [(tick, vic) for tick, vic, *_ in seq if hi is not None and lo < tick <= hi and C.team_of(ctx, vic, rnd) == tm]
    ud = C.death_tick(ctx, U, rnd)
    # 你成了最后一个人时对面还有几个
    clutch = None
    mates_dead = [tick for tick, vic, *_ in seq if C.team_of(ctx, vic, rnd) == tm and vic != U]
    if len(mates_dead) == len(C.mates(ctx, rnd)) and mates_dead and (ud is None or ud > max(mates_dead)):
        us, them = _after(ctx, max(mates_dead), U)
        if them >= _th(ctx)['clutch_min_enemies']:
            clutch = them
    tick0 = hi if hi is not None else (ud if ud is not None else C.round_info(ctx, rnd)['end'])
    us0, them0 = (seq[first_def][2], seq[first_def][3]) if first_def is not None else _after(ctx, tick0, U)
    st_in = [e for e in events if hi is not None and e['kills'][0]['tick'] <= hi <= e['kills'][-1]['tick']]
    eco = _my_eco(ctx, rnd)
    facts = dict(us=us0, them=them0, t=_t(ctx, tick0, rnd), clutch_n=clutch)
    subj = None
    if gifts:
        g = gifts[0]
        why = 'gift'
        gv = C.sid_of(ctx, g['facts']['victim'])
        blame = '你的责任' if gv == U else '队友或团队'
        txt = '反 eco 有人单走被手枪打死、送出了枪'
        subj = gv
        facts.update(gifter=g['facts']['victim'])
    elif eco:
        why = 'eco'
        bursts = [e for e in events if 'a' in e['variants']]
        burst = [e for e in bursts if (e['kills'][-1]['tick'] - e['kills'][0]['tick']) / _tr(ctx) <= th['eco_burst_s']]
        tb = {'全起': '长枪', '强起': '强起', '半起': '半起'}.get(C.buy(ctx, rnd, 'them'), '长枪')
        txt = f'eco 打对面{tb}，装备差太多'
        if burst:
            e = burst[0]
            txt += f"；{C.name_of(ctx, e['killer'])} 冲出来几秒里连杀了 {len(e['kills'])} 个"
            facts.update(killer=C.name_of(ctx, e['killer']), n=len(e['kills']))
        blame = '合理代价' if (burst or not bursts) else '队友或团队'
    elif st_in:
        why = 'streak'
        e = st_in[0]
        txt = f"{C.name_of(ctx, e['killer'])}一个人连杀打出人数"
        blame = '队友或团队'
        facts.update(killer=C.name_of(ctx, e['killer']), user_in=U in {k['vic'] for k in e['kills']})
    elif hi is not None:
        why = 'deficit'
        vs = {v for _, v in lost}
        blame = '队友或团队' if U not in vs else ('你的责任' if vs == {U} else '你的责任 + 队友或团队')
        names = '、'.join(f"{_nm(ctx, v, U)}（{C.place_cn(C.place_at(ctx, v, t - 1))}）" for t, v in lost)
        txt = f"第 {_ts(ctx, tick0, rnd)} 秒变成 {C.alive_str(us0, them0)}，这一段先倒下的是{names}"
        facts.update(lost=[_nm(ctx, v, U) for _, v in lost], lost_t=[_t(ctx, t, rnd) for t, _ in lost])
        # 这一段倒下的人都是同一个人打掉的：写出来（8d R22：队友D、队友A 都倒在 对手3 的 AWP 下）
        kd = C.deaths(ctx, rnd)
        ks = [kd[(kd['vic'] == v) & (kd['tick'] == t)] for t, v in lost]
        katt = {str(k['att'].iloc[0]) for k in ks if len(k)}
        if len(lost) >= 2 and len(katt) == 1 and all(len(k) for k in ks) and next(iter(katt)) in C.enemies(ctx, rnd):
            kn = C.name_of(ctx, next(iter(katt)))
            awp = all(C.wkey(k['wk'].iloc[0]) == 'awp' for k in ks)
            txt += f"，都被 {kn}{' 的 AWP' if awp else ''}打掉"
            facts.update(seg_killer=kn, seg_killer_awp=awp)
        if rebound is not None:
            rb_us, rb_them = seq[rebound][2], seq[rebound][3]
            facts.update(rebound=C.alive_str(rb_us, rb_them), rebound_t=_t(ctx, seq[rebound][0], rnd))
        # 你在这一段之前就先倒下了（5v5 时第一个倒、队友后来打回平局）：主要原因要先写这一句，
        # 标题也不能写“队友先倒下”（e6b 高分玩家84 R19）
        if ud is not None and last_even is not None and ud < lo and U not in vs:
            ev_us, ev_them = seq[last_even][2], seq[last_even][3]
            # 写你倒下的那一秒，不写“你先倒下”：你前面可能还有队友先倒（128 R18：高分玩家19 8 秒、你 23.7 秒）
            txt = f"第 {_ts(ctx, ud, rnd)} 秒你倒下后队友打回 {C.alive_str(ev_us, ev_them)}，" + txt
            facts.update(user_died_before_segment=True, even=C.alive_str(ev_us, ev_them), user_death_t=_t(ctx, ud, rnd))
        elif rebound is not None:
            txt = f"第 {_ts(ctx, seq[rebound][0], rnd)} 秒打回 {facts['rebound']} 后，" + txt
        if U in vs:
            # 你的部分要用：你是这一段倒下的人之一（第几秒、在哪、被谁）
            ut = next(t for t, v in lost if v == U)
            krow = C.deaths(ctx, rnd)
            krow = krow[(krow['vic'] == U) & (krow['tick'] == ut)]
            ukn = C.name_of(ctx, krow['att'].iloc[0]) if len(krow) and krow['att'].iloc[0] in C.enemies(ctx, rnd) else None
            # 这一段之前已经落后过（3v4 被扳回 3v3 后你倒下，8d R15）：不能写“人数从这里开始落后”
            behind_before = last_even is not None and any(x[3] > x[2] for x in seq[:last_even])
            facts['user_lost'] = dict(t=_t(ctx, ut, rnd), place=C.place_cn(C.place_at(ctx, U, ut - 1)), killer=ukn,
                                      first=lost[0][1] == U, gave_lead=bool(gave_lead and lost[0][1] == U),
                                      behind_before=bool(behind_before))
        subj = U if U in vs else (lost[0][1] if lost else None)
    elif clutch:
        why = 'clutch'
        blame = '队友或团队'
        # 写出谁、第几秒、在哪倒下（8d R13：22.1–40.5 秒 队友B、队友A、队友D、队友C 先后倒在 A 点）
        md = [(t, v, C.place_cn(C.place_at(ctx, v, t - 1))) for t, v, *_ in seq if C.team_of(ctx, v, rnd) == tm and v != U]
        t0s, t1s = _ts(ctx, md[0][0], rnd), _ts(ctx, md[-1][0], rnd)
        span = f'第 {t0s} 秒' if len(md) == 1 or t0s == t1s else f'第 {t0s} 秒到第 {t1s} 秒'
        places = {p for _, _, p in md}
        if len(places) == 1:
            who = '、'.join(C.name_of(ctx, v) for _, v, _ in md)
            txt = (f"{span} {who}{'先后' if len(md) > 1 else ''}倒在{md[0][2]}，留你一个人打 1v{clutch}")
        else:
            who = '、'.join(f'{C.name_of(ctx, v)}（{p}）' for _, v, p in md)
            txt = f"{span} {who}{'先后' if len(md) > 1 else ''}倒下，留你一个人打 1v{clutch}"
        facts.update(lost=[C.name_of(ctx, v) for _, v, _ in md], lost_t=[_t(ctx, t, rnd) for t, _, _ in md])
    elif events:
        # 人数没落后到 2 个，但有人连杀把我们一个个打掉（TEAM-03 同一件事）：按连杀写，不写“人数没拉开”
        why = 'streak'
        e = events[0]
        txt = f"{C.name_of(ctx, e['killer'])}一个人连杀了 {len(e['kills'])} 个"
        blame = '队友或团队'
        facts.update(killer=C.name_of(ctx, e['killer']), n=len(e['kills']), user_in=U in {k['vic'] for k in e['kills']})
    else:
        why = 'close'
        a, b = (seq[last_even][2], seq[last_even][3]) if last_even is not None else (len(C.mates(ctx, rnd, include_self=True)), len(C.enemies(ctx, rnd)))
        end_tick = seq[last_even][0] if last_even is not None else C.round_info(ctx, rnd)['start']
        died_before = ud is not None and ud <= end_tick
        blame = '队友或团队' if died_before else '你的责任 + 队友或团队'
        # 中间领先过 2 个以上的，“人数一直没拉开”不对：写领先到几v几、后来被追回
        lead = max(seq, key=lambda x: (x[2] - x[3], -x[0]))
        if lead[2] - lead[3] >= th['deficit_n']:
            txt = f'人数一度打到 {C.alive_str(lead[2], lead[3])}，后来被追回来，最后 {C.alive_str(a, b)} 的残局输了'
            facts.update(lead=C.alive_str(lead[2], lead[3]))
        else:
            txt = f'人数一直没拉开，最后 {C.alive_str(a, b)} 的残局输了'
        # 写出谁、第几秒、在哪倒下输掉残局（我方最后一个倒下的人），主要原因要具体
        ours = [(tk_, v) for tk_, v in _all_deaths(ctx, rnd) if C.team_of(ctx, v, rnd) == tm]
        if ours:
            lt, lv = ours[-1]
            krow = C.deaths(ctx, rnd)
            krow = krow[(krow['vic'] == lv) & (krow['tick'] == lt)]
            kn = C.name_of(ctx, krow['att'].iloc[0]) if len(krow) and krow['att'].iloc[0] in C.enemies(ctx, rnd) else None
            lp = C.place_cn(C.place_at(ctx, lv, lt - 1))
            txt += (f"：第 {_ts(ctx, lt, rnd)} 秒{_nm(ctx, lv, U)}在{lp}" + (f'被 {kn} 打死' if kn else '倒下'))
            facts.update(last_death_t=_t(ctx, lt, rnd), last_death=_nm(ctx, lv, U), last_killer=kn)
            # 他倒下后还有人活着打完这个残局：写出最后是谁、回合怎么结束的（bd R9：1v1 是 队友C 打的，对手1（第4场） 拆掉了包）
            dead = {v for _, v in ours}
            left = [x for x in C.mates(ctx, rnd, include_self=True) if x not in dead]
            if left:
                facts.update(survivors=[_nm(ctx, x, U) for x in left])
                ln = '、'.join(_nm(ctx, x, U) for x in left)
                txt += ('后只剩' if ln.startswith('你') else '后只剩 ') + ln
                df = _defuse(ctx, rnd)
                reason = (C.tl_round(ctx, rnd) or {}).get('reason') or ''
                if df is not None and df[1]:
                    txt += f'，{C.name_of(ctx, df[1])} 第 {_ts(ctx, df[0], rnd)} 秒拆掉了包'
                    facts.update(defuse_t=_t(ctx, df[0], rnd))
                elif reason == 'bomb_exploded':
                    txt += '，最后包炸了'
                elif reason in ('target_saved', 'round_time_expired', 'time_ran_out'):
                    txt += '，最后时间到了'
        facts.update(endgame=C.alive_str(a, b), endgame_us=a, endgame_them=b, user_died_before=died_before)
        subj = U if not died_before else None
    if why != 'clutch' and clutch:
        txt += f'；最后留你一个人打 1v{clutch}'
    facts['why'] = why
    if subj is None and blame.startswith('你'):
        subj = U
    ev = {'gift': '对面 eco，我方先送出了一把长枪', 'eco': '我方 eco，对面长枪',
          'streak': '人数是被同一个人连着打掉的', 'deficit': '人数从这一段开始落后',
          'clutch': '队友倒下后只剩你一个人', 'close': '双方人数一直咬得很紧'}[why]
    return [_hit('TEAM-02', '判定', rnd, tick0, ctx, U, subj, blame, txt, '', [], ev, facts, scope='round',
                 death_t=_t(ctx, ud, rnd) if ud else None, death_of=C.name_of(ctx, U) if ud else None)]


# ================================================================ 豁免记录（给集成：这些阵亡不出本模块的问题，交给对枪动作写）
def _exempt(ctx, rule, rnd, tick, reason, **extra):
    """记一条“本来会判、按审核单豁免掉”的阵亡 / 挪动：ctx._team_exempt（team.exemptions(ctx) 读）"""
    lst = getattr(ctx, '_team_exempt', None)
    if lst is None:
        lst = []
        setattr(ctx, '_team_exempt', lst)
    lst.append(dict(rule=rule, round=int(rnd), tick=int(tick), t=_t(ctx, tick, rnd), reason=reason,
                    describe='duel' if rule == 'TEAM-04' else 'none', **C.plain(extra)))


def exemptions(ctx):
    """run(ctx) 之后可读：[{rule, round, tick, t, reason, describe, ...}]，按 (round, tick, rule) 排。
    rule=TEAM-04：你的这次阵亡本来会判“一个人先露头”，按审核单第 7 / 17 条不判，describe='duel' = 按对枪动作写（shot 的判定）；
    reason：open 开局控图 / smoke 隔烟 / solo 单走一侧后出 / traded 进点被换 / cost 我方 eco 或强起打长枪 / far 远处打平 / ct_post CT 守位。
    rule=TEAM-06：你走开有意图（reason=补位/补防/堵路/捡枪/回防/两路夹/下包），不记走散；rule=TEAM-05 reason=heard_only：只听得到枪声不再判。"""
    return sorted(getattr(ctx, '_team_exempt', None) or [], key=lambda e: (e['round'], e['tick'], e['rule']))


# ================================================================ 进点（审核单第 28 条，手册 T-E）
def _first_in(ctx, sid, rnd, places, a, b):
    """[a, b] 里 sid 活着、报点在 places 里的第一帧 tick（每 contact_step_ticks 查一次），没有 None"""
    st = int(_th(ctx)['contact_step_ticks'])
    for tk in range(int(a), int(b) + 1, st):
        if C.alive(ctx, sid, tk, rnd) and C.place_at(ctx, sid, tk) in places:
            return tk
    return None


def _site_of(ctx, place):
    for s, pl in sorted(_th(ctx)['entry_places'].items()):
        if place in pl:
            return s
    return None


def _close_util(ctx):
    """我方能用在进点上的近点道具起爆：闪、火 / 燃烧弹。[(tick, sid, xyz)]，按 tick 排"""
    m = _memo(ctx)
    if 'cutil' in m:
        return m['cutil']
    out = []
    for name in ('daoju', 'sight', 'sight_s4', 'ticks'):
        try:
            raw = ctx.cache(name)
        except (FileNotFoundError, KeyError, OSError):
            continue
        ev = raw.get('ev') if isinstance(raw, dict) else None
        if not isinstance(ev, dict):
            continue
        got = False
        for evn in ('flashbang_detonate', 'inferno_startburn'):
            df = ev.get(evn)
            if df is None or not len(df):
                continue
            got = True
            for r in df.itertuples():
                s = r.user_steamid
                sid = ('' if s != s else str(int(s))) if isinstance(s, float) else str(s)
                out.append((int(r.tick), sid, np.array([r.x, r.y, r.z], float)))
        if got:
            break
    if not out:
        for evn in ('ev_flashbang_detonate', 'ev_inferno_startburn'):
            try:
                df = ctx.cache(evn, dtype={'user_steamid': str})
            except (FileNotFoundError, KeyError, OSError):
                continue
            for r in df.itertuples():
                out.append((int(r.tick), str(r.user_steamid), np.array([r.x, r.y, r.z], float)))
    out.sort(key=lambda u: (u[0], u[1]))
    m['cutil'] = out
    return out


def _entry_no_follow(ctx, U, rnd, V, K, T):
    """V（T 方）是本队第一个进这个包点的人、倒在这里，凶手 entry_follow_s 秒内没被补，也没有队友跟进同一个包点。
    成立返回 dict(site, place, followers, util_n, enter_tick, death_tick, killer, victim)，否则 None"""
    th, tr = _th(ctx), _tr(ctx)
    if C.side_of(ctx, V, rnd) != 'T':
        return None
    pl = _plant(ctx, rnd)
    if pl and pl[0] <= T:
        return None
    if C.t_of(ctx, T, rnd) < th['entry_from_s']:
        return None
    site = _site_of(ctx, C.place_at(ctx, V, T - 1))
    if site is None:
        return None
    places = set(th['entry_places'][site])
    start = C.round_info(ctx, rnd)['start']
    fv = _first_in(ctx, V, rnd, places, start, T - 1)
    if fv is None:
        return None
    # 复核修正：我方 eco / 强起打长枪（同 TEAM-04 ⑤）、或者已经少 entry_behind_n 个人以上时，不按“后面的人没跟上”定责
    # （8d 对手2 R2：eco 3v5 时 对手3 一个人进 A）
    if _my_eco(ctx, rnd) or (C.buy(ctx, rnd, 'us') in th['t04_cost_us'] and C.buy(ctx, rnd, 'them') in th['t04_cost_them']):
        return None
    us_t, them_t = C.alive_at_death(ctx, V, rnd)
    if them_t - us_t >= th['entry_behind_n']:
        return None
    fol = int(th['entry_follow_s'] * tr)
    if _killed_by_team(ctx, rnd, K, V, T, T + fol):
        return None
    mates = [m for m in C.mates(ctx, rnd, V)]
    # 第一个进：队友在他之前没进过这个包点；跟上：他进点后到他倒下 + entry_follow_s 里有队友进了同一个包点
    for m in mates:
        if _first_in(ctx, m, rnd, places, start, T + fol) is not None:
            return None
    vp = C.pos(ctx, V, T - 1)
    followers = [m for m in mates if C.alive(ctx, m, T, rnd) and C.dist3(C.pos(ctx, m, T), vp) <= th['entry_follow_r']]
    if len(followers) < th['entry_min_followers']:
        return None                                   # 后面没有一组人：不是一起打进点（例 635 R16：A 点只剩他一个、你在中路）
    sxy = th['entry_site_xy'][site]
    lo = fv - int(th['entry_util_back_s'] * tr)
    tm = C.team_of(ctx, V, rnd)
    n_util = sum(1 for tk, sid, xyz in _close_util(ctx)
                 if lo <= tk <= T and sid and C.team_of(ctx, sid, rnd) == tm
                 and (C.dist3(xyz, vp) <= th['entry_util_r'] or C.dist2(xyz, sxy) <= th['entry_util_r']))
    return dict(site=site, place=C.place_at(ctx, V, T - 1), followers=sorted(followers), util_n=n_util,
                enter_tick=fv, death_tick=T, killer=K, victim=V)


def _entry_alt(ctx, ent):
    if ent['util_n'] == 0:
        return '还可以进点前先往点里给一颗闪或火，第一个人进去时后面的人贴着跟上，前面一交火马上补'
    return '还可以第一个人进点时后面的人贴着跟上，前面一交火马上补'


def _entry_hit(ctx, U, rnd, ent):
    """你第一个进点倒下、后面的人没及时跟上：TEAM-04 facts.variant=entry，记团队"""
    T = ent['death_tick']
    sc = C.place_cn(f"Bombsite{ent['site']}", f"{ent['site']} 点")
    fn = '、'.join(C.name_of(ctx, m) for m in ent['followers'])
    kn = C.name_of(ctx, ent['killer'])
    txt = f"第 {_ts(ctx, T, rnd)} 秒你第一个进{sc}，被 {kn} 打死，后面的 {fn} 没有及时跟上进点，也没补掉他"
    if ent['util_n'] == 0:
        txt += '；进点前全队没往点里扔闪或火'
    ev = f"你倒下后好几秒都没有队友进{sc}，{kn} 也没被补掉"
    us, them = C.alive_at_death(ctx, U, rnd)
    facts = dict(variant='entry', death_t=_t(ctx, T, rnd), enter_t=_t(ctx, ent['enter_tick'], rnd), killer=kn, site=sc,
                 place=C.place_cn(ent['place']), followers=[C.name_of(ctx, m) for m in ent['followers']],
                 team_util=ent['util_n'], us=us, them=them, near=True,
                 clause=f'你第一个进{sc}，后面的人没有及时跟上')
    return _hit('TEAM-04', '问题', rnd, T, ctx, U, None, '队友或团队', txt, _entry_alt(ctx, ent),
                _kb([], 'T', ent['place']), ev, facts, death_t=_t(ctx, T, rnd), death_of=C.name_of(ctx, U),
                merge_key=f'entry:{T}')


# ================================================================ TEAM-04 豁免（审核单第 7、17 条，手册 POS-G）
def _t_home(ctx, rnd, sid):
    """回合开始时 sid 的对手五个人站位的中心（“往对面那边走”用）"""
    start = C.round_info(ctx, rnd)['start']
    foes = C.enemies(ctx, rnd, sid)
    return np.mean([C.pos(ctx, e, start) for e in foes], axis=0) if foes else None


def _t04_exempt_reason(ctx, U, rnd, K, T, tc):
    """TEAM-04 不判的理由（按手册 POS-G 的顺序取第一条），都不成立返回 None"""
    th, tr = _th(ctx), _tr(ctx)
    side = C.user_side(ctx, rnd)
    start = C.round_info(ctx, rnd)['start']
    place = C.place_at(ctx, U, tc)
    # ① 开局控图第一个对上：开局 t04_open_s 秒内、本回合还没人倒下、你在常规控图位
    deaths = _all_deaths(ctx, rnd)
    # 复核修正：同一场开局对枪里你自己打死的人、接触之后才倒下的人不算“已经有人倒下”（e65 R10：高分玩家04 在下水道
    # 先打死 高分玩家70、0.2 秒后被 高分玩家73 打死，仍是开局控图第一个对上）
    kd = C.deaths(ctx, rnd)
    by_me = {(int(x.tick), x.vic) for x in kd.itertuples() if x.att == U}
    prior = [d for d in deaths if d[0] < tc and d not in by_me]
    if (C.t_of(ctx, tc, rnd) < th['t04_open_s'] and not prior
            and place in set(th['t04_open_places'].get(side, []))):
        return 'open'
    # ② 隔烟对喷
    krow = C.deaths(ctx, rnd)
    krow = krow[(krow['vic'] == U) & (krow['tick'] == T)]
    if len(krow) and 'thrusmoke' in krow.columns and bool(krow['thrusmoke'].iloc[0]):
        return 'smoke'
    # 击杀栏没标隔烟时，用 C-SMOKE：他这次打中你的每一枪都隔着烟
    hk = C.hurts(ctx, rnd)
    hk = hk[(hk['vic'] == U) & (hk['att'] == K) & (hk['tick'] <= T) & (hk['tick'] >= tc - int(th['see_lead_s'] * tr))]
    if len(hk) and all(C.smoked(ctx, U, K, int(tk)) for tk in hk['tick']):
        return 'smoke'
    # ⑦（先于③判，理由更具体）CT 在自己守位上架枪 / 对峙时被拉出来打：没下包、在本方守位报点、接触前没往 T 方走近 >= t04_ct_fwd
    if side == 'CT':
        pl = _plant(ctx, rnd)
        if not (pl and pl[0] <= tc) and place in set(th['own_places']['CT']):
            home = _t_home(ctx, rnd, U)
            a = max(start, tc - int(th['t04_ct_fwd_s'] * tr))
            # 守位上 = 接触前这几秒一直在同一个报点里（守着这个位置）；或者一直在本方守位、同一片区域里，没往 T 方走近 >= t04_ct_fwd。
            # 从别的区域走过来、或往 T 方顶出去找人的，不算守位（照判身后有没有人）
            st = int(th['contact_step_ticks'])
            pls = [C.place_at(ctx, U, tk) for tk in range(a, tc + 1, st)]
            same = all(x == place for x in pls)
            own = all(x in set(th['own_places']['CT']) and _AREA.get(x) == _AREA.get(place) for x in pls)
            adv = (C.dist2(C.pos(ctx, U, a), home) - C.dist2(C.pos(ctx, U, tc), home)) if home is not None else 0.0
            if same or (own and adv < th['t04_ct_fwd']):
                return 'ct_post'
    # ③ 单走一侧后出（Q19：T、CT 都算）：接触前 t04_solo_recent_s 秒内本队别人已经交火或倒下，接触时你身边 t04_solo_r 内没有活着的队友
    lo = tc - int(th['t04_solo_recent_s'] * tr)
    team = [m for m in C.mates(ctx, rnd, U) if m != U]
    H = C.hurts(ctx, rnd)
    H = H[(H['tick'] >= lo) & (H['tick'] <= tc) & (H['dmg'] > 0)]
    fought = any((x.att in team and C.is_enemy(ctx, x.att, x.vic, rnd)) or (x.vic in team and C.is_enemy(ctx, x.vic, x.att, rnd))
                 for x in H.itertuples())
    fought = fought or any(lo <= tk <= tc and v in team for tk, v in deaths)
    if fought and side in th['solo_sides']:
        me = C.pos(ctx, U, tc)
        if not any(C.alive(ctx, m, tc, rnd) and C.dist3(C.pos(ctx, m, tc), me) <= th['t04_solo_r'] for m in team):
            return 'solo'
    # ⑤ 我方 eco / 强起打长枪（交给 COST-01）
    if _my_eco(ctx, rnd) or (C.buy(ctx, rnd, 'us') in th['t04_cost_us'] and C.buy(ctx, rnd, 'them') in th['t04_cost_them']):
        return 'cost'
    # ⑥ 远处打平：凶手 3D > t04_far，这次交火你打中过他
    if C.dist3(C.pos(ctx, U, T - 1), C.pos(ctx, K, T - 1)) > th['t04_far']:
        hu = C.hurts(ctx, rnd)
        if ((hu['att'] == U) & (hu['vic'] == K) & (hu['tick'] >= tc - int(th['see_lead_s'] * tr)) & (hu['tick'] <= T)
                & (hu['dmg'] > 0)).any():
            return 'far'
    return None


# ================================================================ TEAM-04 你一个人先接触身后没人
_NADE_KILL = ('hegrenade', 'inferno', 'molotov', 'incgrenade')


def _team04(ctx, U, rnd, gifts):
    th = _th(ctx)
    tr = _tr(ctx)
    T = C.death_tick(ctx, U, rnd)
    if T is None:
        return []
    mk = next((k for k in _kills(ctx, rnd) if k['vic'] == U and k['tick'] == T), None)
    if mk is None or any(g['facts']['victim'] == C.name_of(ctx, U) for g in gifts):
        return []
    K = mk['att']
    if C.wkey(mk.get('wk')) in _NADE_KILL:
        # 被雷 / 火收掉的：交火的对手是最后几秒用枪打你的人（e6b 高分玩家84 R12：高分玩家80 用枪打残，高分玩家70 远处的雷收掉）；
        # 没有人用枪打过你就不是一次先露头的对枪，不判
        hw = C.hurts(ctx, rnd)
        hw = hw[(hw['vic'] == U) & (hw['tick'] <= T) & (hw['tick'] >= T - int(th['nade_kill_gun_s'] * tr))]
        hw = hw[[C.is_gun(w) and C.is_enemy(ctx, U, a, rnd) for a, w in zip(hw['att'], hw['wk'])]] if len(hw) else hw
        if not len(hw):
            return []
        dmg = hw.groupby('att')['dmg'].sum()
        last = hw.groupby('att')['tick'].max()
        K = sorted(dmg.index, key=lambda a: (-float(dmg[a]), -int(last[a]), str(a)))[0]
    mates = _alive_mates(ctx, rnd, U, T)
    if not mates:
        return []
    tc = _contact_start(ctx, U, K, T)
    mv, spd = _moving(ctx, U, tc)
    if not mv:
        return []
    # 被侧翼绕死：他从你背后 / 侧面打你，你一枪没朝他开、也没看到过他（128 R1：对手的 USP 从侧面一枪）。
    # 这不是你比队友先露头，归属留给基础判定（没人看的侧翼）
    hk = C.hurts(ctx, rnd)
    hk = hk[(hk['vic'] == U) & (hk['att'] == K) & (hk['tick'] <= T)]
    t_hit = int(hk['tick'].min()) if len(hk) else T
    fk = C.fires(ctx, rnd, sid=U)
    fired_k = any(s_['round'] == rnd and tc - int(th['see_lead_s'] * tr) <= s_['tick'] <= T and s_.get('target') == K
                  for s_ in C.shots(ctx, U)) if len(fk) else False
    if C.facing_deg(ctx, U, t_hit, C.pos(ctx, K, t_hit)) > th['flank_facing_deg'] and not fired_k             and C.spots(ctx, U, K, tc - int(th['see_lead_s'] * tr), T) is None:
        return []
    # 3：接触前 3 秒，3D 1500 内没有队友已经和敌人照面
    me = C.pos(ctx, U, tc)
    ens = C.enemies(ctx, rnd)
    for a, b in _cells(ctx, tc - int(th['first_back_s'] * tr), tc):
        for m in C.mates(ctx, rnd):
            if not C.alive(ctx, m, b, rnd) or C.dist3(C.pos(ctx, m, a), me) > th['first_r']:
                continue
            for e in ens:
                if C.spots(ctx, m, e, a, b) is not None or C.spots(ctx, e, m, a, b) is not None:
                    return []
    # 4：接触前 0.5 秒到阵亡，没有活着的队友连续 0.25 秒看到凶手
    if any(_sees_run(ctx, m, K, tc - int(th['see_lead_s'] * tr), T, th['see_min_s']) for m in mates):
        return []
    # 5：凶手 fast_trade_s（审核单第 7 条 / Q15：2→3 秒）内被补掉 = 进点被换了，不判，交给对枪动作
    tt = _killed_by_team(ctx, rnd, K, U, T, T + th['fast_trade_s'] * tr)
    if tt:
        _exempt(ctx, 'TEAM-04', rnd, T, 'traded', trade_t=_t(ctx, tt, rnd))
        return []
    near = _near_mates(ctx, rnd, U, T, th['near_r'])
    first_death = _all_deaths(ctx, rnd)[0][0] == T
    if first_death and not near:
        return []                                     # 6：全场第一个死、身边本来没人（按分工控图，交给时机类）
    # 审核单第 28 条：你第一个进点倒下、后面的人没及时跟上 → 记团队，不判你先露头
    ent = _entry_no_follow(ctx, U, rnd, U, K, T)
    if ent is not None:
        return [_entry_hit(ctx, U, rnd, ent)]
    # 审核单第 7、17 条（手册 POS-G）：总要有人先接触，这些不是“一个人先露头”
    why = _t04_exempt_reason(ctx, U, rnd, K, T, tc)
    if why:
        _exempt(ctx, 'TEAM-04', rnd, T, why, contact_t=_t(ctx, tc, rnd))
        return []
    us, them = C.alive_at_death(ctx, U, rnd)
    traded = _killed_by_team(ctx, rnd, K, U, T, T + th['trade_s'] * tr)
    nxt = None
    if not traded:
        for m in mates:
            s = C.spots(ctx, m, K, T, T + int(th['next_seen_s'] * tr))
            if s is not None:
                nxt = s if nxt is None else min(nxt, s)
    follow = th['t04_late_trade_word'] if traded else ('过了一会儿队友才看到他' if nxt else '之后也没有队友看到他')
    pcn = C.place_cn(C.place_at(ctx, U, tc))
    kn = C.name_of(ctx, K)
    txt = (f"第 {_ts(ctx, tc, rnd)} 秒你在{pcn}" + ('比附近的队友先露头' if near else '，身边没有队友，一个人先露头')
           + f'，交火时没有队友看得到 {kn}，{follow}')
    side = C.user_side(ctx, rnd)
    if them - us >= th['deficit_n']:
        blame = '合理代价'
    elif _my_eco(ctx, rnd):
        blame = '小问题'
    else:
        blame = '你的责任'
    place = C.place_at(ctx, U, tc)
    kplace = C.place_at(ctx, K, tc)
    if side == 'T':
        if _AREA.get(place) == 'A' and kplace == 'CTSpawn':
            # 只给一个思路：打你的人就在 CT 出生点时才写这一条（e6b R12 打你的人在 A 楼梯）
            alt = '还可以在 A 区等 CT 出生点的枪先打向别人，再拉出来打侧身'
            kb = _kb([], 'T', place)
        elif near:
            alt = '还可以等附近的队友到位一起出，你一交火队友马上补上'
            kb = _kb([], 'T', place)
        else:
            alt = '还可以先往队友那边靠，等队友到身边一起出，你一交火队友马上补上'
            kb = _kb([], 'T', place)
    else:
        alt = ('还可以先等附近的队友到位再出，站在队友能补到的位置' if near
               else '还可以先往队友那边靠，站在队友能补到的位置')
        kb = _kb([], 'CT', place)
    ev = f"从开始照面到你倒下，没有队友看到 {kn}" + ('，附近的队友还没跟上' if near else '')
    facts = dict(contact_t=_t(ctx, tc, rnd), death_t=_t(ctx, T, rnd), killer=kn, place=pcn, near=bool(near),
                 near_mates=[C.name_of(ctx, m) for m in near], us=us, them=them, follow=follow,
                 trade_t=_t(ctx, traded, rnd) if traded else None, next_seen_t=_t(ctx, nxt, rnd) if nxt else None,
                 minor='一个人先进' if blame == '合理代价' else ('eco 局' if blame == '小问题' else None))
    return [_hit('TEAM-04', '问题', rnd, tc, ctx, U, U, blame, txt, alt, kb, ev, facts,
                 death_t=_t(ctx, T, rnd), death_of=C.name_of(ctx, U))]


# ================================================================ TEAM-05 / TEAM-06 队友倒下时你补不补得上
def _fired(ctx, sid, t0, t1):
    f = C.fires(ctx, sid=sid)
    return bool(((f['tick'] >= t0) & (f['tick'] <= t1)).any())


def _busy(ctx, rnd, sid, other, t0, t1):
    """sid 在 [t0, t1] 里和别的敌人互相打（打中或被打中）"""
    h = C.hurts(ctx, rnd)
    w = h[(h['tick'] >= t0) & (h['tick'] <= t1)]
    a = w[(w['att'] == sid) & (w['vic'] != other) & (w['vic'] != sid)]
    a = a[[C.is_enemy(ctx, sid, v, rnd) for v in a['vic']]] if len(a) else a
    v = w[(w['vic'] == sid) & (w['att'] != other) & (w['att'] != sid)]
    v = v[[C.is_enemy(ctx, sid, x, rnd) for x in v['att']]] if len(v) else v
    return len(a) + len(v) > 0


def _trade_check(ctx, U, rnd, k):
    """队友 k['vic'] 倒下时用户补不补得上：返回 dict(kind=traded/stack/tried/far/late/could/none, ...) 或 None（用户已死）"""
    th = _th(ctx)
    tr = _tr(ctx)
    T, K, V = k['tick'], k['att'], k['vic']
    if not C.alive(ctx, U, T, rnd):
        return None
    me, vp = C.pos(ctx, U, T), C.pos(ctx, V, T - 1)
    d_v = C.dist3(me, vp)
    saw = _sees_run(ctx, U, K, T - int(th['see_lead_s'] * tr), T + int(th['see_after_s'] * tr), th['see_min_s'])
    heard = C.heard_shots(ctx, U, T - int(th['hear_before_s'] * tr), T + int(th['hear_after_s'] * tr), shooters=[K], near=True)
    heard = len(heard) > 0
    res = dict(T=T, d_v=d_v, saw=saw is not None, heard=heard)
    mine = _killed_by_team(ctx, rnd, K, U, T, T + th['trade_s'] * tr)
    if mine and any(x['att'] == U and x['tick'] == mine for x in _kills(ctx, rnd)):
        res['kind'] = 'traded'
        return res
    # POS-06 ①②：叠人 / 贴身，并且这段时间没看见凶手
    dz = abs(vp[2] - me[2])
    stacked = (C.dist2(vp, me) <= th['stack_xy'] and dz >= th['stack_dz']) or d_v <= th['close_3d']
    if stacked and C.spots(ctx, U, K, T - int(th['see_lead_s'] * tr), T + int(th['see_after_s'] * tr)) is None:
        res['kind'] = 'stack'
        return res
    fired = _fired(ctx, U, T - int(th['see_lead_s'] * tr), T + int(th['see_after_s'] * tr))
    res['fired'] = fired
    if saw is not None and fired:
        res['kind'] = 'tried'
        return res
    if saw is None and d_v > th['drift_far']:
        far = _drift(ctx, U, V, rnd, T)
        if far:
            res.update(kind='far', **far)
            return res
    late = _killed_by_team(ctx, rnd, K, U, T, T + th['late_s'] * tr)
    late_me = late and any(x['att'] == U and x['tick'] == late for x in _kills(ctx, rnd))
    h = C.hurts(ctx, rnd)
    hit_late = bool(((h['att'] == U) & (h['vic'] == K) & (h['tick'] > T) & (h['tick'] <= T + th['late_s'] * tr)).any())
    if late_me or hit_late:
        res['kind'] = 'late'
        return res
    other = _killed_by_team(ctx, rnd, K, U, T, T + th['trade_s'] * tr)
    blind = C.blind_left(ctx, U, T) > th['blind_s']
    busy = _busy(ctx, rnd, U, K, T - int(th['busy_before_s'] * tr), T + int(th['busy_after_s'] * tr))
    us_after, _ = _after(ctx, T, U)
    res.update(other=bool(other), blind=blind, busy=busy, us_after=us_after)
    # 审核单第 16 条 / Q4（手册 POS-D）：只听得到枪声、看不到凶手时守住自己的口子是正常的，删掉“枪声就在你旁边”分支
    if saw is not None and not fired and not blind and not busy and not other and us_after >= 2:
        res['kind'] = 'could'
        return res
    if heard and saw is None and not fired and not blind and not busy and not other and us_after >= 2:
        res['heard_only'] = True
    res['kind'] = 'none'
    return res


def _with(ctx, a, b, tick, r):
    """a、b 算在同一边：同一个报点，或者 3D 不超过 r"""
    pa, pb = C.place_at(ctx, a, tick), C.place_at(ctx, b, tick)
    return (pa is not None and pa == pb) or C.dist3(C.pos(ctx, a, tick), C.pos(ctx, b, tick)) <= r


def _solo_side(ctx, U, rnd, k):
    """第六轮拍板 3：你一个人在一侧（例如 T 一个人在 A 二楼），倒下的队友在人多的那半边，并且是那半边刚开打的
    solo_wait_s 秒里倒下的——单走一侧只能等人多的那边先交火再出来，这之前没出来补、离得远都不算错过机会。
    开打 = 从他倒下往回找他那半边连着的挨枪 / 打中对面（间隔 <= solo_chain_s）。
    人多的那半边 = 开打那一刻和他同一报点或 3D <= solo_r 的活着的队友（含他）>= solo_main_n 个；
    你一个人 = 开打那一刻没有活着的队友和你同一报点或 3D <= solo_near_r，你也不在他那半边，开打前你自己也没在交火。
    成立返回 dict(t_fight, main=[sid])，否则 None"""
    th, tr = _th(ctx), _tr(ctx)
    T, V = int(k['tick']), k['vic']
    team = [m for m in C.mates(ctx, rnd, U) if m != U]
    if V not in team or C.side_of(ctx, U, rnd) not in th['solo_sides']:
        return None
    H = C.hurts(ctx, rnd)
    H = H[(H['tick'] <= T) & (H['tick'] >= T - int(th['solo_back_s'] * tr)) & (H['dmg'] > 0)]
    evs = set()
    for x in H.itertuples():
        if x.vic in team and x.att != x.vic and C.is_enemy(ctx, x.vic, x.att, rnd):
            evs.add((int(x.tick), x.vic))
        elif x.att in team and x.vic != x.att and C.is_enemy(ctx, x.att, x.vic, rnd):
            evs.add((int(x.tick), x.att))
    t_f = T
    for tick, who in sorted(evs, reverse=True):
        if t_f - tick > th['solo_chain_s'] * tr:
            break
        if who == V or _with(ctx, who, V, tick, th['solo_r']):
            t_f = min(t_f, tick)
    if T - t_f > th['solo_wait_s'] * tr:
        return None
    t0 = max(C.round_info(ctx, rnd)['start'], t_f - 1)
    main = [m for m in team if C.alive(ctx, m, t0, rnd) and (m == V or _with(ctx, m, V, t0, th['solo_r']))]
    if len(main) < th['solo_main_n'] or _with(ctx, U, V, t0, th['solo_near_r']):
        return None
    if any(C.alive(ctx, m, t0, rnd) and _with(ctx, m, U, t0, th['solo_near_r']) for m in team):
        return None
    pre = int(th['solo_self_pre_s'] * tr)
    mine = C.hurts(ctx, rnd)
    if (((mine['att'] == U) | (mine['vic'] == U)) & (mine['tick'] >= t_f - pre) & (mine['tick'] <= t_f) & (mine['dmg'] > 0)).any():
        return None                       # 你自己先交上火了：不是在等那半边
    return dict(t_fight=t_f, main=main)


def _drift(ctx, U, V, rnd, T):
    """TEAM-06：回合 10 秒以后、他死前 3–15 秒之间你们曾连续 2 秒在 700 内；之后两人各自走的路程里你的占比"""
    th = _th(ctx)
    tr = _tr(ctx)
    st = int(th['contact_step_ticks'])
    start = C.round_info(ctx, rnd)['start']
    a = max(T - int(th['drift_win_s'][1] * tr), start + int(th['drift_after_s'] * tr))
    b = T - int(th['drift_win_s'][0] * tr)
    need = int(round(th['drift_keep_s'] * tr / st))
    run, last = 0, None
    for tk in range(a, b + 1, st):
        ok = (C.alive(ctx, U, tk, rnd) and C.alive(ctx, V, tk, rnd)
              and C.dist3(C.pos(ctx, U, tk), C.pos(ctx, V, tk)) <= th['drift_near'])
        run = run + 1 if ok else 0
        if run >= need:
            last = tk
    if last is None:
        return None
    mo, mv = C.path_len(ctx, U, last, T - 1), C.path_len(ctx, V, last, T - 1)
    share = mo / (mo + mv) if mo + mv > 0 else 0.5
    return dict(near_tick=last, share=share)


def _planting_between(ctx, U, rnd, t0, t1):
    """TEAM-06 豁免：包是你下的，下包（含下包前 t06_plant_pre_s 秒的拿包、蹲下）落在 [t0, t1] 里——你离开队友是去下包"""
    r = C.tl_round(ctx, rnd)
    p = (r or {}).get('plant') or {}
    if p.get('tick') is None:
        return False
    if not (p.get('by_user') or C.sid_of(ctx, p.get('planter')) == U):
        return False
    pt = int(p['tick'])
    return t0 <= pt and pt - int(_th(ctx)['t06_plant_pre_s'] * _tr(ctx)) <= t1


def _went_back(ctx, V, rnd, t0, t1):
    """TEAM-06：队友 V 从 t0 到 t1 往回走——离对面出生点（回合开始时对面 5 个人站的地方的中心）远了 t06_back_dist 以上
    （T 方从中远退回房区守后路、绕后；CT 往后收）。这是他自己的走位，不算你走开（e6b R10）"""
    start = C.round_info(ctx, rnd)['start']
    foes = C.enemies(ctx, rnd, V)
    if not foes:
        return False
    home = np.mean([C.pos(ctx, e, start) for e in foes], axis=0)
    return C.dist3(C.pos(ctx, V, t1 - 1), home) - C.dist3(C.pos(ctx, V, t0), home) >= _th(ctx)['t06_back_dist']


def _follow_hit(ctx, U, rnd, k, ent):
    """队友第一个进点倒下、你是后面的人之一没及时跟上（审核单第 28 条“记后面的人”）：TEAM-05 facts.variant=entry_follow"""
    T = k['tick']
    vn, kn = C.name_of(ctx, k['vic']), C.name_of(ctx, k['att'])
    sc = C.place_cn(f"Bombsite{ent['site']}", f"{ent['site']} 点")
    others = [m for m in ent['followers'] if m != U]
    who = '你' + ('和 ' + '、'.join(C.name_of(ctx, m) for m in others) if others else '')
    txt = f"第 {_ts(ctx, T, rnd)} 秒 {vn} 第一个进{sc}被 {kn} 打死，后面的{who}没有及时跟上进点，也没补掉他"
    if ent['util_n'] == 0:
        txt += '；进点前全队没往点里扔闪或火'
    ev = f"{vn} 倒下后好几秒都没有人进{sc}，{kn} 也没被补掉"
    blame = '你的责任 + 队友或团队' if others else '你的责任'
    facts = dict(variant='entry_follow', mate=vn, killer=kn, site=sc, death_t=_t(ctx, T, rnd),
                 followers=[_nm(ctx, m, U) for m in ent['followers']], team_util=ent['util_n'],
                 my_place=C.place_cn(C.place_at(ctx, U, T)), saw=False, heard=False, n=1, ts=[_t(ctx, T, rnd)],
                 mates=[vn], place=C.place_cn(ent['place']))
    return _hit('TEAM-05', '问题', rnd, T, ctx, U, U, blame, txt, _entry_alt(ctx, ent), _kb([], 'T', ent['place']),
                ev, facts, death_t=_t(ctx, T, rnd), death_of=vn, merge_key=f'entry:{T}')


def _entry_util_idea(ctx, U, rnd, done):
    """审核单第 28 条 / 手册 UTIL-E：T 方本队第一个人进包点（开局 entry_from_s 秒后、下包前）时，[t1 − entry_util_back_s,
    t1 + entry_util_lag_s] 内我方没有一颗闪 / 火在包点中心 entry_util_r 内起爆 → 团队一条“思路”（不定个人责任），
    不管第一个人倒没倒下（第 4 场 R11 66：你第一个进 B，0 颗）。这一回合 TEAM-04 entry / TEAM-05 entry_follow 已经写了 0 颗的不再出"""
    th, tr = _th(ctx), _tr(ctx)
    if C.user_side(ctx, rnd) != 'T':
        return []
    if any((h.get('facts') or {}).get('variant') in ('entry', 'entry_follow') and (h.get('facts') or {}).get('team_util') == 0
           for h in done):
        return []
    if _my_eco(ctx, rnd) or (C.buy(ctx, rnd, 'us') in th['t04_cost_us'] and C.buy(ctx, rnd, 'them') in th['t04_cost_them']):
        return []                               # eco / 强起打长枪：道具本来就少，不出这条团队思路
    ri = C.round_info(ctx, rnd)
    pl = _plant(ctx, rnd)
    a = int(ri['start'])
    b = int(pl[0]) if pl else int(ri['end'])
    team = C.mates(ctx, rnd, U, include_self=True)
    tm = C.team_of(ctx, U, rnd)
    out = []
    for site in sorted(th['entry_util_places']):
        places = set(th['entry_util_places'][site])           # 只认包点本身（跳台、A 楼梯这些 T 开局就能到）
        firsts = [(t, m) for m in team for t in [_first_in(ctx, m, rnd, places, a, b)] if t is not None]
        if not firsts:
            continue
        t1, m1 = min(firsts)
        if C.t_of(ctx, t1, rnd) < th['entry_from_s']:
            continue
        sxy = th['entry_site_xy'][site]
        lo, hi = t1 - int(th['entry_util_back_s'] * tr), t1 + int(th['entry_util_lag_s'] * tr)
        R = th['entry_util_idea_r']
        n = sum(1 for tk, sid, xyz in _close_util(ctx)
                if lo <= tk <= hi and sid and C.team_of(ctx, sid, rnd) == tm and C.dist2(xyz, sxy) <= R)
        # 手册 UTIL-E 检测口径：闪、火、烟都算（句子只说“闪或火”，有烟落在点上也不出）
        n += sum(1 for x in C.smokes(ctx, rnd) if lo <= x['t0'] <= hi and x.get('by') and C.team_of(ctx, x['by'], rnd) == tm
                 and C.dist2((x['x'], x['y'], x['z']), sxy) <= R)
        if n:
            continue
        sc = C.place_cn(f"Bombsite{site}", f"{site} 点")
        who = '你' if m1 == U else C.name_of(ctx, m1)
        txt = f"第 {_ts(ctx, t1, rnd)} 秒{who}第一个进{sc}，进点前全队没往点里扔闪或火"
        alt = '还可以进点前先往点里给一颗闪或火，第一个人进去时后面的人贴着跟上，前面一交火马上补'
        facts = dict(variant='entry_util', site=sc, first=C.name_of(ctx, m1), team_util=0, enter_t=_t(ctx, t1, rnd))
        out.append(_hit('TEAM-05', '思路', rnd, t1, ctx, U, U, '—', txt, alt, [], f'{who}进{sc}前后，我方没有闪或火落在点上',
                        facts, merge_key=f'entry_util:{rnd}:{site}'))
        break                                   # 一回合只给一条（第一个被进的包点）
    return out


def _site_toward(ctx, a, b, xy_map, need):
    """a、b 两个 3D 点：往哪个包点平面走近了 >= need（走近最多的那个）→ (包点, 走近多少) 或 None"""
    best = None
    for site, xy in sorted(xy_map.items()):
        d = C.dist2(a, xy) - C.dist2(b, xy)
        if d >= need and (best is None or d > best[1]):
            best = (site, d)
    return best


def _best_tier(ctx, sid, tick):
    """tick 时身上最好的枪的档（有背包数据用背包，没有用手上的枪）"""
    fr = _inv_frames(ctx).get(sid)
    if fr:
        tk, inv = fr
        i = int(np.searchsorted(tk, int(tick), side='right')) - 1
        if i >= 0 and isinstance(inv[i], (list, tuple, np.ndarray)):
            return max([C.gun_tier(w) for w in inv[i] if isinstance(w, str)] + [-1])
    return C.gun_tier(C.weapon_at(ctx, sid, tick))


def _route_of(ctx, sid, rnd, t0, t1):
    """[t0, t1) 里 sid 最后走过的进攻路线（team.json pinch_routes 报点 → 路线；包点等不在表里的报点跳过），没有 None"""
    th = _th(ctx)
    routes = th['pinch_routes']
    st = int(th['contact_step_ticks'])
    last = None
    for tk in range(int(t0), int(t1), st):
        if not C.alive(ctx, sid, tk, rnd):
            break
        r = routes.get(C.place_at(ctx, sid, tk) or '')
        if r:
            last = r
    return last


def _move_intent(ctx, U, rnd, t0, k):
    """审核单第 8 条（手册 POS-E）：你从 t0（最后还和他在一起）到他倒下这段挪动的意图（T、CT 都做；只用你当时拿得到的信息）。
    返回 dict(kind=补位/补防/堵路/捡枪/回防/两路夹/两路没对上/无, dst, anchors, extra)"""
    th, tr = _th(ctx), _tr(ctx)
    T, V = int(k['tick']), k['vic']
    side = C.user_side(ctx, rnd)
    p0, p1 = C.pos(ctx, U, t0), C.pos(ctx, U, T - 1)
    near = th['intent_closer']
    # 补位：最后在一起前 intent_kill_back_s 秒到他倒下之间，别的队友倒下了（击杀栏），你往那个倒下处走近，或往本方出生点那边回；
    # 往哪走看到他倒下后 intent_look_after_s 秒（你自己当时的走向，a32 R10：从 B 后巷掉头回中路，他倒下时才走到 T 方房区）
    lo = t0 - int(th['intent_kill_back_s'] * tr)
    tm = C.team_of(ctx, U, rnd)
    ud = C.death_tick(ctx, U, rnd)
    te = T + int(th['intent_look_after_s'] * tr)
    pe = C.pos(ctx, U, min(te, ud - 1) if ud is not None else te)
    start = C.round_info(ctx, rnd)['start']
    own = np.mean([C.pos(ctx, m, start) for m in C.mates(ctx, rnd, U, include_self=True)], axis=0)
    for x in _kills(ctx, rnd):
        if lo <= x['tick'] < T and x['vic'] not in (U, V) and C.team_of(ctx, x['vic'], rnd) == tm:
            if (C.dist2(p0, x['vpos']) - C.dist2(pe, x['vpos']) >= near
                    or C.dist2(p0, own) - C.dist2(pe, own) >= near):
                return dict(kind='补位', dst=C.place_cn(x['vplace']), anchors=[])
    # 捡枪：这段时间捡起一把比身上最好的枪更高档的枪
    pk = _pickups(ctx)
    if pk is not None and len(pk):
        w = pk[(pk['sid'] == U) & (pk['tick'] > t0) & (pk['tick'] <= T)]
        had = _best_tier(ctx, U, t0)
        for it in w.itertuples():
            if C.is_gun(it.item) and C.gun_tier(it.item) > had:
                return dict(kind='捡枪', dst=C.weapon_cn(it.item), anchors=[])
    # 两路夹：你和他都往同一个包点走近；到点时间差 <= pinch_gap_s 算对上（他没到就按他倒下那一刻）
    su = _site_toward(ctx, p0, p1, th['intent_site_xy'], near)
    sv = _site_toward(ctx, C.pos(ctx, V, t0), C.pos(ctx, V, T - 1), th['intent_site_xy'], near)
    if su and sv and su[0] == sv[0]:
        places = set(th['entry_places'][su[0]])
        va = _first_in(ctx, V, rnd, places, t0, T) or T
        ua = _first_in(ctx, U, rnd, places, t0, T + int(th['pinch_gap_s'] * tr))
        if ua is not None and abs(ua - va) <= th['pinch_gap_s'] * tr:
            return dict(kind='两路夹', dst=su[0], anchors=[])
        if ua is None or abs(ua - va) > th['pinch_gap_s'] * tr:
            # 只有不同路线去同一个点才叫两路：他倒下的报点和你那段走的报点不一样；
            # 复核修正：报点按路线分组（pinch_routes：B 后巷 + B 二楼一条路、中路 / B 小一条、下水道一条……），
            # 两人这段最后走的是同一条路线就不叫两路（128 R11：你在 B 后巷、他走 B 后巷 → B 二楼）
            ru, rv = _route_of(ctx, U, rnd, t0, T), _route_of(ctx, V, rnd, t0, T)
            if C.place_at(ctx, U, T - 1) != C.place_at(ctx, V, T - 1) and not (ru and rv and ru == rv):
                return dict(kind='两路没对上', dst=su[0], anchors=[])
    # 回防：已下包，CT 往包走近
    pl = _plant(ctx, rnd)
    if side == 'CT' and pl and pl[0] <= T and pl[2] is not None:
        if C.dist2(p0, pl[2]) - C.dist2(p1, pl[2]) >= near:
            return dict(kind='回防', dst=pl[1], anchors=[])
    if side != 'CT':
        return None
    # CT：往哪个包点挪了（intent_site_xy，平面走近 >= intent_shift_closer）
    big = _site_toward(ctx, p0, p1, th['intent_site_xy'], th['intent_shift_closer'])
    # 堵路：出发前 intent_sig_back_s 秒有 T 上过我方雷达 / 你听得到他开枪，你往他那时的位置走近，他倒下时你还在本方守位报点
    if C.place_at(ctx, U, T - 1) in set(th['own_places']['CT']):
        a = t0 - int(th['intent_sig_back_s'] * tr)
        spots_ = [C.pos(ctx, e, lt) for e, lt, _ in C.radar_enemies(ctx, t0, th['intent_sig_back_s'], U)]
        hs = C.heard_shots(ctx, U, a, t0, shooters=C.enemies(ctx, rnd, U))
        spots_ += [np.array([r.user_X, r.user_Y, r.user_Z], float) for r in hs.itertuples()]
        if any(C.dist2(p0, q) - C.dist2(p1, q) >= near for q in spots_):
            return dict(kind='堵路', dst=big[0] if big else None, anchors=[])
    if big is None:
        return None
    dst = big[0]
    anchors = [m for m in C.mates(ctx, rnd, U) if m != U and C.alive(ctx, m, t0, rnd) and _AREA.get(C.place_at(ctx, m, t0)) == dst]
    if len(anchors) <= th['intent_anchor_max']:
        return dict(kind='补防', dst=dst, anchors=anchors)
    return dict(kind='无', dst=dst, anchors=anchors, extra=True)


def _team0506(ctx, U, rnd, skip_could=()):
    th = _th(ctx)
    tr = _tr(ctx)
    tm = C.team_of(ctx, U, rnd)
    side = C.user_side(ctx, rnd)
    hits = []
    could = []
    for k in _kills(ctx, rnd):
        if k['vic'] == U or C.team_of(ctx, k['vic'], rnd) != tm:
            continue
        c = _trade_check(ctx, U, rnd, k)
        if not c:
            continue
        # 第六轮拍板 3：你一个人在一侧、他在人多那半边刚开打时倒下——没出来补 / 离得远不算你的（8d R16 48.5 A 二楼）
        # 走散（far）里是你自己走开的（你的路程占比 >= share_both）：这一侧是你几秒前自己走出来的，不是事先分好的单走一侧，
        # 照判（128 R9：36.1 秒还和 高分玩家19 一起在 A 点，一个人走进 CT 出生点，39.1 秒他倒下）
        if (c['kind'] == 'could' or (c['kind'] == 'far' and c['share'] < th['share_both'])) and _solo_side(ctx, U, rnd, k):
            continue
        vn, kn = C.name_of(ctx, k['vic']), C.name_of(ctx, k['att'])
        # 审核单第 28 条：队友第一个进点倒下，你是后面的人之一、没交火也没跟上 → TEAM-05 variant=entry_follow
        if c['kind'] in ('could', 'none', 'stack'):
            ent = _entry_no_follow(ctx, U, rnd, k['vic'], k['att'], k['tick'])
            if (ent is not None and U in ent['followers'] and not _solo_side(ctx, U, rnd, k)
                    and not _busy(ctx, rnd, U, None, k['tick'] - int(th['busy_before_s'] * tr),
                                  k['tick'] + int(th['entry_follow_s'] * tr))):
                hits.append(_follow_hit(ctx, U, rnd, k, ent))
                continue
        if c.get('heard_only'):
            _exempt(ctx, 'TEAM-05', rnd, k['tick'], 'heard_only', mate=vn, killer=kn)
        if c['kind'] == 'could':
            if k['tick'] not in skip_could:
                could.append((k, c))
        elif c['kind'] == 'far':
            sh = c['share']
            # 你走开是去下包（带着包、正在下包）：不算你走散
            if _planting_between(ctx, U, rnd, c['near_tick'], k['tick']):
                continue
            # 他一个人往自己出生点那边走（绕后、回防、偷人）：是他自己的选择，不算你走开
            back = _went_back(ctx, k['vic'], rnd, c['near_tick'], k['tick'])
            if back:
                sh = 0.0
            if sh >= th['share_self']:
                blame, how = '你的责任', '后来是你走开了'
            elif sh >= th['share_both']:
                blame, how = '你的责任 + 队友或团队', '后来你们俩各走各的，越拉越远'
            elif back:
                blame, how = '队友或团队', '后来是他往回走开了'
            else:
                blame, how = '队友或团队', '后来是他往前走开了'
            vp = C.place_cn(k['vplace'])
            # 审核单第 8 条（手册 POS-E，拍板 4）：你走开先判意图（T、CT 都做）。补位 / 补防 / 堵路 / 捡枪 / 回防 / 两路夹对上了：
            # 有理由的挪动，不记走散（不出时刻，exemptions 留记录）；两路夹没对上：写“两路没对上”；CT 往人够的一侧挪、没有理由：多余的动作
            intent = _move_intent(ctx, U, rnd, c['near_tick'], k) if sh >= th['share_both'] else None
            # 复核修正：team 自己返回 None（没认出任何意图）时也要问 pos.move_intent（原来只在“无 / 多余”时问，
            # c7e R20 CT 去补只剩一个人的 A、e65 R8 去堵 高分玩家73 露过面的路，都被写成走散）
            if sh >= th['share_both'] and (intent is None or intent['kind'] in ('无',) or intent.get('extra')):
                # 集成（pos agent 的 move_intent，手册 POS-E）：team 自己没认出意图时再问一次 pos.move_intent（T、CT 都做，
                # 下包 / 回防 / 拦拆 / 捡枪 / 补位 / 补防 / 堵路）；有一个就算有理由的挪动。追人（chase）不算走开的理由
                pk = [x.get('kind') for x in _pos.move_intent(ctx, U, rnd, int(c['near_tick']), int(k['tick']))]
                pk = [x for x in pk if x in th.get('pos_intent_ok', ())]
                if pk:
                    intent = dict(intent or {}, kind=_POS_INTENT_CN.get(pk[0], pk[0]), extra=False, by_pos=pk[0])
            reason = ''
            if intent is not None and intent['kind'] not in ('无', '两路没对上'):
                _exempt(ctx, 'TEAM-06', rnd, k['tick'], intent['kind'], mate=vn, near_t=_t(ctx, c['near_tick'], rnd),
                        dst=intent.get('dst'))
                continue
            if intent is not None and intent['kind'] == '两路没对上':
                dst_cn = C.place_cn(f"Bombsite{intent['dst']}", f"{intent['dst']} 点")
                how = f'后来你们分两路去{dst_cn}，两路没对上，他倒下时你还没到'
                blame = '你的责任 + 队友或团队'
                reason = 'pinch'
            elif intent is not None and intent.get('extra'):
                dst = intent['dst']
                how += f"；你往 {dst} 那边挪，那边已经有 {len(intent['anchors'])} 个队友守，这次挪动是多余的动作"
                reason = 'extra'
            txt = (f"{vn}在{vp}被打死时你离他太远、看不到凶手；第 {_ts(ctx, c['near_tick'], rnd)} 秒时你们还在一起，{how}")
            alt, kb = '', []
            if sh < th['share_both']:
                # 是他自己走开的：主语写他，不写成“你离他太远”（读着像你的错，8d R15 队友C 一个人往小推车走）
                txt = (f"第 {_ts(ctx, c['near_tick'], rnd)} 秒时 {vn} 还和你在一起，后来他一个人{'往回' if back else '往前'}走到{vp}，"
                       f"离开了你能补枪的范围，被 {kn} 打死")
                alt = '还可以两个人一起走，留在一个人架、一个人能补的距离'
            if reason == 'pinch':
                alt = '还可以两路夹点时留意另一路走到哪了，两边差不多一起到再打进去'
                kb = _kb([], side, C.place_at(ctx, U, k['tick']))
            elif sh >= th['share_both']:
                # 按这一段你实际做了什么配：中间打死过人才写“打完人”（635 R5/R16：一枪没开就走散了）
                fought = any(x['att'] == U and c['near_tick'] <= x['tick'] <= k['tick'] for x in _kills(ctx, rnd))
                alt = ('还可以打完人先往队友身边靠，留在一个人架、一个人能补的距离' if fought else
                       '还可以换位前先看一眼队友在哪，留在一个人架、一个人能补的距离')
                kb = _kb([], side, C.place_at(ctx, U, k['tick']))
            ev = f"{vn}倒下时你在{C.place_cn(C.place_at(ctx, U, k['tick']))}，离他太远"
            facts = dict(mate=vn, killer=kn, mate_place=vp, near_t=_t(ctx, c['near_tick'], rnd), share=round(sh, 2),
                         intent=(intent or {}).get('kind'), intent_dst=(intent or {}).get('dst'), reason=reason or None,
                         death_t=_t(ctx, k['tick'], rnd), my_place=C.place_cn(C.place_at(ctx, U, k['tick'])))
            hits.append(_hit('TEAM-06', '问题', rnd, k['tick'], ctx, U, U if sh >= th['share_both'] else k['vic'], blame,
                             txt, alt, kb, ev, facts, death_t=_t(ctx, k['tick'], rnd), death_of=vn))
    # TEAM-05：同一个凶手连着杀的几次合成一条
    by_k = defaultdict(list)
    for k, c in could:
        by_k[k['att']].append((k, c))
    for K in sorted(by_k, key=lambda s: by_k[s][0][0]['tick']):
        lst = by_k[K]
        k0 = lst[0][0]
        saw = True                                    # 只剩“看得到却没补”（审核单第 16 条删掉了只听得到的分支）
        names = '、'.join(C.name_of(ctx, k['vic']) for k, _ in lst)
        kn = C.name_of(ctx, K)
        vp = C.place_cn(k0['vplace'])
        txt = f'{names}在{vp}被 {kn} 打死时，你看得到他，你没出去补'
        alt = '还可以队友一倒马上拉出去补，他正忙着打你队友，顾不上你'
        kb = _kb([], side, k0['vplace'])
        ev = f'{kn} 打死队友时你看得到他，这段时间你没开枪'
        facts = dict(mates=[C.name_of(ctx, k['vic']) for k, _ in lst], killer=kn, place=vp, saw=saw,
                     heard=any(c['heard'] for _, c in lst), n=len(lst), ts=[_t(ctx, k['tick'], rnd) for k, _ in lst],
                     my_place=C.place_cn(C.place_at(ctx, U, k0['tick'])))
        hits.append(_hit('TEAM-05', '问题', rnd, k0['tick'], ctx, U, U, '你的责任', txt, alt, kb, ev,
                         facts, death_t=_t(ctx, k0['tick'], rnd), death_of=C.name_of(ctx, k0['vic']),
                         merge_key=f'trade:{K}:{k0["tick"]}'))
    return hits


# ================================================================ TEAM-G1 / TEAM-G2 补枪、一起打同一个人
def _teamG12(ctx, U, rnd):
    th = _th(ctx)
    tr = _tr(ctx)
    tm = C.team_of(ctx, U, rnd)
    side = C.user_side(ctx, rnd)
    hits = []
    ks = _kills(ctx, rnd)
    for uk in [k for k in ks if k['att'] == U]:
        E, KT = uk['vic'], uk['tick']
        # TEAM-G1：E 之前 12 秒内打死过队友（取最近的一次），那时你还活着
        md = [k for k in ks if k['att'] == E and k['vic'] != U and C.team_of(ctx, k['vic'], rnd) == tm
              and 0 < KT - k['tick'] <= th['trade_late_s'] * tr and C.alive(ctx, U, k['tick'], rnd)]
        if md:
            k0 = md[-1]
            t0 = k0['tick']
            dt = (KT - t0) / tr
            mn, kn = C.name_of(ctx, k0['vic']), C.name_of(ctx, E)
            us, them = _after(ctx, t0, U)
            saw = C.spots(ctx, U, E, t0 - int(th['see_pm_s'] * tr), t0 + int(th['see_pm_s'] * tr)) is not None
            facts = dict(mate=mn, killer=kn, victim=kn, mate_place=C.place_cn(k0['vplace']), t0=_t(ctx, t0, rnd),
                         kill_t=_t(ctx, KT, rnd), dt=C.sec(dt), us=us, them=them, saw=saw)
            if dt <= th['trade_fast_s']:
                # 人数按你补枪那一刻前后算（队友倒下到你补枪之间可能又有人倒下）
                kb_us, kb_them = _after(ctx, KT - 1, U)
                ka_us, ka_them = _after(ctx, KT, U)
                won_all = ka_them == 0 and _won(ctx, rnd)
                txt = (f"第 {_ts(ctx, t0, rnd)} 秒 {mn} 在{C.place_cn(k0['vplace'])}被 {kn} 打死，你"
                       + ('马上' if dt < 1 else '很快') + '就把他补掉，'
                       + ('残局赢下' if won_all else f'{C.alive_str(kb_us, kb_them)} 拉回 {C.alive_str(ka_us, ka_them)}'))
                facts.update(us_kill=kb_us, them_kill=kb_them, us_after=ka_us)
                alt, kb = '', []
                facts.update(variant='补枪', them_after=ka_them, won=won_all,
                             why_good='队友一倒你马上把凶手补掉，人数没被拉开')
                ev = f'{mn} 倒下后你很快打死了 {kn}'
            else:
                mine = C.dist3(C.pos(ctx, U, t0), C.pos(ctx, U, KT))
                his = C.dist3(C.pos(ctx, E, t0), C.pos(ctx, E, KT - 1))
                # 跟上去：你走得够多、至少是他的一半，并且是往他那边走（离他最后的位置更近了）
                toward = C.dist3(C.pos(ctx, U, KT), C.pos(ctx, E, KT - 1)) < C.dist3(C.pos(ctx, U, t0), C.pos(ctx, E, KT - 1))
                follow = mine >= th['follow_min'] and mine >= his * th['follow_ratio'] and toward
                if follow:
                    txt = f'{mn} 倒下后，你跟上去把刚杀人的 {kn} 打掉了'
                    why = '队友倒下后你跟上去把凶手打掉，替队友换了回来'
                else:
                    # 复核修正：你死后才（用雷 / 火）把他收掉的，不是“他找过来时你已经架好了”（bd R25：你死后你的雷炸死 对手5（第4场））
                    if not C.alive(ctx, U, KT, rnd) or not C.is_gun(uk.get('wk')):
                        continue
                    txt = f'{mn} 倒下后，{kn} 接着往你这边来，被你打掉，替队友换了回来'
                    # “守在原来的位置”只在你真的没怎么动时写（8d R9）
                    stay = mine <= th['stay_max']
                    # “已经架好了”只给打死他前 g1_set_s 秒一直没在跑的（速度 <= g1_set_ups）
                    vs = [C.move_speed(ctx, U, tk) for tk in range(KT - int(th['g1_set_s'] * tr), KT + 1, 8)]
                    vs = [v for v in vs if v is not None]
                    set_up = bool(vs) and max(vs) <= th['g1_set_ups']
                    why = ('队友倒下后你守住了位置，他找过来时你把他打掉，替队友换了回来' if stay and set_up else
                           '队友倒下后他找过来时你已经架好了，把他打掉，替队友换了回来' if set_up else
                           '队友倒下后他找过来时你把他打掉，替队友换了回来')
                    stay = stay and set_up
                    facts.update(stay=bool(stay), set_up=bool(set_up),
                                 why_good_nopos='队友倒下后他找过来时你把他打掉，替队友换了回来',
                                 ev_nopos=f'{mn} 倒下后 {kn} 自己走到了你的枪口前')
                alt, kb = '', []
                facts.update(variant='跟上去' if follow else '他找过来', why_good=why)
                ev = (f'{mn} 倒下后你主动往 {kn} 那边走' if follow else
                      (f'{mn} 倒下后你守在原来的位置，{kn} 自己走到了你的枪口前' if facts.get('stay')
                       else f'{mn} 倒下后你已经架好了，{kn} 自己走到了你的枪口前' if facts.get('set_up')
                       else f'{mn} 倒下后 {kn} 往你这边来，被你打掉'))
            hits.append(_hit('TEAM-G1', '好', rnd, KT, ctx, U, U, '—', txt, alt, kb, ev, facts, merge_key=f'kill:{KT}'))
            continue
        # TEAM-G2：队友先打中了他，你跟上打死（不是补枪）
        h = C.hurts(ctx, rnd)
        w = h[(h['vic'] == E) & (h['tick'] >= KT - int(th['assist_s'] * tr)) & (h['tick'] < KT)]
        w = w[[a != U and C.team_of(ctx, a, rnd) == tm for a in w['att']]] if len(w) else w
        if not len(w):
            continue
        sums = w.groupby('att')['dmg'].sum()
        M = sorted(sums.index, key=lambda s: (-float(sums[s]), s))[0]
        if float(sums[M]) < th['assist_dmg'] or not C.alive(ctx, M, KT, rnd):
            continue
        lo = KT - int(th['assist_s'] * tr)
        both = C.spots(ctx, U, E, lo, KT) is not None and C.spots(ctx, M, E, lo, KT) is not None
        pu, pm = C.pos(ctx, U, KT), C.pos(ctx, M, KT)
        close = C.dist3(pu, pm) <= th['assist_r'] and abs(pu[2] - pm[2]) <= th['assist_dz']
        if not (both or close):
            continue
        mn, vn = C.name_of(ctx, M), C.name_of(ctx, E)
        wk = w[w['att'] == M]['wk'].iloc[-1]
        txt = f'第 {_ts(ctx, KT, rnd)} 秒和 {mn} 一起打 {vn}：{mn} 先用{C.weapon_cn(wk)}打中了他，你跟上把他打死'
        alt, kb = '', []
        # 只有真的离得近才写“离得不远”（128 R22：旁边一条 ECO-G1 说你离队友远）；否则写两个人都看得到他
        ev = f'{mn} 打中 {vn} 后还活着，' + ('你们俩离得不远' if close else '你们俩都看得到他')
        facts = dict(mate=mn, victim=vn, weapon=C.weapon_cn(wk), kill_t=_t(ctx, KT, rnd), both_seen=both, close=close,
                     why_good='两个人打同一个人，他一次只能还一个人的枪', manner=f'和 {mn} 一起打同一个人')
        hits.append(_hit('TEAM-G2', '好', rnd, KT, ctx, U, U, '—', txt, alt, kb, ev, facts, merge_key=f'kill:{KT}'))
    return hits


# ================================================================ TEAM-G3 反 eco 不单走
def _g3_fought(ctx, U, rnd, t0, t1):
    """[t0, t1] 里你和对面真的交上了火：你用枪打中他时他 ±g3_fight_s 秒内看见过你；或他用枪打中你时你 ±g3_fight_s 秒内看见过他 / 朝他开过枪"""
    th, tr = _th(ctx), _tr(ctx)
    w = int(th['g3_fight_s'] * tr)
    h = C.hurts(ctx, rnd)
    h = h[(h['tick'] >= t0) & (h['tick'] <= t1) & (h['dmg'] > 0) & h['wk'].map(C.is_gun)]
    for x in h.itertuples():
        tk = int(x.tick)
        if x.att == U and C.is_enemy(ctx, U, x.vic, rnd) and C.spots(ctx, x.vic, U, tk - w, tk + w) is not None:
            return True
        if x.vic == U and C.is_enemy(ctx, U, x.att, rnd):
            if C.spots(ctx, U, x.att, tk - w, tk + w) is not None or _fired(ctx, U, tk - w, tk + w):
                return True
    return False


def _teamG3(ctx, U, rnd, bad_death):
    th = _th(ctx)
    tr = _tr(ctx)
    if not _anti_eco(ctx, rnd) or bad_death:
        return []
    side = C.user_side(ctx, rnd)
    info = C.round_info(ctx, rnd)
    ud = C.death_tick(ctx, U, rnd)
    lo = info['start'] + int(th['eco_from_s'] * tr)
    hi = ud if ud is not None else info['end']
    st = int(th['eco_step_ticks'])
    ens = C.enemies(ctx, rnd)
    fwd = set(th['ct_forward_places'])
    exposed = alone = 0
    first = None
    for tk in range(lo, hi, st):
        b = tk + st - 1
        if side == 'CT' and C.place_at(ctx, U, tk) in fwd:
            return []                                 # CT 进了前压报点（越过中路）
        seers = [e for e in ens if C.alive(ctx, e, tk, rnd) and C.spots(ctx, e, U, tk, b) is not None]
        if not seers:
            continue
        exposed += 1
        first = first if first is not None else tk
        mates = _alive_mates(ctx, rnd, U, tk)
        covered = any(C.spots(ctx, e, m, tk, b) is not None for e in seers for m in mates)
        if not covered:
            me = C.pos(ctx, U, tk)
            for m in mates:
                pm = C.pos(ctx, m, tk)
                if C.dist3(me, pm) <= th['cover_r'] and abs(me[2] - pm[2]) <= th['cover_dz']:
                    covered = True
                    break
        if not covered:
            alone += 1
    alone_s, exposed_s = alone * st / tr, exposed * st / tr
    if not exposed or alone_s > th['alone_max_s']:      # 整回合没和对面照过面：不夸（对标口径是"照过面的人"）
        return []
    if ud is not None:
        mk = next((k for k in _kills(ctx, rnd) if k['vic'] == U and k['tick'] == ud), None)
        if mk is None or not _killed_by_team(ctx, rnd, mk['att'], U, ud, ud + th['eco_trade_s'] * tr):
            return []
        # 复核修正（第 34 条“只夸打出效果的”）：死了、这回合一点伤害都没打出来的不夸（d5a R17：一枪没开就倒下，标题是反 eco 被翻）
        hu0 = C.hurts(ctx, rnd)
        if not len(hu0[(hu0['att'] == U) & hu0['vic'].isin(list(C.enemies(ctx, rnd))) & (hu0['dmg'] > 0)]):
            return []
    # 审核单第 34 条：被看见不到 exposed_min_s、整回合也没交火的，没什么可夸的
    # 死了、被看见也不到 exposed_min_s：这次露面就是被对面点掉的那一下，不夸（348aed71 R7 例）
    if exposed_s < th['exposed_min_s'] and (ud is not None or not _g3_fought(ctx, U, rnd, lo, hi)):
        return []
    always = alone == 0 and exposed_s >= th['exposed_min_s']
    # 这回合对面用枪打中过你：不写“几乎没让对面看到你”（128 R6：两串对枪的证据都写着“他正朝你开枪”）
    hu = C.hurts(ctx, rnd)
    shot_at = bool(len(hu[(hu['vic'] == U) & hu['att'].isin(list(ens)) & ~hu['wk'].isin(['hegrenade', 'inferno', 'molotov', 'incgrenade'])]))
    txt = '反 eco 这回合你没有一个人往前找人头' + ('，没越过中路' if side == 'CT' else '')
    if always:
        txt += '，被对面看见的时候，身边总有队友也能打到那个人'
    elif not shot_at:
        txt += '，几乎没让对面看到你'
    # 不再补“这回合你没死”（128 R6：读着是凑字）
    alt, kb = '', []                      # “继续这样打……”是夸奖不是另一种思路；理由放 why_good
    kb = _kb([], side, 'Middle')
    ev = ('被对面看到的那几次，旁边都有队友' if always else
          '整回合被对面看到的时间很短' if not shot_at else '被对面看到的时候，大多数时间身边有队友')
    facts = dict(exposed_s=C.sec(exposed_s), alone_s=C.sec(alone_s), died=ud is not None, variant='身边总有队友' if always else '几乎没被看到',
                 why_good='打对面 eco 没有一个人往前找人头，被看到时身边总有队友能补')
    return [_hit('TEAM-G3', '好', rnd, first, ctx, U, U, '—', txt, alt, kb, ev, facts, scope='round')]


# ================================================================ 入口
def run(ctx):
    """跑全部团队配合规则，返回 Hit 列表（按回合、时刻、规则排）"""
    U = C.user_sid(ctx)
    hits = []
    setattr(ctx, '_team_exempt', [])                  # 豁免记录（exemptions(ctx)），每次 run 重建
    for r in C.rounds(ctx):
        rnd = r['n']
        if C.team_of(ctx, U, rnd) == 0:
            continue
        gifts = _team07(ctx, U, rnd)
        events = _streaks(ctx, U, rnd)
        hits += gifts
        t03 = _team03(ctx, U, rnd, events)
        hits += t03
        # 对面冲出来几秒连杀（TEAM-03 burst，合理代价）里倒下的队友：不再判“你能补没补”（8d R16 46.3 同一件事一边合理代价一边你的责任）
        burst_ticks = set()
        for h in t03:
            if (h.get('facts') or {}).get('burst'):
                burst_ticks |= {k['tick'] for e in events if C.name_of(ctx, e['killer']) == h['facts']['killer']
                                for k in e['kills']}
        hits += _eco_hunt(ctx, U, rnd)
        t04 = _team04(ctx, U, rnd, gifts)
        hits += t04
        if _won(ctx, rnd):
            hits += _team01(ctx, U, rnd)
        else:
            hits += _team02(ctx, U, rnd, gifts, events)
        hits += _team0506(ctx, U, rnd, burst_ticks)
        hits += _teamG12(ctx, U, rnd)
        hits += _entry_util_idea(ctx, U, rnd, t04 + [h for h in hits if h['round'] == rnd and h['rule'] == 'TEAM-05'])
        bad = bool(t04) or any(g['is_user'] for g in gifts)
        hits += _teamG3(ctx, U, rnd, bad)
    hits.sort(key=lambda h: (h['round'], h['t'], h['rule'], h['tick'] if h['tick'] is not None else -1))
    return hits
