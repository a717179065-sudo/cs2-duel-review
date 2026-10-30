# -*- coding: utf-8 -*-
"""开枪动作层：每次交火的第一枪（移动/停稳、命中、开镜），按武器类、对枪方式汇总，和高分局/职业同口径对标。
由 pro/analysis/match3/shots_match3.py 参数化而来，算法不变。

run(ctx) 输出（work/<id>/）：
  shooting.json     note / moving_threshold / by_class / by_mode / match3_duels（键名沿用原格式："user_m3"、"match3_duels" = 本场）
  shooting_out.txt  打印版
  _shots_all.csv    本场 + 对标所有交火的第一枪明细
缓存：work/<id>/cache/shots_ev.pkl、shots_active.pkl（本场的 weapon_fire / player_hurt、没开枪交火的手持武器）；
  对标 demo 的同类缓存直接读 bench/shots_cache/（只读），缺的才解析（要本地有对标 demo 原文件），存到 work/<id>/cache/bench/。

和原脚本的区别：
  · demo / id / 用户 / 输出目录从 ctx 来；用户本场交火 = work/<key>/base/user_duels.csv；
  · 对标 high / pro 里去掉本场（本场也在对标库里时）和用户本人的行（用户也在别的对标场里时）；
  · "用户其他场"= 静态库里同一个 steamid 的其他 Mirage 场（facts.other_reviews：groups.csv 所有组的 bench/data/duels_<id>.csv
    里有这个 steamid 的，去掉本场，按 demo id 排序；只取用户本人的行），没有就是空。不扫 work/，结果和之前复盘过哪些场无关；
  · 报点中文名用 config.PLACE_CN；
  · 原脚本里人工挑的第 3 场例子（examples）和人工写的结论（conclusion）不再生成，交给规则层（SHOT-*、SUM-01/02）。
"""
import json
import math
import pickle

import numpy as np
import pandas as pd
from demoparser2 import DemoParser

from .. import config
from . import other_reviews, user_duels

OUTPUTS = ['shooting.json']
BENCH_CACHE = config.BENCH_CACHE                  # 对标 demo 的缓存（只读，bench/shots_cache）
TR = config.TICKRATE
MOVE = 110.0

NOTE = """开枪动作核查（第一枪），用户和对标完全同一套算法，全部直接从 demo 算：
  · 交火 = duels_*.csv 的一行（一次击杀/阵亡，双方之一死掉）。用户本场用这一场的底稿 base/user_duels.csv。
    对标 = bench/data/duels_<demo>.csv，groups.csv 里 high（11 场 FACEIT 高分局）和 pro（12 场职业）的所有玩家（不含本场、不含用户本人）。
    用户其他场 = 同一个用户在这里复盘过的其他 Mirage demo（只看用户本人）。
  · 交火窗口 = [min(开打tick, 这一对玩家 3 秒内第一次互相打中的 tick) - 0.5 秒, 击杀 tick]。
  · 第一枪 = 窗口内这名玩家第一发 weapon_fire（排除刀、道具、电击枪）。
  · 速度 = weapon_fire 事件上的 velocity_X/Y 合成的水平速度（游戏单位/秒）。
      移动 = 速度 > 110（静步阈值）；另给"准度速度"口径：速度 <= 该枪最大速度 × 0.34（CS2 移动不掉准的上限，
      AK 约 73、M4 约 76、AWP 约 68、MAC-10 约 82）。
  · AWP/鸟狙另看 is_scoped（开枪那一刻是否开镜）。
  · 第一枪命中 = 第一枪 tick ~ +2 tick 内这名玩家对该交火的敌人有 player_hurt（非道具）。
  · 谁先打中 / 结果 / 对枪方式 直接用 duels csv 的字段（对枪方式按 开打tick 双方速度 > 70 判：架枪 / 拉枪 / 双方都在动 / 双方都没动）。
  · 武器类按第一枪的武器；整场没开枪就死的交火按开打 tick 手里的武器归类，单独计"没开枪"。"""

# ---------------------------------------------------------------- 武器
CLS = {}
for w in ['ak47', 'm4a1', 'm4a1_silencer', 'm4a4', 'galilar', 'famas', 'aug', 'sg556']:
    CLS[w] = '步枪'
for w in ['awp', 'ssg08', 'g3sg1', 'scar20']:
    CLS[w] = '狙'
for w in ['mac10', 'mp9', 'mp7', 'mp5sd', 'ump45', 'p90', 'bizon']:
    CLS[w] = '冲锋枪'
