# -*- coding: utf-8 -*-
"""解析 demo：逐回合时间线 + 整场概览（由 pro/analysis/match3/extract_match3.py 参数化而来，算法不变）。

run(ctx) 输出（work/<id>/）：
  timeline.json        {"meta": {...}, "rounds": [每回合一项]}
  match_overview.json  每回合一行 + 整场用户数据 + 双方计分板 + data_issues
  match_overview.csv   每回合一行（方便直接看）

回合编号：begin_new_match 之后第一次 round_freeze_end 为第 1 回合（和 duels.py load() 一致），
直接沿用 work/<key>/base/rounds.csv（= extract_pro.py 的 rounds_<id>.csv）的 start_tick（冻结结束）/end_tick（round_end）。
时间 t 一律是"冻结结束后第几秒"，保留 1 位小数；坐标取整。

和原脚本的区别（只有这些）：
  · demo 路径 / id / 用户 / 输出目录都从 ctx 来；这一场的底稿（回合、用户交火、sit 局势、本场位置）读 work/<key>/base/；
  · 场次（meta.match_no）：旧的多场报告 report/duels_de_mirage_<用户名>.csv 里查得到才填，否则 None；
  · 用户某回合不在场（掉线/晚进）时，这回合的阵营按队友推，不报错；计分板名单取所有回合在场的人；
  · 报点中文名用 config.PLACE_CN（rules_v1 C-PLACE：Shop=超市、Scaffolding=跳台、去掉英文括号）；
  · 去掉"最终比分是不是 10:13"这条只对第 3 场成立的检查；
  · 开头先检查地图（只支持 Mirage）和用户在不在 demo 里。
"""
import json
import math
from collections import Counter, defaultdict

import numpy as np
import pandas as pd
from demoparser2 import DemoParser

from .. import config
from . import FactsError, match_no, positions_refs, rounds_csv, sit_rows, user_duels

OUTPUTS = ['timeline.json', 'match_overview.json', 'match_overview.csv']

TR = config.TICKRATE
TRADE_TICKS = 5 * TR          # 5 秒内换掉凶手算"被补枪"
FLASH_LOOKBACK = 5 * TR        # 死前 5 秒是否被闪

PLACE_CN = config.PLACE_CN
REASON_CN = {
    'smoke': '被穿烟', 'flashed': '白了被杀', 'utildmg': '被道具消耗', 'crossed': '被交叉火力',
    'outpeeked': '架点被拉掉', 'teammate': '队友位置被打穿', 'info': '队友报了点没防住', 'flank': '被绕后',
    'peekedinto': '拉枪撞枪口', 'slow': '没做好开枪准备', 'aim': '预瞄不到位', 'nokill': '没补死',
}
RESP_CN = {'self': '你的责任', 'team': '队友/团队', 'cost': '合理代价'}
ROUND_REASON_CN = {'ct_killed': 'CT 全灭', 't_killed': 'T 全灭', 'bomb_defused': '拆包', 'target_bombed': '炸包',
                   'time_ran_out': '时间到'}
SITE_OF_PLACE = {'BombsiteA': 'A', 'BombsiteB': 'B'}


def site_map(planted):
    """包点实体编号 → 'A'/'B'（每个 demo 编号不一样，不能写死）：
    同一个编号的下包人报点多数是 BombsiteA/B 就用它；报点认不出时按下包位置（Mirage 的 A 点在地图下方、y 小于 -1000）"""
    votes = {}
    for _, b in planted.iterrows():
        sid = int(b['site'])
        s = SITE_OF_PLACE.get(b['user_last_place_name'])
        if s is None:
            s = 'A' if float(b['user_Y']) < -1000 else 'B'
        votes.setdefault(sid, {}).setdefault(s, 0)
        votes[sid][s] += 1
    return {sid: sorted(v, key=lambda k: (-v[k], k))[0] for sid, v in sorted(votes.items())}

PISTOLS = {'Glock-18', 'USP-S', 'P2000', 'P250', 'Five-SeveN', 'Tec-9', 'CZ75-Auto', 'Desert Eagle',
           'Dual Berettas', 'R8 Revolver'}
PRIMARIES = {'AK-47', 'M4A4', 'M4A1-S', 'AWP', 'Galil AR', 'FAMAS', 'SG 553', 'AUG', 'SSG 08', 'G3SG1', 'SCAR-20',
             'MP9', 'MAC-10', 'MP7', 'MP5-SD', 'UMP-45', 'P90', 'PP-Bizon', 'Nova', 'XM1014', 'MAG-7', 'Sawed-Off',
             'M249', 'Negev'}
NADES = {'Smoke Grenade': 'smoke', 'Flashbang': 'flash', 'High Explosive Grenade': 'he', 'Molotov': 'molotov',
         'Incendiary Grenade': 'incendiary', 'Decoy Grenade': 'decoy'}
NADE_WEAPONS = {'weapon_smokegrenade', 'weapon_flashbang', 'weapon_hegrenade', 'weapon_molotov',
                'weapon_incgrenade', 'weapon_decoy'}


def r1(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 1)


