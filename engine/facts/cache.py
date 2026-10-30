# -*- coding: utf-8 -*-
"""准备帧数据：把规则原型要用的逐帧数据和事件从 demo 里解析出来存到 work/<id>/cache/。

每个缓存的取样、字段、事件、存法和原型脚本完全一样（只把写死的 demo 路径/输出路径换成 ctx），
所以 pickle/csv 内容和 rules_proto/_cache 里的原版相同。文件名和原型对应关系见 engine/ctx.py 开头。

  ticks.pkl          ← rules_proto/_parse_ticks.py               每一帧全员位置/朝向/可见性/速度 + 事件
  sight.pkl          ← rules_proto/_sight_cache.py               每 4 tick，回合内（到结束后 3 秒）
  sight_s4.pkl       ← rules_proto/站位与视线.py parse_demo(step=4)
  daoju.pkl          ← rules_proto/_cache_build.py               每 8 tick，含 inventory / flash_duration
  pp_ticks.pkl       ← rules_proto/_parse_下包后_回防_保枪.py      每 8 tick，到结束后 5 秒，含 has_defuser
  ticks8.csv、ev_*.csv ← rules_proto/_cache/parse_cache.py       每 8 tick，到结束后 3 秒（好的操作、时机与转点）
  grid.parquet、team_ev.pkl ← rules_proto/团队配合.py _parse    每 8 tick，按 base/rounds.csv 的起止

依赖：timeline.json（key_ticks）和 work/<key>/base/rounds.csv（这一场的回合表，base 阶段生成）。
"""
import pickle
import time

import numpy as np
import pandas as pd
from demoparser2 import DemoParser

from . import rounds_csv

TR = 64


# ---------------------------------------------------------------- _parse_ticks.py
def build_ticks(p, out, tl, ctx):
    F = ["X", "Y", "Z", "pitch", "yaw", "team_num", "is_alive", "duck_amount", "health", "active_weapon_name",
         "approximate_spotted_by", "last_place_name", "velocity_X", "velocity_Y", "is_scoped", "total_rounds_played",
         "flash_duration"]
    t = p.parse_ticks(F)
    t['steamid'] = t['steamid'].astype(str)
    ev = {}
    ev['fire'] = p.parse_event('weapon_fire', player=['X', 'Y', 'Z', 'pitch', 'yaw', 'velocity_X', 'velocity_Y', 'is_scoped', 'last_place_name', 'team_num'])
    ev['hurt'] = p.parse_event('player_hurt', player=['X', 'Y', 'Z', 'last_place_name'])
    ev['death'] = p.parse_event('player_death', player=['X', 'Y', 'Z', 'last_place_name', 'team_num'], other=['total_rounds_played'])
    ev['freeze_end'] = p.parse_event('round_freeze_end', other=['total_rounds_played'])
    ev['round_end'] = p.parse_event('round_end', other=['total_rounds_played'])
    try:
        ev['begin'] = p.parse_event('begin_new_match')
    except Exception:
        ev['begin'] = pd.DataFrame(columns=['tick'])
    ev['blind'] = p.parse_event('player_blind')
    pickle.dump(dict(ticks=t, **ev), open(out / 'ticks.pkl', 'wb'))