for w in ['glock', 'hkp2000', 'usp_silencer', 'p250', 'fiveseven', 'tec9', 'cz75a', 'deagle', 'elite', 'revolver']:
    CLS[w] = '手枪'
for w in ['nova', 'xm1014', 'mag7', 'sawedoff', 'm249', 'negev']:
    CLS[w] = '霰弹/机枪'
ACTIVE2KEY = {'AK-47': 'ak47', 'M4A1-S': 'm4a1_silencer', 'M4A4': 'm4a1', 'Galil AR': 'galilar', 'FAMAS': 'famas',
              'AUG': 'aug', 'SG 553': 'sg556', 'AWP': 'awp', 'SSG 08': 'ssg08', 'G3SG1': 'g3sg1', 'SCAR-20': 'scar20',
              'MAC-10': 'mac10', 'MP9': 'mp9', 'MP7': 'mp7', 'MP5-SD': 'mp5sd', 'UMP-45': 'ump45', 'P90': 'p90',
              'PP-Bizon': 'bizon', 'Glock-18': 'glock', 'P2000': 'hkp2000', 'USP-S': 'usp_silencer', 'P250': 'p250',
              'Five-SeveN': 'fiveseven', 'Tec-9': 'tec9', 'CZ75-Auto': 'cz75a', 'Desert Eagle': 'deagle',
              'Dual Berettas': 'elite', 'R8 Revolver': 'revolver', 'Nova': 'nova', 'XM1014': 'xm1014', 'MAG-7': 'mag7',
              'Sawed-Off': 'sawedoff', 'M249': 'm249', 'Negev': 'negev'}
MAXSPD = {'ak47': 215, 'm4a1': 225, 'm4a1_silencer': 225, 'galilar': 215, 'famas': 220, 'aug': 220, 'sg556': 210,
          'awp': 200, 'ssg08': 230, 'g3sg1': 215, 'scar20': 215, 'mac10': 240, 'mp9': 240, 'mp7': 220, 'mp5sd': 235,
          'ump45': 230, 'p90': 230, 'bizon': 240, 'glock': 240, 'hkp2000': 240, 'usp_silencer': 240, 'p250': 240,
          'fiveseven': 240, 'tec9': 240, 'cz75a': 240, 'deagle': 230, 'elite': 240, 'revolver': 220, 'nova': 220,
          'xm1014': 215, 'mag7': 225, 'sawedoff': 210, 'm249': 195, 'negev': 150}
WCN = {'ak47': 'AK', 'm4a1': 'M4A4', 'm4a1_silencer': 'M4A1-S', 'galilar': 'Galil', 'famas': 'FAMAS', 'awp': 'AWP',
       'ssg08': '鸟狙', 'mac10': '吹风机(MAC-10)', 'mp9': 'MP9', 'p90': 'P90', 'ump45': 'UMP', 'mp7': 'MP7',
       'glock': 'Glock', 'usp_silencer': 'USP', 'hkp2000': 'P2000', 'p250': 'P250', 'fiveseven': 'FN57',
       'deagle': '沙鹰', 'tec9': 'Tec-9', 'cz75a': 'CZ75', 'elite': '双枪'}
NONGUN_HURT = {'hegrenade', 'inferno', 'molotov', 'incgrenade', 'flashbang', 'smokegrenade', 'decoy', 'world',
               'taser', 'knife', 'bomb', 'planted_c4'}
PLACE_CN = config.PLACE_CN
MODE_CN = {'拉出': '拉枪', '遭遇': '双方都在动', '对峙': '双方都没动'}   # 对标 csv 里是旧名字
CLASSES = ['步枪', '狙', '冲锋枪', '手枪']
GROUPS = ['user_m3', 'user_other', 'high', 'pro']
GNAME = {'user_m3': '你(本场)', 'user_other': '你(其他 Mirage 场次)', 'high': '高分局', 'pro': '职业'}
MODES = ['拉枪', '架枪', '双方都在动', '双方都没动']


def wkey(name):
    if not isinstance(name, str):
        return None
    return name[7:] if name.startswith('weapon_') else name


def cls_of(k):
    return CLS.get(k) if k else None