def ri(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else int(round(float(x)))


def cn(place):
    if place is None or (isinstance(place, float) and math.isnan(place)) or place == '':
        return None
    return PLACE_CN.get(place, place)


def nn(v):
    """把 pandas/numpy 的空值、numpy 标量转成 JSON 友好的值。"""
    if v is None:
        return None
    if isinstance(v, (float, np.floating)) and math.isnan(v):
        return None
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return float(v)
    return v


def as_df(x, cols=()):
    return x if isinstance(x, pd.DataFrame) else pd.DataFrame(columns=list(cols))


BAD_DEMO_MSG = '这个文件不是 CS2 demo，或者文件不完整、损坏了，重新下载一次再放进来'


def check_demo(ctx, p=None):
    """只支持 Mirage；用户必须在 demo 里。不满足就抛 FactsError（中文说明）。返回 (header, 玩家表)。"""
    try:
        p = p or DemoParser(str(ctx.dem))
        header = p.parse_header()
    except Exception as e:                 # noqa: BLE001  demoparser 对坏文件只抛英文的 Exception('UnknownFile') 之类
        raise FactsError(BAD_DEMO_MSG) from e
    mp = header.get('map_name')
    if mp not in config.SUPPORTED_MAPS:
        raise FactsError(f'目前只支持 Mirage（这个 demo 是 {mp}）')
    info = p.parse_player_info()
    roster = {str(s): str(n) for s, n in zip(info['steamid'], info['name'])}
    if ctx.user not in roster:
        # 有的 demo 没有 player_info：再用每回合冻结结束那一帧的在场玩家补一遍
        fe = p.parse_event('round_freeze_end')
        ticks = sorted(set(int(t) for t in fe['tick'])) if isinstance(fe, pd.DataFrame) and len(fe) else []
        if ticks:
            t = p.parse_ticks(['team_num'], ticks=ticks)
            t = t[t['team_num'].isin([2, 3])]
            for s, n in zip(t['steamid'], t['name']):
                roster.setdefault(str(s), str(n))
    if ctx.user not in roster:
        people = '；'.join(f'{n}（{s}）' for s, n in sorted(roster.items(), key=lambda x: (x[1].lower(), x[0])))
        raise FactsError(f'这个 demo 里没有用户 {ctx.user}。demo 里的玩家：{people or "（读不到玩家名单）"}')
    return header, roster


def run(ctx):
    """解析 ctx.dem，写 timeline.json / match_overview.json / match_overview.csv。返回一句中文小结。
    （要不要重跑由 pipeline 决定：调用了就重算。）"""
    DEM = ctx.dem
    DEMO = ctx.id
    USER = ctx.user
    OUT = ctx.work
    OUT.mkdir(parents=True, exist_ok=True)
    issues = []   # 数据问题

    # ---------------------------------------------------------------- 读取
    print('解析 demo ...', flush=True)
    p = DemoParser(str(DEM))
    header, _ = check_demo(ctx, p)
    match_start = int(p.parse_event('begin_new_match')['tick'].max())

    R = pd.read_csv(rounds_csv(ctx))
    R = R.sort_values('round').reset_index(drop=True)
    freeze_end_ev = sorted(int(t) for t in p.parse_event('round_freeze_end')['tick'] if t > match_start)
    if freeze_end_ev != [int(t) for t in R.start_tick]:
        issues.append('base/rounds.csv 的 start_tick 和 demo 里 begin_new_match 之后的 round_freeze_end 不一致')
    round_start_ev = sorted(int(t) for t in p.parse_event('round_start')['tick'])   # 第 1 回合的 round_start 比 begin_new_match 早 1 tick
    buy_end_ev = sorted(int(t) for t in p.parse_event('buytime_ended')['tick'] if t > match_start)

    rounds = []
    for i, r in R.iterrows():
        st = int(r.start_tick)
        fs = max([t for t in round_start_ev if t <= st], default=st - 15 * TR)       # 冻结开始
        be = min([t for t in buy_end_ev if t > st], default=st + 20 * TR)          # 购买时间结束
        rounds.append(dict(round=int(r['round']), start=st, freeze_start=fs, buy_end=be, end=int(r.end_tick),
                           dur=float(r.dur_s), winner=r.winner, reason=r.reason))
    for i, rd in enumerate(rounds):
        # 事件归属窗口：本回合冻结开始 → 下回合冻结开始（含回合结束后的残局击杀，另外打标记）
        rd['win_lo'] = rd['freeze_start']
        rd['win_hi'] = rounds[i + 1]['freeze_start'] if i + 1 < len(rounds) else rd['end'] + 15 * TR
    RN = {rd['round']: rd for rd in rounds}


    def round_of(tick):
        for rd in rounds:
            if rd['win_lo'] <= tick < rd['win_hi']:
                return rd['round']
        return None


    def tsec(tick, rnd):
        return r1((tick - RN[rnd]['start']) / TR)


    def norm_sid(x):
        if x is None or (not isinstance(x, str) and pd.isna(x)):
            return None
        x = str(x)
        return None if x in ('', 'nan', 'None', '0', '<NA>') else x


    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, np.ndarray):
            return [clean(v) for v in o.tolist()]
        if o is pd.NA:
            return None
        return nn(o)


    def ev(name, **kw):
        d = as_df(p.parse_event(name, **kw))
        if not len(d):
            # 这场一次都没发生的事件（例如没有拆包）：给一张带常用列的空表，后面按回合/人筛选不会缺列
            d = pd.DataFrame({c: pd.Series(dtype=object) for c in d.columns})
            for c in ('tick', 'round'):
                d[c] = pd.Series(dtype='int64')
            for c in ('user_steamid', 'user_name', 'attacker_steamid', 'attacker_name'):
                if c not in d:
                    d[c] = pd.Series(dtype=object)
            return d
        if len(d):
            d = d[d['tick'] > match_start].copy()
            for c in d.columns:
                if c.endswith('steamid'):
                    d[c] = pd.Series([norm_sid(x) for x in d[c]], index=d.index, dtype=object)
            d['round'] = [round_of(int(t)) for t in d['tick']]
            d = d[d['round'].notna()].copy()
            d['round'] = d['round'].astype(int)
        return d


    PF = ['X', 'Y', 'Z', 'last_place_name', 'team_num']
    deaths = ev('player_death', player=PF)
    hurts = ev('player_hurt', player=PF)
    fires = ev('weapon_fire')
    blinds = ev('player_blind', player=['team_num'])
    purch = ev('item_purchase')
    smoke_det = ev('smokegrenade_detonate')
    smoke_exp = ev('smokegrenade_expired')
    flash_det = ev('flashbang_detonate')
    he_det = ev('hegrenade_detonate')
    fire_start = ev('inferno_startburn')
    fire_exp = ev('inferno_expire')
    planted = ev('bomb_planted', player=['X', 'Y', 'last_place_name'])
    defused = ev('bomb_defused', player=['X', 'Y', 'last_place_name'])
    print(f'events: deaths {len(deaths)} hurts {len(hurts)} fires {len(fires)} blinds {len(blinds)}', flush=True)

    G = p.parse_grenades()
    G = G[G['x'].notna() & (G['tick'] > match_start)].copy()
    G['steamid'] = G['steamid'].astype(str)

    # ---------------------------------------------------------------- 名字 / 阵营
    name_of = {}
    for d, pre in ((deaths, 'attacker_'), (deaths, 'user_'), (hurts, 'attacker_'), (hurts, 'user_')):
        for s, n in zip(d[pre + 'steamid'], d[pre + 'name']):
            if s and isinstance(n, str):
                name_of[s] = n

    # ---------------------------------------------------------------- 手雷段落（扔出 → 起爆）
    KIND = {'CSmokeGrenadeProjectile': 'smoke', 'CFlashbangProjectile': 'flash', 'CHEGrenadeProjectile': 'he',
            'CMolotovProjectile': 'molotov', 'CDecoyProjectile': 'decoy'}
    G = G.sort_values(['grenade_entity_id', 'steamid', 'tick'])
    nades = []
    for (eid, sid), g in G.groupby(['grenade_entity_id', 'steamid'], sort=False):
        tk = g['tick'].values
        cut = np.where(np.diff(tk) > 32)[0]
        starts = np.r_[0, cut + 1]
        ends = np.r_[cut, len(tk) - 1]
        for a, b in zip(starts, ends):
            seg = g.iloc[a:b + 1]
            kind = KIND.get(seg['grenade_type'].iat[0], seg['grenade_type'].iat[0])
            t0, t1 = int(seg['tick'].iat[0]), int(seg['tick'].iat[-1])
            rnd = round_of(t0)
            if rnd is None:
                continue
            nd = dict(eid=int(eid), steamid=sid, kind=kind, round=rnd, throw_tick=t0, end_tick=t1,
                      first_xyz=(seg['x'].iat[0], seg['y'].iat[0], seg['z'].iat[0]),
                      last_xyz=(seg['x'].iat[-1], seg['y'].iat[-1], seg['z'].iat[-1]),
                      det_tick=None, det_xyz=None, expire_tick=None, inferno_eid=None)
            det = {'smoke': smoke_det, 'flash': flash_det, 'he': he_det}.get(kind)
            if det is not None and len(det):
                m = det[(det['entityid'] == eid) & (det['user_steamid'] == sid) & (det['tick'] >= t0) & (det['tick'] <= t1 + 16)]
                if len(m):
                    e = m.iloc[0]
                    nd['det_tick'] = int(e['tick']); nd['det_xyz'] = (e['x'], e['y'], e['z'])
            if kind == 'molotov' and len(fire_start):
                m = fire_start[(fire_start['user_steamid'] == sid) & (fire_start['tick'] >= t1 - 16) & (fire_start['tick'] <= t1 + 64)]
                if len(m):
                    e = m.iloc[(m['tick'] - t1).abs().argsort().iat[0]]
                    nd['det_tick'] = int(e['tick']); nd['det_xyz'] = (e['x'], e['y'], e['z'])
                    nd['inferno_eid'] = int(e['entityid'])
                    if len(fire_exp):
                        x = fire_exp[(fire_exp['entityid'] == e['entityid']) & (fire_exp['tick'] > e['tick'])]
                        if len(x):
                            nd['expire_tick'] = int(x['tick'].min())
            if kind == 'smoke' and nd['det_tick'] and len(smoke_exp):
                x = smoke_exp[(smoke_exp['entityid'] == eid) & (smoke_exp['tick'] > nd['det_tick'])]
                if len(x):
                    nd['expire_tick'] = int(x['tick'].min())
            nades.append(nd)
    nades.sort(key=lambda d: d['throw_tick'])
    print(f'grenades: {len(nades)}', flush=True)

    # ---------------------------------------------------------------- 关键时刻（先用事件算，再一次性解析快照）
    DU = user_duels(ctx)                # 用户本场交火（base/user_duels.csv）
    MATCH_NO = match_no(ctx, DU)        # 旧多场报告里的场次号，查不到是 None
    for _, d in DU.iterrows():
        rr = round_of(int(d['tick']))
        if rr != int(d['回合']):
            issues.append(f'duels csv 第 {d["回合"]} 回合 tick {d["tick"]} 实际落在第 {rr} 回合')

    user_death_tick = {}
    for _, d in deaths[deaths['user_steamid'] == USER].iterrows():
        user_death_tick.setdefault(int(d['round']), int(d['tick']))

    first_fight_tick = {}
    for rd in rounds:
        rnd = rd['round']
        c = []
        du = DU[DU['回合'] == rnd]
        if len(du):
            c.append(int(du['开打tick'].min()))
        h = hurts[(hurts['round'] == rnd) & ((hurts['attacker_steamid'] == USER) | (hurts['user_steamid'] == USER))
                  & (hurts['attacker_team_num'] != hurts['user_team_num']) & hurts['attacker_steamid'].notna()]
        if len(h):
            c.append(int(h['tick'].min()))
        if c:
            first_fight_tick[rnd] = min(c)

    SITE = site_map(planted)
    plant_row = {}
    for _, b in planted.iterrows():
        rnd = int(b['round'])
        if int(b['tick']) <= RN[rnd]['end'] and rnd not in plant_row:
            plant_row[rnd] = b
        elif int(b['tick']) > RN[rnd]['end']:
            issues.append(f'第 {rnd} 回合 {tsec(int(b["tick"]), rnd)} 秒有一次回合结束后的下包（{b["user_name"]}），不计入本回合 plant')

    snap_ticks = set()
    for rd in rounds:
        rnd = rd['round']
        snap_ticks |= {rd['start'], rd['buy_end'], rd['freeze_start'] + 2}
        if rnd in user_death_tick:
            snap_ticks |= {user_death_tick[rnd], user_death_tick[rnd] - 1}
        if rnd in first_fight_tick:
            snap_ticks.add(first_fight_tick[rnd])
        if rnd in plant_row:
            snap_ticks.add(int(plant_row[rnd]['tick']))
    for nd in nades:
        snap_ticks.add(nd['throw_tick'])
    snap_ticks = sorted(snap_ticks)
    SP = ['X', 'Y', 'Z', 'last_place_name', 'team_num', 'health', 'is_alive', 'active_weapon_name', 'armor_value',
          'has_helmet', 'has_defuser', 'current_equip_value', 'balance', 'cash_spent_this_round', 'inventory']
    print(f'快照 tick {len(snap_ticks)} 个 ...', flush=True)
    S = p.parse_ticks(SP, ticks=snap_ticks)
    S['steamid'] = S['steamid'].astype(str)
    S = S[S['team_num'].isin([2, 3])]
    SNAP = {tk: g.set_index('steamid') for tk, g in S.groupby('tick')}
    for _, r in S.iterrows():
        name_of.setdefault(r['steamid'], r['name'])


    def snap(tick, sid=None):
        s = SNAP.get(tick)
        if s is None:
            return None
        if sid is None:
            return s
        return s.loc[sid] if sid in s.index else None


    # 阵营（按冻结结束那一刻）
    team_of = {}
    for rd in rounds:
        s = snap(rd['start'])
        team_of[rd['round']] = {sid: ('T' if tn == 2 else 'CT') for sid, tn in s['team_num'].items()}
        if USER not in team_of[rd['round']]:
            issues.append(f'第 {rd["round"]} 回合冻结结束时找不到用户')
        if len(s) != 10:
            issues.append(f'第 {rd["round"]} 回合冻结结束时只有 {len(s)} 名玩家')

    # 用户每回合的阵营：在场就用 demo 里的；不在场（掉线/晚进）按最近一个在场回合的队友这回合站哪边推
    present = [rd['round'] for rd in rounds if USER in team_of[rd['round']]]
    if not present:
        raise FactsError(f'这个 demo 里用户 {USER} 一个回合都没有上场')
    USIDE = {}
    for rd in rounds:
        rnd = rd['round']
        if USER in team_of[rnd]:
            USIDE[rnd] = team_of[rnd][USER]
            continue
        r0 = min(present, key=lambda x: (abs(x - rnd), x))
        mates0 = [sid for sid, sd in team_of[r0].items() if sd == team_of[r0][USER] and sid != USER]
        votes = Counter(team_of[rnd][sid] for sid in mates0 if sid in team_of[rnd])
        USIDE[rnd] = sorted(votes, key=lambda k: (-votes[k], k))[0] if votes else team_of[r0][USER]

    # 用户逐秒路径
    path_ticks = []
    for rd in rounds:
        stop = min(user_death_tick.get(rd['round'], rd['end']), rd['end'])
        for k in range(0, (stop - rd['start']) // TR + 1):
            tk = rd['start'] + k * TR
            path_ticks += [tk, tk - 8 if k > 0 else tk + 8]
    print(f'用户路径 tick {len(path_ticks)} 个 ...', flush=True)
    UP = p.parse_ticks(['X', 'Y', 'Z', 'last_place_name', 'health', 'armor_value', 'active_weapon_name', 'is_walking',
                        'duck_amount', 'is_scoped', 'yaw', 'is_alive'], ticks=sorted(set(path_ticks)), players=[int(USER)])
    UP = UP.set_index('tick')

    # ---------------------------------------------------------------- 落点报点（坐标 → 地点名）
    ref = []
    for f in positions_refs(ctx):        # 其他场的静态位置 + 本场 base/positions.csv
        ref.append(pd.read_csv(f, usecols=['X', 'Y', 'Z', 'place']))
    ref.append(S[['X', 'Y', 'Z', 'last_place_name']].rename(columns={'last_place_name': 'place'}))
    ref = pd.concat(ref, ignore_index=True).dropna()
    ref = ref[ref['place'] != '']
    ref['gx'] = (ref['X'] / 32).round(); ref['gy'] = (ref['Y'] / 32).round(); ref['gz'] = (ref['Z'] / 48).round()
    cells = ref.groupby(['gx', 'gy', 'gz']).agg(X=('X', 'mean'), Y=('Y', 'mean'), Z=('Z', 'mean'),
                                               place=('place', lambda s: s.value_counts().index[0])).reset_index()
    CX, CY, CZ, CP = cells['X'].values, cells['Y'].values, cells['Z'].values, cells['place'].values


    def place_at(x, y, z, air=False):
        if x is None:
            return None
        if air:      # 闪光在空中爆：只按平面找，并排除比它高很多的地面格子
            d = (CX - x) ** 2 + (CY - y) ** 2 + np.where(CZ > z + 40, 1e9, 0)
        else:
            d = (CX - x) ** 2 + (CY - y) ** 2 + (1.5 * (CZ - z)) ** 2
        i = int(np.argmin(d))
        return CP[i] if d[i] < 400 ** 2 else None


    # ---------------------------------------------------------------- 伤害（有效伤害，扣掉溢出）
    hurts = hurts.sort_values('tick').reset_index(drop=True)
    hp_now = {}
    eff = []
    for _, h in hurts.iterrows():
        key = (int(h['round']), h['user_steamid'])
        before = hp_now.get(key, 100)
        e = min(int(h['dmg_health']), before)
        eff.append(e)
        hp_now[key] = int(h['health'])
    hurts['dmg'] = eff


    def side_of(rnd, sid):
        return team_of[rnd].get(sid)


    # ---------------------------------------------------------------- 击杀（首杀、补枪）
    deaths = deaths.sort_values('tick').reset_index(drop=True)
    kills_by_round = defaultdict(list)
    for _, d in deaths.iterrows():
        rnd = int(d['round']); rd = RN[rnd]
        tk = int(d['tick'])
        vs, ks = d['user_steamid'], d['attacker_steamid']
        vside = side_of(rnd, vs)
        kside = side_of(rnd, ks) if ks else None
        kills_by_round[rnd].append(dict(
            tick=tk, t=tsec(tk, rnd), post_round=tk > rd['end'],
            killer=name_of.get(ks, ks) if ks else 'world', killer_id=ks, killer_side=kside,
            killer_place=d['attacker_last_place_name'] if ks else None, killer_place_cn=cn(d['attacker_last_place_name']) if ks else None,
            killer_xy=[ri(d['attacker_X']), ri(d['attacker_Y'])] if ks else None,
            victim=name_of.get(vs, vs), victim_id=vs, victim_side=vside,
            victim_place=d['user_last_place_name'], victim_place_cn=cn(d['user_last_place_name']),
            victim_xy=[ri(d['user_X']), ri(d['user_Y'])],
            weapon=d['weapon'], headshot=bool(d['headshot']), thrusmoke=bool(d['thrusmoke']),
            penetrated=int(d['penetrated']) > 0, attackerblind=bool(d['attackerblind']), noscope=bool(d['noscope']),
            assister=name_of.get(d['assister_steamid'], d['assister_steamid']) if d['assister_steamid'] else None,
            assister_id=d['assister_steamid'], flash_assist=bool(d['assistedflash']),
            distance_m=r1(d['distance']) if pd.notna(d['distance']) else None,   # demoparser2 给的是米
            teamkill=bool(ks and kside == vside and ks != vs), suicide=bool(ks == vs) or not ks,
        ))
    for rnd, ks in kills_by_round.items():
        live = [k for k in ks if not k['post_round']]
        for i, k in enumerate(ks):
            k['opening'] = bool(live) and k is live[0]
            # 被补枪：凶手在 5 秒内被死者的队友杀掉
            k['traded'] = any(o['victim_id'] == k['killer_id'] and o['killer_side'] == k['victim_side']
                              and 0 <= o['tick'] - k['tick'] <= TRADE_TICKS for o in ks[i + 1:]) if k['killer_id'] else False
            k['trade_kill'] = any(o['killer_id'] == k['victim_id'] and o['victim_side'] == k['killer_side']
                                  and 0 <= k['tick'] - o['tick'] <= TRADE_TICKS for o in ks[:i])

    # ---------------------------------------------------------------- sit_rounds
    SIT = sit_rows(ctx)
    SIT = SIT[SIT['demo'] == DEMO]


    def classify_inv(inv):
        inv = list(inv) if isinstance(inv, (list, tuple, np.ndarray)) else []
        prim = [w for w in inv if w in PRIMARIES]
        pist = [w for w in inv if w in PISTOLS]
        nd = [w for w in inv if w in NADES]
        other = [w for w in inv if w not in PRIMARIES | PISTOLS | set(NADES)]
        return dict(primary=prim[0] if prim else None, pistol=pist[0] if pist else None, nades=nd,
                    c4='C4 Explosive' in other, zeus='Zeus x27' in other)


    def team_equip(tick, side, rnd):
        s = snap(tick)
        ids = [sid for sid, sd in team_of[rnd].items() if sd == side]
        v = [int(s.loc[i, 'current_equip_value']) for i in ids if i in s.index]
        return dict(sum=int(sum(v)), mean=ri(np.mean(v)) if v else None, per_player={name_of.get(i, i): int(s.loc[i, 'current_equip_value']) for i in ids if i in s.index})


    def pos_entry(sid, tick, ref_xy, rnd):
        s = snap(tick, sid)
        if s is None:
            return None
        alive = bool(s['is_alive']) and int(s['health']) > 0
        e = dict(name=name_of.get(sid, sid), alive=alive, hp=int(s['health']),
                 place=s['last_place_name'] if cn(s['last_place_name']) else None, place_cn=cn(s['last_place_name']),
                 xy=[ri(s['X']), ri(s['Y'])], weapon=s['active_weapon_name'] if alive else None)
        e['dist'] = ri(math.hypot(s['X'] - ref_xy[0], s['Y'] - ref_xy[1])) if ref_xy else None
        if not alive:
            # 死人的快照坐标停在死亡点附近，距离仍按死亡点算
            e['note'] = '已阵亡'
        return e


    # ---------------------------------------------------------------- 逐回合
    us_score, them_score = 0, 0
    timeline = []
    for rd in rounds:
        rnd = rd['round']; st = rd['start']; end = rd['end']
        side = USIDE[rnd]
        enemy_side = 'CT' if side == 'T' else 'T'
        mates = [s for s, sd in team_of[rnd].items() if sd == side and s != USER]
        enemies = [s for s, sd in team_of[rnd].items() if sd == enemy_side]
        won = rd['winner'] == side
        item = dict(round=rnd, side=side, score_before=f'{us_score}:{them_score}', winner=rd['winner'],
                    result='赢' if won else '输', reason=rd['reason'], reason_cn=ROUND_REASON_CN.get(rd['reason'], rd['reason']),
                    dur=r1(rd['dur']))
        # ---- 下包 / 拆包
        if rnd in plant_row:
            b = plant_row[rnd]
            item['plant'] = dict(t=tsec(int(b['tick']), rnd), tick=int(b['tick']), site=SITE.get(int(b['site']), ''),
                                 planter=b['user_name'], planter_side='T', by_user=b['user_steamid'] == USER,
                                 planter_place=b['user_last_place_name'], planter_place_cn=cn(b['user_last_place_name']),
                                 xy=[ri(b['user_X']), ri(b['user_Y'])])
        else:
            item['plant'] = None
        df_ = defused[defused['round'] == rnd]
        item['defuse'] = (dict(t=tsec(int(df_['tick'].iat[0]), rnd), defuser=df_['user_name'].iat[0],
                               by_user=df_['user_steamid'].iat[0] == USER) if len(df_) else None)

        # ---- 经济
        sit = SIT[(SIT['round'] == rnd) & (SIT['side'] == side)]
        sit_e = SIT[(SIT['round'] == rnd) & (SIT['side'] == enemy_side)]
        buy_tick = rd['buy_end']
        kr = [k['tick'] for k in kills_by_round.get(rnd, []) if not k['post_round']]
        team_buy_tick = min(buy_tick, kr[0] - 1) if kr and kr[0] - 1 >= st else buy_tick
        if team_buy_tick not in SNAP:
            team_buy_tick = buy_tick
        u_tick = buy_tick
        if rnd in user_death_tick and user_death_tick[rnd] - 1 < buy_tick:
            u_tick = user_death_tick[rnd] - 1
        # 购买结束前已经有人倒下：背包看第一次死人之前那一刻，之后的枪可能是捡来的（e6b 高分玩家84 R6）
        if team_buy_tick < u_tick and snap(team_buy_tick, USER) is not None:
            u_tick = team_buy_tick
        us = snap(u_tick, USER)
        us_fe = snap(st, USER)
        inv_buy = classify_inv(us['inventory']) if us is not None else {}
        inv_fe = classify_inv(us_fe['inventory']) if us_fe is not None else {}
        up = purch[(purch['round'] == rnd) & (purch['steamid'].astype(str) == USER) & (purch['tick'] <= buy_tick)]
        purchases = [dict(t=tsec(int(x['tick']), rnd), item=x['item_name'], cost=int(x['cost']), refunded=bool(x['was_sold']))
                     for _, x in up.iterrows()]
        us_fs = snap(rd['freeze_start'] + 2, USER)
        # 开局钱 = 冻结开始那一刻的余额（上回合带下来的枪不算钱）
        start_money = int(us_fs['balance']) if us_fs is not None else (
            int(us['balance']) + int(us['cash_spent_this_round']) if us is not None else None)
        item['econ'] = dict(
            situation=nn(sit['situation'].iat[0]) if len(sit) else None,
            sub=nn(sit['sub'].iat[0]) if len(sit) else None,
            econ_sit=nn(sit['econ_sit'].iat[0]) if len(sit) else None,
            econ_detail=nn(sit['econ_detail'].iat[0]) if len(sit) else None,
            our_buy=nn(sit['buy'].iat[0]) if len(sit) else None, enemy_buy=nn(sit['enemy_buy'].iat[0]) if len(sit) else None,
            enemy_econ_sit=nn(sit_e['econ_sit'].iat[0]) if len(sit_e) else None,
            equip_freeze_end={'us': team_equip(st, side, rnd), 'them': team_equip(st, enemy_side, rnd)},
            equip_after_buy={'tick': team_buy_tick, 'us': team_equip(team_buy_tick, side, rnd),
                             'them': team_equip(team_buy_tick, enemy_side, rnd)},
            user=dict(start_money=start_money,
                      carried_in=list(us_fs['inventory']) if us_fs is not None else None,
                      money_left=int(us['balance']) if us is not None else None,
                      spent=int(us['cash_spent_this_round']) if us is not None else None,
                      equip_value=int(us['current_equip_value']) if us is not None else None,
                      primary=inv_buy.get('primary'), pistol=inv_buy.get('pistol'),
                      armor=int(us['armor_value']) if us is not None else None,
                      helmet=bool(us['has_helmet']) if us is not None else None,
                      defuser=bool(us['has_defuser']) if us is not None else None,
                      nades=inv_buy.get('nades'), c4=inv_buy.get('c4'),
                      inventory_tick=u_tick,
                      inventory_at_freeze_end=list(us_fe['inventory']) if us_fe is not None else None,
                      purchases=purchases),
        )
        nade_buys = Counter(NADES.get(x['item'], None) for x in purchases if not x['refunded'] and x['item'] in NADES)
        if nade_buys and sum(nade_buys.values()) > len(inv_buy.get('nades') or []):
            item['econ']['user']['nade_note'] = '买的道具数量多于 inventory 列出的（inventory 里同类道具可能只列一次，或已经扔掉）'

        # ---- 用户路径
        stop = min(user_death_tick.get(rnd, end), end)
        path = []
        for k in range(0, (stop - st) // TR + 1):
            tk = st + k * TR
            if tk not in UP.index:
                continue
            a = UP.loc[tk]
            b_tk = tk - 8 if k > 0 else tk + 8
            spd = None
            if b_tk in UP.index:
                b = UP.loc[b_tk]
                spd = math.hypot(a['X'] - b['X'], a['Y'] - b['Y']) / (8 / TR)
            if not bool(a['is_alive']):
                break
            mv = None if spd is None else ('静止' if spd < 10 else ('静步' if bool(a['is_walking']) or spd < 140 else '跑'))
            path.append(dict(t=float(k), X=ri(a['X']), Y=ri(a['Y']), Z=ri(a['Z']), place=a['last_place_name'] or None,
                             place_cn=cn(a['last_place_name']), hp=int(a['health']), armor=int(a['armor_value']),
                             weapon=a['active_weapon_name'], speed=ri(spd), moving=mv,
                             crouch=float(a['duck_amount']) > 0.5, scoped=bool(a['is_scoped']), yaw=ri(a['yaw'])))
        item['user_path'] = path

        # ---- 道具
        def nade_entry(nd, full):
            thr = snap(nd['throw_tick'], nd['steamid'])
            land = nd['det_xyz'] or nd['last_xyz']
            kind = nd['kind']
            if kind == 'molotov' and side_of(rnd, nd['steamid']) == 'CT':
                kind = 'incendiary'
            lp = place_at(land[0], land[1], land[2], air=nd['kind'] == 'flash')
            e = dict(t=tsec(nd['throw_tick'], rnd), kind=kind,
                     land_place=lp, land_place_cn=cn(lp), land_xy=[ri(land[0]), ri(land[1])],
                     det_t=tsec(nd['det_tick'], rnd) if nd['det_tick'] else None)
            if full:
                e['throw_place'] = (thr['last_place_name'] or None) if thr is not None else None
                e['throw_place_cn'] = cn(e['throw_place'])
                e['throw_xy'] = [ri(thr['X']), ri(thr['Y'])] if thr is not None else [ri(nd['first_xyz'][0]), ri(nd['first_xyz'][1])]
                e['land_z'] = ri(land[2])
                e['throw_tick'] = nd['throw_tick']
            if nd['expire_tick']:
                e['end_t'] = tsec(nd['expire_tick'], rnd)
            if nd['det_tick'] is None and nd['kind'] in ('smoke', 'flash', 'he', 'molotov'):
                e['note'] = '没找到起爆事件（火被烟灭/扔出时回合已结束等），落点取弹体最后位置'
            thrower_side = side_of(rnd, nd['steamid'])
            # 效果
            if nd['kind'] == 'flash' and nd['det_tick']:
                bl = blinds[(blinds['entityid'] == nd['eid']) & (blinds['attacker_steamid'] == nd['steamid'])
                            & ((blinds['tick'] - nd['det_tick']).abs() <= 4)]
                # 闪爆的时候已经死了的人（观战视角也会收到 player_blind）不算被闪到（128 R11：高分玩家13 22.0 秒死，闪 22.9 秒才爆）
                dead_at = {k['victim_id']: k['tick'] for k in kills_by_round.get(rnd, [])}
                e['blinded'] = [dict(name=name_of.get(x['user_steamid'], x['user_steamid']),
                                     enemy=side_of(rnd, x['user_steamid']) != thrower_side,
                                     dur=r1(x['blind_duration']),
                                     dur_raw=round(float(x['blind_duration']), 6)) for _, x in bl.iterrows()
                                if float(x['blind_duration']) >= 0.5 and x['user_steamid'] != nd['steamid']
                                and not (x['user_steamid'] in dead_at and dead_at[x['user_steamid']] < nd['det_tick'])]
            if nd['kind'] == 'he' and nd['det_tick']:
                hh = hurts[(hurts['attacker_steamid'] == nd['steamid']) & (hurts['weapon'] == 'hegrenade')
                           & ((hurts['tick'] - nd['det_tick']).abs() <= 4)]
                e['damage'] = [dict(name=name_of.get(x['user_steamid']), enemy=side_of(rnd, x['user_steamid']) != thrower_side,
                                    dmg=int(x['dmg'])) for _, x in hh.iterrows() if int(x['dmg']) > 0]
            if nd['kind'] == 'molotov' and nd['det_tick']:
                hi = nd['expire_tick'] or nd['det_tick'] + 7 * TR
                hh = hurts[(hurts['attacker_steamid'] == nd['steamid']) & hurts['weapon'].isin(['inferno', 'molotov', 'incgrenade'])
                           & (hurts['tick'] >= nd['det_tick']) & (hurts['tick'] <= hi + 8)]
                agg = hh.groupby('user_steamid')['dmg'].sum()
                e['damage'] = [dict(name=name_of.get(s), enemy=side_of(rnd, s) != thrower_side, dmg=int(v)) for s, v in agg.items() if v > 0]
            return e

        rn = [nd for nd in nades if nd['round'] == rnd]
        item['user_util'] = [nade_entry(nd, True) for nd in rn if nd['steamid'] == USER]
        item['team_util'] = [dict(who=name_of.get(nd['steamid']), **nade_entry(nd, True)) for nd in rn if nd['steamid'] in mates]
        item['enemy_util'] = [dict(who=name_of.get(nd['steamid']), **nade_entry(nd, False)) for nd in rn if nd['steamid'] in enemies]

        # ---- 交火（duels csv）
        fights = []
        for _, d in DU[DU['回合'] == rnd].sort_values('开打tick').iterrows():
            eid = str(d['敌人ID'])
            lo, hi = int(d['开打tick']) - 3 * TR, int(d['tick'])
            ex = hurts[(hurts['tick'] >= lo) & (hurts['tick'] <= hi)
                       & (((hurts['attacker_steamid'] == USER) & (hurts['user_steamid'] == eid))
                          | ((hurts['attacker_steamid'] == eid) & (hurts['user_steamid'] == USER)))]
            rc = nn(d['原因'])
            fights.append(dict(
                t=r1(d['开局第几秒']), start_t=tsec(int(d['开打tick']), rnd), start_tick=int(d['开打tick']), end_tick=int(d['tick']),
                result=d['结果'], enemy=d['敌人'], enemy_id=eid,
                my_place=d['我的报点'], my_place_cn=cn(d['我的报点']), enemy_place=d['敌人报点'], enemy_place_cn=cn(d['敌人报点']),
                my_xy=[ri(d['我X']), ri(d['我Y'])], enemy_xy=[ri(d['敌X']), ri(d['敌Y'])],
                kill_weapon=d['武器'], headshot=nn(d['爆头']),
                situation=nn(d['局势']), engage=nn(d['对枪方式']), fight_type=nn(d['交火类型']),
                aim_err=r1(nn(d['看见敌人时准星偏差°'])), aim_err_lr=r1(nn(d['左右偏差°'])), aim_err_ud=r1(nn(d['高低偏差°'])),
                see_to_end_s=(round(float(d['看见到结束秒']), 2) if pd.notna(d['看见到结束秒']) else None), first_hit=nn(d['谁先打中']), thru_smoke=nn(d['隔烟']),
                my_facing_deg=nn(d['我朝向夹角°']), enemy_facing_deg=nn(d['敌朝向夹角°']),
                enemies_seeing_me=nn(d['看见我的敌人数']), hp_at_start=nn(d['开打血量']),
                flashed=nn(d['被闪']), flashed_by_team=nn(d['被队友闪']), util_dmg_before=nn(d['道具掉血']),
                my_speed=nn(d['我速度']), enemy_speed=nn(d['敌速度']),
                enemy_just_killed_mate=nn(d['对手刚杀过队友']), mate_saw_first=nn(d['队友先看见对手']),
                alive_us=nn(d['我方存活']), alive_them=nn(d['敌方存活']),
                my_gun_tier=nn(d['我的枪档']), enemy_gun_tier=nn(d['敌人枪档']), opening_duel=nn(d['首杀对枪']),
                standard_pos=nn(d['标准位']), killer_pos_note=nn(d['凶手位置']),
                reason=rc, reason_cn=REASON_CN.get(rc) if rc else None,
                resp=nn(d['责任']), resp_cn=RESP_CN.get(nn(d['责任'])) if nn(d['责任']) else None, resp_note=nn(d['责任说明']),
                econ=nn(d['经济']), enemy_econ=nn(d['对方经济']), note=nn(d['备注']),
                exchange=[dict(t=tsec(int(x['tick']), rnd), by='我' if x['attacker_steamid'] == USER else '敌',
                               dmg=int(x['dmg']), hitgroup=x['hitgroup'], weapon=x['weapon']) for _, x in ex.iterrows()],
            ))
        item['user_fights'] = fights

        hr = hurts[hurts['round'] == rnd]
        dealt = hr[hr['attacker_steamid'] == USER]
        taken = hr[hr['user_steamid'] == USER]
        item['user_damage_dealt'] = [dict(t=tsec(int(x['tick']), rnd), victim=name_of.get(x['user_steamid']),
                                          teammate=side_of(rnd, x['user_steamid']) == side, dmg=int(x['dmg']),
                                          dmg_armor=int(x['dmg_armor']), weapon=x['weapon'], hitgroup=x['hitgroup'],
                                          victim_hp_after=int(x['health']), victim_place_cn=cn(x['user_last_place_name']),
                                          my_place_cn=cn(x['attacker_last_place_name'])) for _, x in dealt.iterrows()]
        item['user_damage_taken'] = [dict(t=tsec(int(x['tick']), rnd),
                                          attacker=name_of.get(x['attacker_steamid'], 'world') if x['attacker_steamid'] else 'world',
                                          teammate=side_of(rnd, x['attacker_steamid']) == side if x['attacker_steamid'] else False,
                                          dmg=int(x['dmg']), weapon=x['weapon'], hitgroup=x['hitgroup'], hp_after=int(x['health']),
                                          attacker_place_cn=cn(x['attacker_last_place_name']) if x['attacker_steamid'] else None,
                                          my_place_cn=cn(x['user_last_place_name'])) for _, x in taken.iterrows()]
        uf = fires[(fires['round'] == rnd) & (fires['user_steamid'] == USER) & (fires['tick'] >= st) & (fires['tick'] <= stop)
                   & ~fires['weapon'].isin(NADE_WEAPONS) & ~fires['weapon'].str.contains('knife|bayonet|c4', case=False, na=False)]
        fb = defaultdict(Counter)
        for _, x in uf.iterrows():
            fb[int((int(x['tick']) - st) // TR)][x['weapon'].replace('weapon_', '')] += 1
        item['user_fire'] = [dict(t=float(s), shots=sum(c.values()), weapon='/'.join(c)) for s, c in sorted(fb.items())]

        # ---- 击杀、存活
        ks = kills_by_round.get(rnd, [])
        s0 = snap(st)
        alive = {'T': sum(1 for sid, sd in team_of[rnd].items() if sd == 'T' and bool(s0.loc[sid, 'is_alive'])),
                 'CT': sum(1 for sid, sd in team_of[rnd].items() if sd == 'CT' and bool(s0.loc[sid, 'is_alive']))}
        alive_list = [dict(t=0.0, T=alive['T'], CT=alive['CT'], us=alive[side], them=alive[enemy_side], event='开局')]
        kout = []
        for k in ks:
            if not k['post_round'] and k['victim_side'] in alive:
                alive[k['victim_side']] -= 1
                alive_list.append(dict(t=k['t'], T=alive['T'], CT=alive['CT'], us=alive[side], them=alive[enemy_side],
                                       event=f"{k['killer']} → {k['victim']}"))
            kout.append({kk: vv for kk, vv in k.items() if kk not in ('killer_id', 'victim_id', 'assister_id')}
                        | dict(user_involved=USER in (k['killer_id'], k['victim_id']),
                               victim_is_mate=k['victim_id'] in mates, victim_is_user=k['victim_id'] == USER,
                               killer_is_user=k['killer_id'] == USER))
        item['kills'] = kout
        item['alive'] = alive_list

        # ---- 用户阵亡
        ud = None
        for k in ks:
            if k['victim_id'] == USER:
                tk = k['tick']
                bl = blinds[(blinds['user_steamid'] == USER) & (blinds['tick'] <= tk) & (blinds['tick'] >= tk - FLASH_LOOKBACK)]
                bl = bl[bl['blind_duration'] >= 0.3]
                ksnap = snap(tk, k['killer_id']) if k['killer_id'] else None
                prev = snap(tk - 1, USER)
                ud = dict(t=k['t'], tick=tk, post_round=k['post_round'], place=k['victim_place'], place_cn=k['victim_place_cn'],
                          xy=k['victim_xy'], killer=k['killer'], killer_side=k['killer_side'], killer_place=k['killer_place'],
                          killer_place_cn=k['killer_place_cn'], killer_xy=k['killer_xy'], weapon=k['weapon'],
                          headshot=k['headshot'], thrusmoke=k['thrusmoke'], killer_blind=k['attackerblind'],
                          killer_hp=int(ksnap['health']) if ksnap is not None else None,
                          my_weapon=prev['active_weapon_name'] if prev is not None else None,
                          traded=k['traded'], distance_m=k['distance_m'],
                          flashed_5s=bool(len(bl)),
                          flashes=[dict(t=tsec(int(x['tick']), rnd), by=name_of.get(x['attacker_steamid']),
                                        by_teammate=side_of(rnd, x['attacker_steamid']) == side, dur=r1(x['blind_duration']))
                                   for _, x in bl.iterrows()],
                          alive_before=None)
                # 死前双方人数
                before = [a for a in alive_list if a['t'] is not None and a['t'] < k['t']] or alive_list[:1]
                ud['alive_before'] = dict(us=before[-1]['us'], them=before[-1]['them'])
                break
        item['user_death'] = ud

        # ---- 关键时刻队友/敌人位置
        moments = {}
        if rnd in first_fight_tick:
            moments['first_fight'] = first_fight_tick[rnd]
        if ud:
            moments['user_death'] = ud['tick']
        if item['plant']:
            moments['plant'] = item['plant']['tick']
        tat, eat = {}, {}
        for mname, tk in moments.items():
            if mname == 'user_death':
                ref_xy, ualive = ud['xy'], False
            else:
                usn = snap(tk, USER)
                ualive = bool(usn['is_alive']) and int(usn['health']) > 0 if usn is not None else False
                ref_xy = [usn['X'], usn['Y']] if usn is not None else None
                if not ualive and ud:
                    ref_xy = ud['xy']
            usn = snap(tk, USER)
            tat[mname] = dict(t=tsec(tk, rnd), tick=tk, user_alive=ualive,
                              user_place_cn=(ud['place_cn'] if mname == 'user_death' else (cn(usn['last_place_name']) if usn is not None else None)),
                              mates=[e for e in (pos_entry(s, tk, ref_xy, rnd) for s in mates) if e])
            eat[mname] = dict(t=tsec(tk, rnd), enemies=[e for e in (pos_entry(s, tk, ref_xy, rnd) for s in enemies) if e])
        item['teammates_at'] = tat
        item['enemies_at'] = eat
        item['key_ticks'] = dict(freeze_start=rd['freeze_start'], round_start=st, buy_end=rd['buy_end'],
                                 first_fight=moments.get('first_fight'), user_death=ud['tick'] if ud else None,
                                 plant=item['plant']['tick'] if item['plant'] else None, round_end=end,
                                 goto_hint=f"demo_gototick {st}")
        # ---- 用户本回合小结
        item['user_summary'] = dict(
            kills=sum(1 for k in ks if k['killer_id'] == USER and not k['teamkill'] and not k['post_round']),
            deaths=1 if ud and not ud['post_round'] else 0,
            assists=sum(1 for k in ks if k['assister_id'] == USER and not k['post_round']),
            dmg=int(dealt[np.array([side_of(rnd, s) != side for s in dealt['user_steamid']], dtype=bool) & (dealt['tick'] <= end)]['dmg'].sum()),
            util_dmg=int(dealt[dealt['weapon'].isin(['hegrenade', 'inferno', 'molotov', 'incgrenade'])
                               & np.array([side_of(rnd, s) != side for s in dealt['user_steamid']], dtype=bool)]['dmg'].sum()),
            survived=ud is None or ud['post_round'],
        )
        # 数据检查
        if not path:
            issues.append(f'第 {rnd} 回合没有用户路径')
        if not len(sit):
            issues.append(f'第 {rnd} 回合 base/sit.csv 里没有用户阵营的行')
        if len(sit) and nn(sit['user_team'].iat[0]) is not True:
            issues.append(f'第 {rnd} 回合 base/sit.csv 的 user_team 标记和 demo 阵营不一致')
        if len(sit) and nn(sit['user_k'].iat[0]) is not None:
            if int(sit['user_k'].iat[0]) != item['user_summary']['kills'] or int(sit['user_d'].iat[0]) != item['user_summary']['deaths']:
                issues.append(f'第 {rnd} 回合 sit_rounds 的用户 K/D ({int(sit["user_k"].iat[0])}/{int(sit["user_d"].iat[0])}) '
                              f'和这里 ({item["user_summary"]["kills"]}/{item["user_summary"]["deaths"]}) 不一致')
        if ud and not ud['post_round'] and not any(f['result'] == '输' for f in fights):
            issues.append(f'第 {rnd} 回合用户阵亡（{ud["t"]} 秒，被 {ud["killer"]}）但 duels csv 里没有"输"的交火')
        timeline.append(item)
        if won:
            us_score += 1
        else:
            them_score += 1

    # ---------------------------------------------------------------- 计分板
    # 名单 = 所有回合冻结结束时在场的人（中途换上来的替补也算）；start_side = 这个人第一次上场时的阵营；
    # 我方/对方按第一次上场那回合和用户是不是同一边分
    first_r = {}
    for rd in rounds:
        for sid in sorted(team_of[rd['round']]):
            first_r.setdefault(sid, rd['round'])
    players = sorted(first_r)
    ours_sid = {sid for sid in players if team_of[first_r[sid]][sid] == USIDE[first_r[sid]]}
    board = {}
    for sid in players:
        b = dict(name=name_of.get(sid, sid), start_side=team_of[first_r[sid]][sid], is_user=sid == USER, k=0, d=0, a=0, hs=0, dmg=0,
                 util_dmg=0, opening_k=0, opening_d=0, trade_k=0, traded_d=0, flash_assist=0, enemies_flashed=0,
                 kast_rounds=0, rounds=len(rounds), multi={2: 0, 3: 0, 4: 0, 5: 0})
        board[sid] = b
    for item, rd in zip(timeline, rounds):
        rnd = rd['round']
        ks = [k for k in kills_by_round.get(rnd, []) if not k['post_round']]
        kc = Counter()
        kast = set()
        died = set()
        for k in ks:
            if k['killer_id'] in board and not k['teamkill'] and not k['suicide']:
                b = board[k['killer_id']]; b['k'] += 1; b['hs'] += k['headshot']; kc[k['killer_id']] += 1
                b['opening_k'] += k['opening']; b['trade_k'] += k['trade_kill']; kast.add(k['killer_id'])
            if k['victim_id'] in board:
                b = board[k['victim_id']]; b['d'] += 1; b['opening_d'] += k['opening']; b['traded_d'] += k['traded']
                died.add(k['victim_id'])
                if k['traded']:
                    kast.add(k['victim_id'])
            if k['assister_id'] in board:
                board[k['assister_id']]['a'] += 1; kast.add(k['assister_id'])
                board[k['assister_id']]['flash_assist'] += k['flash_assist']
        for sid in players:
            if sid not in died:
                kast.add(sid)
        for sid in kast:
            if sid in board:
                board[sid]['kast_rounds'] += 1
        for sid, n in kc.items():
            if n >= 2:
                board[sid]['multi'][min(n, 5)] += 1
        hr = hurts[(hurts['round'] == rnd) & (hurts['tick'] <= rd['end'])]
        for _, x in hr.iterrows():
            a, v = x['attacker_steamid'], x['user_steamid']
            if a in board and side_of(rnd, a) != side_of(rnd, v):
                board[a]['dmg'] += int(x['dmg'])
                if x['weapon'] in ('hegrenade', 'inferno', 'molotov', 'incgrenade'):
                    board[a]['util_dmg'] += int(x['dmg'])
        bl = blinds[(blinds['round'] == rnd) & (blinds['blind_duration'] >= 1.0)]
        for _, x in bl.iterrows():
            a = x['attacker_steamid']
            if a in board and side_of(rnd, a) != side_of(rnd, x['user_steamid']):
                board[a]['enemies_flashed'] += 1
    n_r = len(rounds)
    for b in board.values():
        b['adr'] = r1(b['dmg'] / n_r); b['hs_pct'] = ri(100 * b['hs'] / b['k']) if b['k'] else 0
        b['kast_pct'] = ri(100 * b['kast_rounds'] / n_r); b['kd'] = round(b['k'] / b['d'], 2) if b['d'] else None
        b['multi'] = {f'{k}k': v for k, v in b['multi'].items()}
    user_team_start = USIDE[rounds[0]['round']]
    ours = sorted([b for s, b in board.items() if s in ours_sid], key=lambda b: -b['k'])
    theirs = sorted([b for s, b in board.items() if s not in ours_sid], key=lambda b: -b['k'])

    rows = []
    for it in timeline:
        ud = it['user_death']; us = it['user_summary']
        rows.append(dict(round=it['round'], side=it['side'], score_before=it['score_before'],
                         situation=it['econ']['situation'], sub=it['econ']['sub'], econ_sit=it['econ']['econ_sit'],
                         enemy_econ=it['econ']['enemy_econ_sit'], result=it['result'], reason_cn=it['reason_cn'],
                         plant=(f"{it['plant']['site']} {it['plant']['t']}s" if it['plant'] else None),
                         user_primary=it['econ']['user']['primary'], user_k=us['kills'], user_d=us['deaths'],
                         user_a=us['assists'], user_dmg=us['dmg'],
                         user_died=bool(ud and not ud['post_round']), death_t=ud['t'] if ud else None,
                         death_place_cn=ud['place_cn'] if ud else None, killer=ud['killer'] if ud else None,
                         killer_place_cn=ud['killer_place_cn'] if ud else None,
                         death_resp=next((f['resp_cn'] for f in it['user_fights'] if f['result'] == '输'), None) if ud else None,
                         death_reason=next((f['reason_cn'] for f in it['user_fights'] if f['result'] == '输'), None) if ud else None))
    final = f'{us_score}:{them_score}'

    ub = board[USER]
    user_name = name_of.get(USER, USER)
    HR = config.halves_th(ctx.th)[0]
    reg = [it for it in timeline if it['round'] <= 2 * HR]
    otr = [it for it in timeline if it['round'] > 2 * HR]
    user_match = dict(name=user_name, k=ub['k'], d=ub['d'], a=ub['a'], adr=ub['adr'], hs_pct=ub['hs_pct'], kast_pct=ub['kast_pct'],
                      opening_k=ub['opening_k'], opening_d=ub['opening_d'], trade_k=ub['trade_k'], traded_d=ub['traded_d'],
                      util_dmg=ub['util_dmg'], enemies_flashed=ub['enemies_flashed'], flash_assist=ub['flash_assist'],
                      multi=ub['multi'],
                      rounds_survived=sum(1 for it in timeline if it['user_summary']['survived']),
                      # by_side 只算常规回合（前 2 × half_rounds 回合）；加时回合另记 overtime，不并进同一边的半场
                      by_side={sd: dict(rounds=sum(1 for it in reg if it['side'] == sd),
                                        won=sum(1 for it in reg if it['side'] == sd and it['result'] == '赢'),
                                        k=sum(it['user_summary']['kills'] for it in reg if it['side'] == sd),
                                        d=sum(it['user_summary']['deaths'] for it in reg if it['side'] == sd),
                                        dmg=sum(it['user_summary']['dmg'] for it in reg if it['side'] == sd))
                               for sd in ('CT', 'T')},
                      **({'overtime': dict(rounds=len(otr), won=sum(1 for it in otr if it['result'] == '赢'),
                                           k=sum(it['user_summary']['kills'] for it in otr),
                                           d=sum(it['user_summary']['deaths'] for it in otr),
                                           dmg=sum(it['user_summary']['dmg'] for it in otr))} if otr else {}),
                      deaths_by_resp=dict(Counter(r['death_resp'] or '未归因' for r in rows if r['user_died'])),
                      deaths_by_reason=dict(Counter(r['death_reason'] or '未归因' for r in rows if r['user_died'])))

    meta = dict(match_no=MATCH_NO, demo=DEMO, map=header.get('map_name'), dem_path=str(DEM), user=user_name, user_steamid=USER,
                user_start_side=user_team_start, final_score=final, rounds=len(timeline), tickrate=TR,
                time_note='t = 冻结结束后第几秒（1 位小数）；坐标为游戏单位取整',
                place_cn=PLACE_CN, reason_cn=REASON_CN, resp_cn=RESP_CN,
                teammates=[b['name'] for b in ours if not b['is_user']], enemies=[b['name'] for b in theirs])

    with open(OUT / 'timeline.json', 'w', encoding='utf-8') as f:
        json.dump(clean(dict(meta=meta, rounds=timeline)), f, ensure_ascii=False, indent=1, allow_nan=False)
    with open(OUT / 'match_overview.json', 'w', encoding='utf-8') as f:
        json.dump(clean(dict(meta={k: meta[k] for k in ('match_no', 'demo', 'map', 'user', 'final_score', 'rounds', 'user_start_side')},
                             rounds=rows, user=user_match, scoreboard=dict(ours=ours, theirs=theirs), data_issues=issues)),
                  f, ensure_ascii=False, indent=1, allow_nan=False, default=nn)
    pd.DataFrame(rows).to_csv(OUT / 'match_overview.csv', index=False, encoding='utf-8-sig')

    print(f'完成：{len(timeline)} 回合，比分 {final}')
    print('用户：', {k: user_match[k] for k in ('k', 'd', 'a', 'adr', 'hs_pct', 'kast_pct')})
    print('数据问题：')
    for s in issues:
        print('  -', s)
    # 窗口进度卡片上直接显示这句；数据提示（issues）只打印在日志里
    return f'{len(timeline)} 回合，比分 {final}，复盘 {user_name}'