# ---------------------------------------------------------------- _sight_cache.py
def build_sight(p, out, tl, ctx):
    STEP = 4
    F = ['X', 'Y', 'Z', 'yaw', 'pitch', 'is_alive', 'health', 'team_num', 'duck_amount', 'active_weapon_name',
         'approximate_spotted_by', 'is_scoped', 'velocity_X', 'velocity_Y', 'last_place_name']
    ticks = []
    for r in tl['rounds']:
        k = r['key_ticks']
        ticks += list(range(k['round_start'], k['round_end'] + 3 * 64, STEP))
    df = p.parse_ticks(F, ticks=sorted(set(ticks)))
    df['steamid'] = df['steamid'].astype(str)
    df = df[df['team_num'].isin([2, 3])].copy()
    df['spotted_by'] = df['approximate_spotted_by'].apply(
        lambda v: tuple(str(x) for x in v) if v is not None and not isinstance(v, str) else ())
    df = df.drop(columns=['approximate_spotted_by'])
    ev = {}
    for name, kw in [('weapon_fire', dict(player=['X', 'Y', 'Z', 'team_num'])),
                     ('player_hurt', dict(player=['X', 'Y', 'Z', 'team_num'])),
                     ('player_death', dict(player=['X', 'Y', 'Z', 'team_num'])),
                     ('smokegrenade_detonate', {}), ('smokegrenade_expired', {}), ('flashbang_detonate', {}),
                     ('hegrenade_detonate', {}), ('inferno_startburn', {}), ('inferno_expire', {}),
                     ('bomb_planted', {}), ('player_blind', {})]:
        try:
            e = p.parse_event(name, **kw)
            if not isinstance(e, pd.DataFrame):
                e = pd.DataFrame()
            for c in ('user_steamid', 'attacker_steamid'):
                if c in e:
                    e[c] = e[c].astype(str)
            ev[name] = e
        except Exception as ex:
            print('skip', name, ex)
            ev[name] = pd.DataFrame()
    pickle.dump({'ticks': df, 'ev': ev, 'step': STEP}, open(out / 'sight.pkl', 'wb'))


# ---------------------------------------------------------------- 站位与视线.py parse_demo(dem, key, 4)
def build_sight_s4(p, out, tl, ctx, step=4):
    FIELDS = ['X', 'Y', 'Z', 'yaw', 'pitch', 'is_alive', 'health', 'team_num', 'duck_amount', 'active_weapon_name',
              'approximate_spotted_by', 'is_scoped', 'last_place_name']
    EVENTS = [('weapon_fire', dict(player=['X', 'Y', 'Z'])), ('player_hurt', dict(player=['X', 'Y', 'Z'])),
              ('player_death', dict(player=['X', 'Y', 'Z'])), ('smokegrenade_detonate', {}),
              ('smokegrenade_expired', {}), ('flashbang_detonate', {}), ('inferno_startburn', {}),
              ('inferno_expire', {}), ('bomb_planted', {}), ('round_freeze_end', {}), ('round_end', {}),
              ('begin_new_match', {})]
    ev = {}
    for name, kw in EVENTS:
        try:
            e = p.parse_event(name, **kw)
        except Exception:
            e = None
        if not isinstance(e, pd.DataFrame):
            e = pd.DataFrame(columns=['tick'])
        for c in ('user_steamid', 'attacker_steamid'):
            if c in e:
                e[c] = e[c].astype(str)
        ev[name] = e
    ms = int(ev['begin_new_match']['tick'].max()) if len(ev['begin_new_match']) else 0
    fe = sorted(int(t) for t in ev['round_freeze_end']['tick'] if t > ms)
    re_ = ev['round_end']
    if 'winner' in re_:
        re_ = re_[re_['winner'].notna()]
    rend = sorted(int(t) for t in re_['tick'] if t > ms)
    rounds = []
    for i, s in enumerate(fe):
        nxt = fe[i + 1] if i + 1 < len(fe) else 10 ** 9
        ends = [t for t in rend if s < t <= nxt]
        if not ends:
            continue
        rounds.append(dict(n=len(rounds) + 1, start=s, end=ends[0]))
    ticks = []
    for r in rounds:
        ticks += list(range(r['start'], r['end'] + 3 * TR, step))
    df = p.parse_ticks(FIELDS, ticks=sorted(set(ticks)))
    df['steamid'] = df['steamid'].astype(str)
    df = df[df['team_num'].isin([2, 3])].copy()
    df['spotted_by'] = df['approximate_spotted_by'].apply(
        lambda v: tuple(str(x) for x in v) if v is not None and not isinstance(v, str) else ())
    df = df.drop(columns=['approximate_spotted_by'])
    d = dict(ticks=df, ev=ev, rounds=rounds, step=step, match_start=ms)
    pickle.dump(d, open(out / f'sight_s{step}.pkl', 'wb'))