def pct(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else f'{round(100 * x)}%'


def summ(df):
    n = len(df)
    fd = df[df['fired'] == True].copy()
    for c in ['moving', 'inaccurate', 'first_shot_hit']:
        fd[c] = fd[c].astype(bool)
    out = dict(duels=int(n), fired=int(len(fd)), no_shot=int(n - len(fd)),
               win_rate=(df['result'] == '赢').mean() if n else None,
               first_shot_moving_rate=fd['moving'].mean() if len(fd) else None,
               first_shot_inaccurate_rate=fd['inaccurate'].mean() if len(fd) else None,
               first_shot_hit_rate=fd['first_shot_hit'].mean() if len(fd) else None,
               i_hit_first_rate=(df['first_hit_by'] == '我').mean() if n else None,
               median_speed=float(fd['speed'].median()) if len(fd) else None)
    if len(fd):
        mv, st = fd[fd['moving']], fd[~fd['moving']]
        out['win_rate_moving'] = (mv['result'] == '赢').mean() if len(mv) else None
        out['win_rate_still'] = (st['result'] == '赢').mean() if len(st) else None
        out['n_moving'], out['n_still'] = int(len(mv)), int(len(st))
        out['first_shot_hit_rate_moving'] = mv['first_shot_hit'].mean() if len(mv) else None
        out['first_shot_hit_rate_still'] = st['first_shot_hit'].mean() if len(st) else None
        out['run_rate_over180'] = (fd['speed'] > 180).mean()
        sth = fd['shots_to_first_hit'].dropna()
        out['median_shots_to_first_hit'] = float(sth.median()) if len(sth) else None
        out['never_hit_rate'] = fd['shots_to_first_hit'].isna().mean()
        sn = fd[fd['wclass'] == '狙']
        if len(sn):
            out['sniper_scoped_rate'] = sn['scoped'].astype(bool).mean()
    return out


def fmt(s):
    keys = ['first_shot_moving_rate', 'first_shot_inaccurate_rate', 'first_shot_hit_rate', 'i_hit_first_rate',
            'win_rate', 'win_rate_moving', 'win_rate_still', 'sniper_scoped_rate', 'first_shot_hit_rate_moving',
            'first_shot_hit_rate_still', 'run_rate_over180', 'never_hit_rate']
    o = dict(s)
    for k in keys:
        if k in o:
            o[k] = pct(o[k])
    return o


def user_demo_file(demo):
    """report/cache/1-<id>-*.dem；没有返回 None。"""
    c = sorted(config.REPORT.glob(f'cache/1-{demo}*.dem'))
    return c[0] if c else None


def run(ctx):
    """写 shooting.json / shooting_out.txt / _shots_all.csv。依赖 timeline.json（用户名）。"""
    USER = ctx.user
    DEMO = ctx.id
    OUT = ctx.work
    CACHE = ctx.work / 'cache'
    BENCH_OUT = CACHE / 'bench'
    CACHE.mkdir(parents=True, exist_ok=True)
    user_name = ctx.json('timeline.json')['meta']['user']
    OTHERS = {d: f for d, f in other_reviews(ctx)}      # 用户其他场：demo id → 静态交火表 bench/data/duels_<id>.csv
    LOG = []

    def P(*a):
        s = ' '.join(str(x) for x in a)
        print(s, flush=True)
        LOG.append(s)

    def dem_path(demo):
        if demo == DEMO:
            return ctx.dem
        c = user_demo_file(demo)
        if c:
            return c
        c = sorted((config.PRO / 'cache').glob(f'1-{demo}*.dem'))
        if c:
            return c[0]
        c = config.PRO / 'hltv' / f'{demo}.dem'
        return c if c.exists() else None

    def cache_file(demo, suffix=''):
        """本场 → work/<id>/cache/shots_ev.pkl / shots_active.pkl；对标 → 先找只读缓存，再找 work 里补的。"""
        if demo == DEMO:
            return CACHE / ('shots_active.pkl' if suffix else 'shots_ev.pkl'), True
        ro = BENCH_CACHE / f'{demo}{suffix}.pkl'
        if ro.exists():
            return ro, False
        return BENCH_OUT / f'{demo}{suffix}.pkl', True

    def bench_ro(demo, suffix=''):
        """仓库自带的只读对标缓存（bench/shots_cache，不用 pickle）：
        <demo>.fire.parquet / <demo>.hurt.parquet，<demo>_active.json（[[tick, steamid, 武器或 null, 开镜], ...]，null = 不知道）。
        没有就返回 None，退回 pickle 缓存 / 解析 demo。"""
        if demo == DEMO:
            return None
        if suffix:
            p = BENCH_CACHE / f'{demo}_active.json'
            if not p.exists():
                return None
            rows = json.loads(p.read_text(encoding='utf-8'))
            return {(r[0], r[1]): (r[2] if r[2] is not None else float('nan'), r[3]) for r in rows}
        f, h = BENCH_CACHE / f'{demo}.fire.parquet', BENCH_CACHE / f'{demo}.hurt.parquet'
        if not (f.exists() and h.exists()):
            return None
        return dict(fire=pd.read_parquet(f), hurt=pd.read_parquet(h), p=None)

    def load_events(demo):
        ro = bench_ro(demo)
        if ro is not None:
            return ro
        cp, writable = cache_file(demo)
        if cp.exists() and demo != DEMO:          # 本场每次都重新解析（2 秒），对标读缓存
            return pickle.load(open(cp, 'rb'))
        dp = dem_path(demo)
        if dp is None:
            return None
        P(f'  解析 {demo} ...')
        p = DemoParser(str(dp))
        f = p.parse_event('weapon_fire', player=['velocity_X', 'velocity_Y', 'is_scoped', 'is_walking', 'ducking',
                                                 'X', 'Y', 'last_place_name'])
        f = f[['tick', 'user_steamid', 'weapon', 'user_velocity_X', 'user_velocity_Y', 'user_is_scoped',
               'user_is_walking', 'user_ducking', 'user_last_place_name']].copy()
        f['user_steamid'] = f['user_steamid'].astype(str)
        f['wk'] = f['weapon'].map(wkey)
        f = f[f['wk'].map(lambda k: k in CLS)]
        f['spd'] = np.hypot(f['user_velocity_X'].astype(float), f['user_velocity_Y'].astype(float))
        h = p.parse_event('player_hurt')[['tick', 'attacker_steamid', 'user_steamid', 'dmg_health', 'weapon']].copy()
        h['attacker_steamid'] = h['attacker_steamid'].astype(str)
        h['user_steamid'] = h['user_steamid'].astype(str)
        obj = dict(fire=f.sort_values('tick').reset_index(drop=True), hurt=h.sort_values('tick').reset_index(drop=True), p=None)
        obj['dem'] = str(dp)
        cp.parent.mkdir(parents=True, exist_ok=True)
        pickle.dump(obj, open(cp, 'wb'))
        return obj

    def active_at(demo, pairs):
        """开打 tick 手里的武器（只给没开枪的交火用）。pairs: [(tick, steamid)]"""
        if not pairs:
            return {}
        cp, writable = cache_file(demo, '_active')
        ro = bench_ro(demo, '_active')
        if ro is not None:
            cache, cp, writable = ro, BENCH_OUT / f'{demo}_active.pkl', False
        else:
            cache = pickle.load(open(cp, 'rb')) if cp.exists() and demo != DEMO else {}
        need = [x for x in pairs if x not in cache]
        if need:
            dp = dem_path(demo)
            if dp is None:                         # 缓存缺条目、又没有 demo 原文件：缺的几条按"不知道手里武器"处理
                return {x: cache.get(x, (None, None)) for x in pairs}
            p = DemoParser(str(dp))
            t = p.parse_ticks(['active_weapon_name', 'is_scoped'], ticks=sorted({int(a) for a, _ in need}))
            t['steamid'] = t['steamid'].astype(str)
            idx = {(int(r.tick), r.steamid): (r.active_weapon_name, bool(r.is_scoped)) for r in t.itertuples()}
            for x in need:
                cache[x] = idx.get((int(x[0]), x[1]), (None, None))
            if not writable:                       # 只读缓存缺了几条：补的写到 work 里
                cp = BENCH_OUT / cp.name
            cp.parent.mkdir(parents=True, exist_ok=True)
            pickle.dump(cache, open(cp, 'wb'))
        return {x: cache[x] for x in pairs}

    def analyse(demo, du, group):
        ev = load_events(demo)
        if ev is None:
            P(f'  !! 找不到 {demo} 的 demo 文件，跳过')
            return []
        F, H = ev['fire'], ev['hurt']
        Hg = H[~H['weapon'].map(lambda w: (wkey(w) or '').startswith('knife') or wkey(w) in NONGUN_HURT)]
        fire_by = {s: g for s, g in F.groupby('user_steamid')}
        rows, noshot = [], []
        for d in du.itertuples(index=False):
            sid, eid = str(d.steamid), str(d.敌人ID)
            kt, et = int(d.tick), int(d.开打tick)
            pair = Hg[(Hg['tick'] >= et - 3 * TR) & (Hg['tick'] <= kt)
                      & (((Hg['attacker_steamid'] == sid) & (Hg['user_steamid'] == eid))
                         | ((Hg['attacker_steamid'] == eid) & (Hg['user_steamid'] == sid)))]
            lo = min(et, int(pair['tick'].min()) if len(pair) else et) - TR // 2
            g = fire_by.get(sid)
            w = g[(g['tick'] >= lo) & (g['tick'] <= kt)] if g is not None else pd.DataFrame()
            rec = dict(group=group, demo=demo, steamid=sid, player=d.player, round=int(d.回合), side=d.阵营,
                       result=d.结果, kill_t=float(d.开局第几秒), kill_tick=kt, engage_tick=et, enemy=d.敌人, enemy_id=eid,
                       first_hit_by=d.谁先打中, mode=MODE_CN.get(d.对枪方式, d.对枪方式), situation=d.局势, my_place=d.我的报点,
                       enemy_place=d.敌人报点, engage_speed=(None if pd.isna(d.我速度) else float(d.我速度)),
                       enemy_speed=(None if pd.isna(d.敌速度) else float(d.敌速度)), alive_us=d.我方存活, alive_them=d.敌方存活)
            if len(w) == 0:
                rec.update(fired=False)
                noshot.append(rec)
                rows.append(rec)
                continue
            f0 = w.iloc[0]
            ft = int(f0['tick'])
            k = f0['wk']
            spd = float(f0['spd'])
            hit = Hg[(Hg['attacker_steamid'] == sid) & (Hg['user_steamid'] == eid) & (Hg['tick'] >= ft) & (Hg['tick'] <= ft + 2)]
            my_hits = Hg[(Hg['attacker_steamid'] == sid) & (Hg['user_steamid'] == eid) & (Hg['tick'] >= lo) & (Hg['tick'] <= kt)]
            shots_to_hit = None
            if len(my_hits):
                fh_t = int(my_hits['tick'].min())
                shots_to_hit = int(((w['tick'] >= ft) & (w['tick'] <= fh_t)).sum())
            rec.update(fired=True, fire_tick=ft, fire_t=round(float(d.开局第几秒) - (kt - ft) / TR, 1), weapon=k,
                       wclass=cls_of(k), speed=round(spd), moving=bool(spd > MOVE),
                       inaccurate=bool(spd > 0.34 * MAXSPD.get(k, 240)),
                       scoped=(bool(f0['user_is_scoped']) if cls_of(k) == '狙' else None),
                       walking=bool(f0['user_is_walking']), ducking=bool(f0['user_ducking']),
                       first_shot_hit=bool(len(hit)), shots_in_fight=int(len(w)), shots_to_first_hit=shots_to_hit,
                       fire_place=f0['user_last_place_name'])
            rows.append(rec)
        # 没开枪的交火：按开打 tick 手里的武器归类
        act = active_at(demo, [(r['engage_tick'], r['steamid']) for r in noshot])
        for r in noshot:
            a, sc = act.get((r['engage_tick'], r['steamid']), (None, None))
            k = ACTIVE2KEY.get(a)
            r.update(weapon=k, wclass=cls_of(k) or '其他(刀/道具)', active_name=a)
        return rows

    # ---------------------------------------------------------------- 读交火
    G = pd.read_csv(config.DATA / 'groups.csv')
    U = user_duels(ctx, dtype={'敌人ID': str})
    U['steamid'], U['player'], U['demo'] = USER, user_name, DEMO

    P(f'== 用户本场（{DEMO}）')
    rows = analyse(DEMO, U, 'user_m3')
    P('== 对标 high / pro（不含本场、不含用户本人）')
    for _, g in G[G['group'].isin(['high', 'pro'])].iterrows():
        if str(g.demo) == DEMO:
            P(f'  跳过 {g.demo}（就是本场）')
            continue
        du = pd.read_csv(config.DATA / f'duels_{g.demo}.csv', dtype={'steamid': str, '敌人ID': str})
        du = du[du['steamid'] != USER]
        rows += analyse(g.demo, du, g.group)
    others = sorted(OTHERS)
    P(f'== 用户其他 Mirage 场次（参考，只看用户本人）：{others}')
    for dm in others:
        du = pd.read_csv(OTHERS[dm], dtype={'steamid': str, '敌人ID': str})
        du = du[du['steamid'] == str(USER)].copy()
        du['player'], du['demo'] = user_name, dm
        rows += analyse(dm, du, 'user_other')

    A = pd.DataFrame(rows)
    A.to_csv(OUT / '_shots_all.csv', index=False, encoding='utf-8-sig')
    groups_used = [g for g in GROUPS if g != 'user_other' or others]     # 没复盘过别的场：不写 user_other

    P('\n== 按武器类（第一枪武器；没开枪按手里武器）')
    by_class = {}
    for c in CLASSES + ['霰弹/机枪', '其他(刀/道具)']:
        by_class[c] = {}
        for gname in groups_used:
            x = A[(A['group'] == gname) & (A['wclass'] == c)]
            s = summ(x)
            by_class[c][gname] = s
            if gname == 'user_m3' or len(x):
                P(f'  {c:6s} {GNAME[gname]:14s}', json.dumps(fmt(s), ensure_ascii=False))

    P('\n== 按对枪方式 × 武器类')
    by_mode = {}
    for m in MODES:
        by_mode[m] = {}
        for gname in groups_used:
            x = A[(A['group'] == gname) & (A['mode'] == m)]
            by_mode[m][gname] = dict(all=summ(x), **{c: summ(x[x['wclass'] == c]) for c in ['步枪', '狙']})
            P(f'  {m:6s} {GNAME[gname]:14s} 全部', json.dumps(fmt(by_mode[m][gname]['all']), ensure_ascii=False))
            for c in ['步枪', '狙']:
                if by_mode[m][gname][c]['duels']:
                    P(f'  {"":6s} {"":14s} {c}', json.dumps(fmt(by_mode[m][gname][c]), ensure_ascii=False))

    # 用户本场逐条
    M3 = A[A['group'] == 'user_m3'].sort_values(['round', 'kill_tick'])
    P('\n== 本场逐条')
    detail = []
    for r in M3.itertuples():
        d = dict(round=r.round, side=r.side, result=r.result, enemy=r.enemy, mode=r.mode, situation=r.situation,
                 my_place=PLACE_CN.get(r.my_place, r.my_place), enemy_place=PLACE_CN.get(r.enemy_place, r.enemy_place),
                 first_hit_by=r.first_hit_by, kill_t=r.kill_t, kill_tick=r.kill_tick, engage_speed=r.engage_speed,
                 wclass=r.wclass, weapon=WCN.get(r.weapon, r.weapon), fired=bool(r.fired))
        if r.fired:
            d.update(fire_t=r.fire_t, fire_tick=int(r.fire_tick), speed=int(r.speed), moving=bool(r.moving),
                     inaccurate=bool(r.inaccurate), scoped=(None if pd.isna(r.scoped) else bool(r.scoped)),
                     first_shot_hit=bool(r.first_shot_hit), shots_in_fight=int(r.shots_in_fight),
                     shots_to_first_hit=(None if pd.isna(r.shots_to_first_hit) else int(r.shots_to_first_hit)),
                     fire_place=PLACE_CN.get(r.fire_place, r.fire_place))
        detail.append(d)
        if r.fired:
            P(f'  R{r.round:>2} {r.fire_t:>5}s {d["weapon"]:8s} 速度{int(r.speed):>4} {"移动" if r.moving else "停稳"}'
              f'{" 开镜" if r.scoped is True else (" 未开镜" if r.scoped is False else "")} 首枪{"中" if r.first_shot_hit else "空"}'
              f' 先中={r.first_hit_by} {r.mode} {r.result} vs {r.enemy} @{d["my_place"]} tick {int(r.fire_tick)}')
        else:
            P(f'  R{r.round:>2} {r.kill_t:>5}s {str(d["weapon"]):8s} 没开枪 {r.mode} {r.result} vs {r.enemy} @{d["my_place"]} tick {r.kill_tick}')

    with open(OUT / 'shooting.json', 'w', encoding='utf-8') as f:
        json.dump(dict(
            note=NOTE,
            moving_threshold=MOVE,
            by_class={c: {g: fmt(v) for g, v in d.items()} for c, d in by_class.items()},
            by_mode={m: {g: {k: fmt(v) for k, v in d.items()} for g, d in dd.items()} for m, dd in by_mode.items()},
            match3_duels=detail,
        ), f, ensure_ascii=False, indent=1, default=lambda o: o.item() if hasattr(o, 'item') else str(o))
    (OUT / 'shooting_out.txt').write_text('\n'.join(LOG), encoding='utf-8')
    P('\n写出', OUT / 'shooting.json')
    return (f'本场 {len(detail)} 次交火（{sum(1 for d in detail if d["fired"])} 次开了枪）'
            + (f'，参考库里你的其他 {len(others)} 场' if others else ''))