# ---------------------------------------------------------------- _cache_build.py
def build_daoju(p, out, tl, ctx):
    STEP = 8
    ticks = []
    for r in tl["rounds"]:
        k = r["key_ticks"]
        ticks += list(range(k["round_start"], k["round_end"] + 1, STEP))
    df = p.parse_ticks(["X", "Y", "Z", "yaw", "pitch", "is_alive", "health", "team_num", "inventory",
                        "approximate_spotted_by", "last_place_name", "active_weapon_name", "velocity_X", "velocity_Y", "flash_duration"], ticks=ticks)
    df["steamid"] = df["steamid"].astype(str)
    df = df[df["team_num"].isin([2, 3])]
    evs = {}
    for name in ["player_blind", "player_hurt", "player_death", "weapon_fire", "flashbang_detonate", "hegrenade_detonate",
                 "smokegrenade_detonate", "inferno_startburn", "inferno_expire", "smokegrenade_expired", "decoy_started"]:
        try:
            e = p.parse_event(name, player=["X", "Y", "Z", "team_num"], other=["total_rounds_played"])
        except Exception as ex:
            print("ev fail", name, ex); e = pd.DataFrame()
        if not isinstance(e, pd.DataFrame):
            e = pd.DataFrame()
        else:
            for c in ("user_steamid", "attacker_steamid"):
                if c in e:
                    e[c] = e[c].astype(str)
        evs[name] = e
    pickle.dump({"ticks": df, "ev": evs}, open(out / "daoju.pkl", "wb"))


# ---------------------------------------------------------------- _parse_下包后_回防_保枪.py
def build_pp(p, out, tl, ctx):
    STEP = 8
    FIELDS = ['X', 'Y', 'Z', 'yaw', 'pitch', 'velocity_X', 'velocity_Y', 'is_alive', 'health', 'team_num',
              'active_weapon_name', 'approximate_spotted_by', 'inventory', 'has_defuser', 'is_scoped', 'last_place_name']
    ticks = []
    for r in tl['rounds']:
        k = r['key_ticks']
        ticks += list(range(k['round_start'], k['round_end'] + 5 * 64, STEP))
    df = p.parse_ticks(FIELDS, ticks=sorted(set(ticks)))
    df['steamid'] = df['steamid'].astype(str)
    df['spotted_by'] = df['approximate_spotted_by'].apply(lambda v: tuple(str(x) for x in v) if v is not None and not isinstance(v, str) else ())
    df = df.drop(columns=['approximate_spotted_by'])
    ev = {}
    for name, kw in [('weapon_fire', dict(player=['X', 'Y', 'Z', 'velocity_X', 'velocity_Y', 'is_scoped'])),
                     ('player_hurt', dict(player=['X', 'Y', 'Z'])),
                     ('player_death', dict(player=['X', 'Y', 'Z'])),
                     ('bomb_planted', {}), ('bomb_begindefuse', {}), ('bomb_abortdefuse', {}), ('bomb_defused', {}),
                     ('bomb_exploded', {}), ('player_blind', {}), ('item_pickup', {})]:
        try:
            ev[name] = p.parse_event(name, **kw)
        except Exception as e:
            print('skip', name, e)
    pickle.dump({'ticks': df, 'ev': ev}, open(out / 'pp_ticks.pkl', 'wb'))


# ---------------------------------------------------------------- _cache/parse_cache.py（csv）
EV8 = [('player_death', dict(player=['X', 'Y', 'Z'])), ('player_hurt', dict(player=['X', 'Y', 'Z'])),
       ('weapon_fire', dict(player=['X', 'Y', 'Z', 'yaw', 'pitch', 'velocity_X', 'velocity_Y', 'is_scoped'])),
       ('item_pickup', {}), ('bomb_begindefuse', {}), ('bomb_planted', {}), ('bomb_defused', {}),
       ('hegrenade_detonate', {}), ('inferno_startburn', {}), ('smokegrenade_detonate', {}), ('flashbang_detonate', {}),
       ('player_blind', {})]


def build_ticks8(p, out, tl, ctx):
    ticks = []
    for r in tl['rounds']:
        kt = r['key_ticks']; lo = kt['round_start']; hi = kt['round_end'] + 64 * 3
        ticks += list(range(lo, hi, 8))
    F = ["X", "Y", "Z", "yaw", "pitch", "velocity_X", "velocity_Y", "velocity_Z", "is_alive", "health", "team_num",
         "approximate_spotted_by", "active_weapon_name", "is_scoped", "ducking", "last_place_name"]
    df = p.parse_ticks(F, ticks=sorted(set(ticks)))
    df['spotted_by'] = df['approximate_spotted_by'].apply(lambda v: '|'.join(str(x) for x in v) if isinstance(v, (list, np.ndarray)) else '')
    df = df.drop(columns=['approximate_spotted_by'])
    df['steamid'] = df['steamid'].astype(str)
    df.to_csv(out / 'ticks8.csv', index=False)
    for e, kw in EV8:
        try:
            d = p.parse_event(e, **kw)
            d.to_csv(out / f'ev_{e}.csv', index=False)
        except Exception as ex:
            print('FAIL', e, ex)


# ---------------------------------------------------------------- 团队配合.py _parse(demo, group)
def build_grid(p, out, tl, ctx):
    STEP = 8
    R = pd.read_csv(rounds_csv(ctx))
    ticks = []
    for r in R.itertuples():
        ticks += list(range(int(r.start_tick), int(r.end_tick) + 1, STEP))
    df = p.parse_ticks(["X", "Y", "Z", "is_alive", "health", "team_num", "approximate_spotted_by", "active_weapon_name"], ticks=ticks)
    df["steamid"] = df["steamid"].astype(str)
    df["spotted_by"] = df["approximate_spotted_by"].apply(
        lambda v: "|".join(str(x) for x in v) if v is not None and not isinstance(v, str) else "")
    df = df.drop(columns=["approximate_spotted_by"])
    df.to_parquet(out / 'grid.parquet')

    def e(name, **kw):
        x = p.parse_event(name, **kw)
        x = x if isinstance(x, pd.DataFrame) else pd.DataFrame()
        for c in ("user_steamid", "attacker_steamid"):
            if c in x:
                x[c] = x[c].astype(str)
        return x
    events = {"fire": e("weapon_fire", player=["X", "Y", "Z"]), "hurt": e("player_hurt"), "blind": e("player_blind"),
              "pick": e("item_pickup", player=["X", "Y", "Z"])}
    pd.to_pickle(events, out / 'team_ev.pkl')


# 名字 → (生成函数, 产出文件)
BUILDERS = [
    ('ticks', build_ticks, ['ticks.pkl']),
    ('sight', build_sight, ['sight.pkl']),
    ('sight_s4', build_sight_s4, ['sight_s4.pkl']),
    ('daoju', build_daoju, ['daoju.pkl']),
    ('pp_ticks', build_pp, ['pp_ticks.pkl']),
    ('ticks8', build_ticks8, ['ticks8.csv'] + [f'ev_{e}.csv' for e, _ in EV8]),
    ('grid', build_grid, ['grid.parquet', 'team_ev.pkl']),
]


def run(ctx, only=None):
    """生成 work/<id>/cache/ 下的全部缓存（only=名字列表时只生成这些）。调用了就重算（跳过与否由 pipeline 决定）。"""
    out = ctx.work / 'cache'
    out.mkdir(parents=True, exist_ok=True)
    tl = ctx.json('timeline.json')
    p = None
    times = {}
    for name, fn, files in BUILDERS:
        if only and name not in only:
            continue
        p = p or DemoParser(str(ctx.dem))
        t0 = time.time()
        fn(p, out, tl, ctx)
        times[name] = round(time.time() - t0, 1)
        print(f'  缓存 {name}: {times[name]}s', flush=True)
        ctx.forget(name)
    # 每项用时已经打印在日志里；窗口进度卡片上只写一句玩家看得懂的
    return f'帧数据准备好了（{len(times)} 项）' if times else '帧数据不用重新准备'
