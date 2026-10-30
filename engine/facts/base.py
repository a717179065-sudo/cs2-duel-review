# -*- coding: utf-8 -*-
"""底稿：只用这一个 demo，算出原来要靠批量脚本事先入库的几份表（新 demo 不用再"先入库"）。

（下面写的 pro/data、pro/analysis 是原型阶段批量入库的目录，只说明格式来源；仓库里的对标数据在 bench/。）

run(ctx) 输出（work/<id>/base/，utf-8-sig）：
  rounds.csv     = pro/data/rounds_<id>.csv      （extract_pro.py）
  positions.csv  = pro/data/positions_<id>.csv   （extract_pro.py）
  kills.csv      = pro/data/kills_<id>.csv       （extract_pro.py）
  util.csv       = pro/data/util_<id>.csv        （extract_pro.py）
  duels.csv      = pro/data/duels_<id>.csv       （extract_pro.py：每个玩家的交火 + 局势/原因，build_report.add_context）
  sit_econ.csv   = pro/analysis/sit_cache/econ_<id>.csv（sit_parse.py：每回合开局/买完两帧每人的钱和背包）
  sit_hurt.csv   = pro/analysis/sit_cache/hurt_<id>.csv（sit_parse.py：伤害事件）
  sit.csv        = pro/analysis/sit_rounds.csv 里这个 demo 的行（sit_rounds.py 的每回合每方部分，不含跨场统计）
  user_duels.csv = report/duels_de_mirage_<用户名>.csv 里这一场的行（build_report.py 单场、单用户）

代码从 extract_pro.py、duels.py、build_report.py、sit_parse.py、sit_rounds.py、pro_prefire.py 搬过来，算法不变，只是：
  · demo 路径 / id / 用户来自 ctx，不需要 --group；
  · duels.csv 的 group 列、sit.csv 的 group 列：groups.csv 里有这个 demo 就用它的组，否则记 user；
  · sit.csv 的 scene 列、user_duels.csv 的场次列：固定 1（单场复盘，原表的值来自多场报告里的排序）；
  · sit.csv 的 user_team / user_* 列：按 ctx.user 算（原表只给 groups.csv 里 user 组、BiuBiuZ 算）；
  · user_duels.csv 的标准位 / 凶手位置：原来查 prefire_ref.json 的 user_deaths_all（只有 BiuBiuZ 那几场），
    这里用同一份文件里职业 + 高分局的开枪位聚类（contexts.*.clusters，静态对标数据）现算这个用户的每次阵亡，
    判定方法和 pro_prefire.py 一样（凶手离该局势·区域前 5 个簇任一中心 ≤150 单位）。
  · duels.csv 的 原因 列按现在的 build_report.full_reason（hidden 已并进 slow）；静态库 bench/data/duels_*.csv 是更早的版本
    写的，还留着 'hidden'。以后要拿本场和对标库比 原因 时，库里的 'hidden' 要当成 'slow'。
对标数据（bench/ 里其他比赛的 data/*、groups.csv、prefire_ref.json 的聚类）只读，不改。
"""
import json
import math
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from demoparser2 import DemoParser

from .. import config
from . import FactsError

OUTPUTS = ['rounds.csv', 'positions.csv', 'kills.csv', 'util.csv', 'duels.csv', 'sit_econ.csv', 'sit_hurt.csv',
           'sit.csv', 'user_duels.csv']
ENC = 'utf-8-sig'


def base_dir(ctx):
    return Path(ctx.work) / 'base'


def path(ctx, name):
    """work/<id>/base/<name>（name 可以不带 .csv）"""
    if not name.endswith('.csv'):
        name += '.csv'
    return base_dir(ctx) / name


# =============================================================================== duels.py（原样）
TICKRATE = 64
EYE_HEIGHT = 64.0          # 站立时眼睛离脚底的高度（游戏单位）
SPOT_LOOKBACK_S = 6.0      # 击杀前最多往回找多少秒去确定"第一次看见"
SPOT_GAP_TICKS = 16        # 视线短暂中断（≤0.25 秒）仍算同一次看见
TEAM_NAME = {2: "T", 3: "CT"}
CROUCH_DROP = 18.0         # 完全蹲下时眼睛降低的高度（64 → 46）
TICK_FIELDS = ["X", "Y", "Z", "pitch", "yaw", "team_num", "is_alive", "duck_amount", "mvps", "health", "current_equip_value",
               "active_weapon_name", "approximate_spotted_by", "last_place_name"]
FACING_DEG = 50.0          # 视线和对手方向的水平夹角小于这个值，算"正对着他"
TRADE_WINDOW_S = 10.0      # 对手在杀我之前多少秒内杀过我队友，算"队友位置被打穿"
INFO_LEAD_S = 1.0          # 队友至少提前多少秒看见过对手，算"有信息但我没防住"


def load(p):
    """duels.load：p 是已经打开的 DemoParser。"""
    header = p.parse_header()
    deaths = p.parse_event(
        "player_death",
        player=["X", "Y", "Z", "last_place_name", "team_num", "pitch", "yaw"],
        other=["total_rounds_played", "is_warmup_period"],
    )
    # 正式开局 = 最后一次 begin_new_match（FACEIT 开局前会重启，重启前的击杀不带 warmup 标记）
    try:
        match_start = int(p.parse_event("begin_new_match")["tick"].max())
    except Exception:
        match_start = 0
    deaths = deaths[~deaths["is_warmup_period"].astype(bool) & (deaths["tick"] > match_start)]

    hurts = p.parse_event("player_hurt")[["tick", "attacker_steamid", "user_steamid", "dmg_health", "health", "weapon"]]
    hurts = hurts[hurts["tick"] > match_start]
    hurts = hurts.astype({"attacker_steamid": str, "user_steamid": str})

    freeze_end = p.parse_event("round_freeze_end", other=["total_rounds_played"])
    freeze_end = freeze_end[freeze_end["tick"] > match_start]
    round_end = p.parse_event("round_end", other=["total_rounds_played"])
    round_end = round_end[(round_end["tick"] > match_start) & round_end["winner"].notna()]
    bombs = {e: p.parse_event(e) for e in ("bomb_planted", "bomb_defused", "player_blind")}
    bombs = {e: (df if isinstance(df, pd.DataFrame) else pd.DataFrame(columns=["tick", "user_steamid", "attacker_steamid", "blind_duration"]))
             for e, df in bombs.items()}
    bombs = {e: (df[df["tick"] > match_start] if len(df) else df) for e, df in bombs.items()}

    ticks = p.parse_ticks(TICK_FIELDS)
    return header, deaths, hurts, freeze_end, round_end, ticks, bombs


def list_players(ticks):
    names = (ticks.dropna(subset=["steamid"])
             .groupby("steamid")["name"].agg(lambda s: s.mode().iat[0]))
    return names


def resolve_player(ticks, query):
    """duels.resolve_player；找不到时抛 FactsError（列出玩家）而不是 SystemExit。"""
    names = list_players(ticks)
    q = str(query).strip()
    if q in names.index.astype(str):
        sid = q
    else:
        hit = names[names.str.lower() == q.lower()]
        if hit.empty:
            hit = names[names.str.lower().str.contains(q.lower(), regex=False)]
        if len(hit) != 1:
            raise FactsError(f"这个 demo 里找不到唯一匹配“{q}”的玩家。demo 里的玩家："
                             + "；".join(f"{n}（{s}）" for s, n in names.items()))
        sid = hit.index[0]
    return str(sid), names[sid] if sid in names.index else names[int(sid)]


def view_vector(pitch_deg, yaw_deg):
    p, y = math.radians(pitch_deg), math.radians(yaw_deg)
    return np.array([math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), -math.sin(p)])


def aim_error(me_row, enemy_row):
    """(总偏差, 水平偏差, 垂直偏差)，单位度。"""
    def eye_z(row):
        duck = getattr(row, "duck_amount", 0.0)
        duck = 0.0 if pd.isna(duck) else float(duck)
        return row.Z + EYE_HEIGHT - CROUCH_DROP * duck
    eye = np.array([me_row.X, me_row.Y, eye_z(me_row)])
    head = np.array([enemy_row.X, enemy_row.Y, eye_z(enemy_row)])
    d = head - eye
    dist = np.linalg.norm(d)
    if dist < 1:
        return np.nan, np.nan, np.nan
    need_yaw = math.degrees(math.atan2(d[1], d[0]))
    need_pitch = -math.degrees(math.atan2(d[2], math.hypot(d[0], d[1])))
    v = view_vector(me_row.pitch, me_row.yaw)
    total = math.degrees(math.acos(np.clip(np.dot(v, d / dist), -1, 1)))
    horiz = (me_row.yaw - need_yaw + 180) % 360 - 180
    vert = need_pitch - me_row.pitch
    return total, horiz, vert


def first_seen_tick(spot_ticks, end_tick):
    window = spot_ticks[(spot_ticks <= end_tick) &
                        (spot_ticks >= end_tick - SPOT_LOOKBACK_S * TICKRATE)]
    if len(window) == 0:
        return None
    start = window[-1]
    for t in window[::-1][1:]:
        if start - t > SPOT_GAP_TICKS:
            break
        start = t
    return int(start)


def build_spot_index(ticks, player_sid):
    seen_by_me = ticks["approximate_spotted_by"].map(
        lambda obs: obs is not None and any(str(o) == player_sid for o in obs))
    rows = ticks.loc[seen_by_me, ["tick", "steamid"]]
    return {str(tgt): np.sort(g["tick"].to_numpy()) for tgt, g in rows.groupby("steamid")}


def round_lookup(freeze_end, round_end):
    fe = freeze_end.sort_values("tick")
    return {int(r.total_rounds_played): int(r.tick) for r in fe.itertuples()}


def first_hit(hurts, a, b, end_tick):
    w = hurts[(hurts.tick <= end_tick) & (hurts.tick >= end_tick - SPOT_LOOKBACK_S * TICKRATE)]
    w = w[((w.attacker_steamid == a) & (w.user_steamid == b)) |
          ((w.attacker_steamid == b) & (w.user_steamid == a))]
    if w.empty:
        return None, None
    first = w.sort_values("tick").iloc[0]
    return ("a" if first.attacker_steamid == a else "b"), int(first.tick)


def facing_angle(obs_row, tgt_row):
    need = math.degrees(math.atan2(tgt_row.Y - obs_row.Y, tgt_row.X - obs_row.X))
    return abs((obs_row.yaw - need + 180) % 360 - 180)


def duel_type(smoke, me_ang, en_ang):
    if smoke:
        return "隔烟"
    if np.isnan(me_ang) or np.isnan(en_ang):
        return "未知"
    me_f, en_f = me_ang <= FACING_DEG, en_ang <= FACING_DEG
    if me_f and en_f:
        return "正面对枪"
    if me_f:
        return "我打他侧身"
    if en_f:
        return "他打我侧身"
    return "混战"


def _rnd(v):
    return None if pd.isna(v) else round(v)


def extract_duels(deaths, hurts, ticks, player_sid, freeze_end):
    ticks = ticks.copy()
    ticks["steamid"] = ticks["steamid"].astype(str)
    spot = build_spot_index(ticks, player_sid)
    by_tick = ticks.drop(columns=["approximate_spotted_by"]).set_index(["tick", "steamid"]).sort_index()
    mine_rows = ticks[ticks["steamid"] == player_sid]
    seen_me_by = dict(zip(mine_rows["tick"], mine_rows["approximate_spotted_by"]))
    ex = mine_rows[["tick", "approximate_spotted_by"]].explode("approximate_spotted_by").dropna()
    seen_by_enemy = {str(o): np.sort(g["tick"].to_numpy()) for o, g in ex.groupby(ex["approximate_spotted_by"].astype(str))}
    fe = round_lookup(freeze_end, None)
    spotted_cache = {}

    def spotted_of(sid, t0, t1):
        if sid not in spotted_cache:
            rows = ticks[ticks["steamid"] == sid]
            spotted_cache[sid] = rows.set_index("tick")["approximate_spotted_by"].sort_index()
        s = spotted_cache[sid]
        return s.loc[t0:t1].items()

    def mates_at(t, team):
        try:
            snap = by_tick.loc[t]
        except KeyError:
            return set()
        return set(snap.index[snap["team_num"] == team]) - {player_sid}

    d = deaths.copy()
    d["attacker_steamid"] = d["attacker_steamid"].astype(str)
    d["user_steamid"] = d["user_steamid"].astype(str)
    mine = d[((d.attacker_steamid == player_sid) | (d.user_steamid == player_sid))
             & (d.attacker_team_num != d.user_team_num)
             & d.attacker_steamid.ne("nan") & d.attacker_steamid.ne("0")]

    out = []
    for r in mine.itertuples():
        won = r.attacker_steamid == player_sid
        enemy = r.user_steamid if won else r.attacker_steamid
        kill_tick = int(r.tick)
        rnd = int(r.total_rounds_played)
        me_seen = first_seen_tick(spot.get(enemy, np.array([])), kill_tick)
        rec = {
            "回合": rnd + 1,
            "阵营": TEAM_NAME.get(int(r.attacker_team_num if won else r.user_team_num), "?"),
            "结果": "赢" if won else "输",
            "开局第几秒": round((kill_tick - fe.get(rnd, kill_tick)) / TICKRATE, 1),
            "我的报点": r.attacker_last_place_name if won else r.user_last_place_name,
            "敌人报点": r.user_last_place_name if won else r.attacker_last_place_name,
            "敌人": r.user_name if won else r.attacker_name,
            "武器": r.weapon,
            "爆头": bool(r.headshot) if won else None,
            "tick": kill_tick,
            "我X": _rnd(r.attacker_X if won else r.user_X),
            "我Y": _rnd(r.attacker_Y if won else r.user_Y),
            "敌X": _rnd(r.user_X if won else r.attacker_X),
            "敌Y": _rnd(r.user_Y if won else r.attacker_Y),
        }
        obs = seen_me_by.get(kill_tick - 1)
        rec["看见我的敌人数"] = 0 if obs is None else len(set(map(str, obs)))
        if me_seen is not None:
            try:
                me_row = by_tick.loc[(me_seen, player_sid)]
                en_row = by_tick.loc[(me_seen, enemy)]
                tot, hor, ver = aim_error(me_row, en_row)
            except KeyError:
                tot = hor = ver = np.nan
            rec.update({
                "看见敌人时准星偏差°": round(tot, 1),
                "左右偏差°": round(hor, 1),
                "高低偏差°": round(ver, 1),
                "看见到结束秒": round((kill_tick - me_seen) / TICKRATE, 2),
            })
        else:
            rec.update({"看见敌人时准星偏差°": np.nan, "左右偏差°": np.nan,
                        "高低偏差°": np.nan, "看见到结束秒": np.nan})
        fh, fh_tick = first_hit(hurts, player_sid, enemy, kill_tick)
        rec["谁先打中"] = {"a": "我", "b": "敌人"}.get(fh, "—")
        rec["隔烟"] = bool(r.thrusmoke)
        en_seen = first_seen_tick(seen_by_enemy.get(enemy, np.array([])), kill_tick)
        starts = [t for t in (me_seen, en_seen) if t is not None]
        engage = min(starts) if starts else (fh_tick if fh_tick is not None else kill_tick - 1)
        try:
            me_e, en_e = by_tick.loc[(engage, player_sid)], by_tick.loc[(engage, enemy)]
            me_ang, en_ang = facing_angle(me_e, en_e), facing_angle(en_e, me_e)
        except KeyError:
            me_ang = en_ang = np.nan
        rec["我朝向夹角°"] = None if np.isnan(me_ang) else round(me_ang)
        rec["敌朝向夹角°"] = None if np.isnan(en_ang) else round(en_ang)
        rec["交火类型"] = duel_type(bool(r.thrusmoke), me_ang, en_ang)
        rec["敌人ID"] = enemy
        rec["开打tick"] = int(engage)
        rec["对手刚杀过队友"] = rec["队友先看见对手"] = False
        if not won:
            my_team = r.user_team_num
            recent = d[(d.attacker_steamid == enemy) & (d.user_team_num == my_team)
                       & (d.user_steamid != player_sid)
                       & (d.tick < kill_tick) & (d.tick >= kill_tick - TRADE_WINDOW_S * TICKRATE)]
            rec["对手刚杀过队友"] = not recent.empty
            mates = mates_at(kill_tick - 1, my_team)
            cutoff = (me_seen if me_seen is not None else kill_tick) - INFO_LEAD_S * TICKRATE
            for t, obs in spotted_of(enemy, kill_tick - SPOT_LOOKBACK_S * 1.5 * TICKRATE, cutoff):
                if obs is not None and mates & set(map(str, obs)):
                    rec["队友先看见对手"] = True
                    break
        if me_seen is None:
            rec["备注"] = "隔烟" if bool(r.thrusmoke) else "无可见记录"
        else:
            rec["备注"] = ""
        out.append(rec)
    return pd.DataFrame(out)


# =============================================================================== build_report.py（原样）
AIM_OFF_DEG = 5.0
TRADE_S = 5.0
MOVING_UPS = 70
UTIL = {"hegrenade", "inferno", "molotov", "incgrenade"}
PLACE_FIX = {
    "de_mirage": [("PalaceInterior", (0, 700, -1760, -1420), "TRamp")],
}


def _norm(w):
    return re.sub(r"[^a-z0-9]", "", str(w).lower())


TIERS = {}
for _names, _tier in [
    ("glock18 glock usps uspsilencer p2000 hkp2000 p250 fiveseven tec9 cz75auto cz75a deserteagle deagle "
     "dualberettas elite r8revolver revolver", 0),
    ("mac10 mp9 mp7 mp5sd ump45 p90 ppbizon bizon nova xm1014 mag7 sawedoff negev m249 ssg08", 1),
    ("ak47 m4a4 m4a1s m4a1silencer m4a1 galilar famas aug sg553 sg556 awp g3sg1 scar20", 2),
]:
    for _n in _names.split():
        TIERS[_n] = _tier
TIER_NAME = {0: "手枪", 1: "冲锋枪/喷子", 2: "步枪/狙"}


def weapon_tier(weapon, equip_value):
    t = TIERS.get(_norm(weapon))
    if t is not None:
        return t
    if equip_value is None or pd.isna(equip_value):
        return None
    return 2 if equip_value >= 3000 else 1 if equip_value >= 1700 else 0


def econ_class(avg_equip, round_no):
    if round_no in (1, 13):
        return "手枪局"
    return "eco" if avg_equip < 1600 else "强起" if avg_equip < 3500 else "长枪"


def fix_place(mapname, place, x, y):
    for src, (x0, x1, y0, y1), dst in PLACE_FIX.get(mapname, []):
        if place == src and x0 <= x <= x1 and y0 <= y <= y1:
            return dst
    return place


def loss_reason(r):
    if r["隔烟"]:
        return "smoke"
    if r["看见我的敌人数"] >= 2:
        return "crossed"
    unseen = pd.isna(r["看见到结束秒"])
    if r["交火类型"] in ("他打我侧身", "混战") or (unseen and r["交火类型"] != "正面对枪"):
        if r["对手刚杀过队友"]:
            return "teammate"
        if r["队友先看见对手"]:
            return "info"
        return "flank"
    if unseen:
        return "hidden"
    if r["谁先打中"] == "我":
        return "nokill"
    if r["看见敌人时准星偏差°"] > AIM_OFF_DEG:
        return "aim"
    return "slow"


def add_context(df, ticks, hurts, extras, freeze_end, sid):
    """局势（守点/转点/回防，控图/打点/下包后）、对枪方式（架枪/拉出/遭遇）和道具影响。"""
    t = ticks[["tick", "steamid", "X", "Y", "health", "last_place_name", "team_num", "is_alive",
               "current_equip_value", "active_weapon_name"]].copy()
    t["steamid"] = t["steamid"].astype(str)
    t = t.set_index(["tick", "steamid"]).sort_index()
    fe = round_lookup(freeze_end, None)
    plants = sorted(int(x) for x in extras.get("bomb_planted", pd.DataFrame(columns=["tick"]))["tick"])
    blind = extras.get("player_blind", pd.DataFrame(columns=["tick", "user_steamid", "blind_duration"]))
    blind = blind[blind["user_steamid"].astype(str) == sid].copy()
    blind["attacker_steamid"] = blind["attacker_steamid"].astype(str)
    my_hurts = hurts[(hurts["user_steamid"] == sid) & hurts["weapon"].isin(UTIL)]
    me_rows = t.xs(sid, level="steamid")

    def at(tick, who):
        try:
            return t.loc[(tick, who)]
        except KeyError:
            return None

    def speed(tick, who, dt=8):
        a, b = at(tick, who), at(tick - dt, who)
        if a is None or b is None:
            return np.nan
        return math.hypot(a.X - b.X, a.Y - b.Y) * TICKRATE / dt

    setup = {}
    phase, role, hp, blinded, utild, my_v, en_v = [], [], [], [], [], [], []
    team_flash, mine_alive, their_alive, my_tier, en_tier = [], [], [], [], []
    for r in df.itertuples():
        start = fe.get(r.回合 - 1, 0)
        eng, kill = r.开打tick, r.tick
        planted = any(start < p_ <= eng for p_ in plants)
        here = at(eng, sid)
        place_now = here.last_place_name if here is not None else r.我的报点
        if r.阵营 == "CT":
            if planted:
                ph = "回防"
            else:
                if r.回合 not in setup:
                    w = me_rows.loc[start + 10 * TICKRATE: start + 25 * TICKRATE, "last_place_name"]
                    setup[r.回合] = w.mode().iat[0] if len(w.dropna()) else None
                ph = "守点" if setup[r.回合] in (None, place_now) else "转点/补防"
        else:
            ph = "下包后" if planted else ("前期控图" if (eng - start) / TICKRATE < 30 else "进攻打点")
        phase.append(ph)
        v1, v2 = speed(eng, sid), speed(eng, r.敌人ID)
        my_v.append(None if np.isnan(v1) else round(v1)); en_v.append(None if np.isnan(v2) else round(v2))
        if np.isnan(v1) or np.isnan(v2):
            role.append("未知")
        else:
            m1, m2 = v1 > MOVING_UPS, v2 > MOVING_UPS
            role.append({(False, True): "架枪", (True, False): "拉出", (True, True): "遭遇", (False, False): "对峙"}[(m1, m2)])
        hp.append(int(here.health) if here is not None and not pd.isna(here.health) else None)
        end = kill
        b = blind[(blind["tick"] <= end) & (blind["tick"] + blind["blind_duration"] * TICKRATE >= eng)]
        b = b[b["blind_duration"] >= 1.0]
        blinded.append(bool(len(b)))
        my_team = here.team_num if here is not None else None
        tf = False
        for bb in b.itertuples():
            fl = at(int(bb.tick), bb.attacker_steamid)
            if fl is not None and my_team is not None and fl.team_num == my_team and bb.attacker_steamid != sid:
                tf = True
        team_flash.append(tf)
        try:
            snap = t.loc[eng]
            alive = snap[snap["is_alive"].astype(bool)]
            mine_alive.append(int((alive["team_num"] == my_team).sum()))
            their_alive.append(int((alive["team_num"] != my_team).sum()))
        except KeyError:
            mine_alive.append(None); their_alive.append(None)
        en = at(eng, r.敌人ID)
        won = r.结果 == "赢"
        my_w = r.武器 if won else (here.active_weapon_name if here is not None else None)
        en_w = (en.active_weapon_name if en is not None else None) if won else r.武器
        my_tier.append(weapon_tier(my_w, here.current_equip_value if here is not None else None))
        en_tier.append(weapon_tier(en_w, en.current_equip_value if en is not None else None))
        u = my_hurts[(my_hurts["tick"] <= eng) & (my_hurts["tick"] >= eng - 10 * TICKRATE)]
        utild.append(int(u["dmg_health"].sum()))
    df["局势"], df["对枪方式"], df["开打血量"] = phase, role, hp
    df["被闪"], df["道具掉血"], df["我速度"], df["敌速度"] = blinded, utild, my_v, en_v
    df["被队友闪"], df["我方存活"], df["敌方存活"] = team_flash, mine_alive, their_alive
    df["我的枪档"], df["敌人枪档"] = my_tier, en_tier
    return df


SITE_PLACES = {"BombsiteA", "BombsiteB", "CTSpawn", "Jungle", "Stairs", "Truck", "Shop", "Scaffolding"}
CHOKE_PLACES = {"TRamp", "PalaceInterior", "Connector", "Apartments", "Catwalk"}
ROLE_NAME = {"拉出": "拉枪", "遭遇": "双方都在动", "对峙": "双方都没动"}


def t_phase(old, place, t):
    if old == "下包后":
        return "守包"
    if place in SITE_PLACES or (place in CHOKE_PLACES and t >= 30):
        return "进点"
    return "前期控图" if t < 30 else "中期"


def relabel(df):
    df["局势"] = [t_phase(ph, pl, t) if side == "T" else ph
                for ph, pl, t, side in zip(df["局势"], df["我的报点"], df["开局第几秒"], df["阵营"])]
    df["对枪方式"] = df["对枪方式"].map(lambda r: ROLE_NAME.get(r, r))
    return df


def blame(r, rd):
    """输掉的交火算谁的：('self' / 'team' / 'cost', 理由)"""
    econ, eecon = (rd or {}).get("econ"), (rd or {}).get("eecon")
    mt, et = r["我的枪档"], r["敌人枪档"]
    a, b = r["我方存活"], r["敌方存活"]
    ok = a is not None and b is not None and not pd.isna(a) and not pd.isna(b)
    if ok:
        a, b = max(int(a), 1), int(b)
    adv = (a - b) if ok else 0
    sp = lambda e: f" {e}" if e and e.isascii() else e   # noqa: E731
    aggressive = r["对枪方式"] in ("拉出", "遭遇")
    if econ == "eco" and eecon in ("强起", "长枪"):
        return "cost", f"我方 eco 打对方{sp(eecon)}，这回合的任务本来就是赌点、偷人头，输掉正面对枪是正常代价"
    if mt is not None and et is not None and not pd.isna(mt) and not pd.isna(et) and et - mt >= 2:
        return "cost", f"你拿{TIER_NAME[int(mt)]}打对方{TIER_NAME[int(et)]}，装备差距太大"
    if ok and b - a >= 2 and a <= 2:
        return "cost", f"当时 {int(a)}v{int(b)}，人数劣势，输了很正常"
    if rd and rd.get("traded") and rd.get("won"):
        return "cost", "你阵亡后被队友补枪换回，回合也赢了，是有价值的阵亡"
    if r["被队友闪"]:
        return "team", "被队友的闪光闪白"
    if r["原因"] == "teammate":
        return "team", "队友的位置先被打穿，对手接着打到了你"
    if r["原因"] == "flank":
        return "team", "没人看的侧翼被绕，当时队友也没发现这个人"
    if r["原因"] == "crossed" and adv < 0:
        return "team", f"{int(a)}v{int(b)} 人数劣势时被交叉火力架住"
    if econ == "长枪" and eecon in ("eco", "强起", "手枪局"):
        return "self", (f"我方长枪打对方{sp(eecon)}，你却主动去找人头被反杀，送了一把枪" if aggressive
                        else f"我方长枪打对方{sp(eecon)}，装备占优却输了对枪")
    if adv >= 2 and aggressive:
        return "self", f"{int(a)}v{int(b)} 人数优势时还主动拉枪，应该等队友一起、靠补枪和时间赢"
    if ok and a == 1 and b >= 3 and mt == 2:
        return "self", f"1v{int(b)} 还拿着步枪去对枪，这时保枪通常更划算"
    return "self", None


def full_reason(r):
    base = loss_reason(r)
    if base == "smoke":
        return base
    if r["被闪"]:
        return "flashed"
    if r["道具掉血"] >= 30 and (r["开打血量"] or 100) <= 70:
        return "utildmg"
    if base in ("hidden", "slow", "aim"):
        if r["对枪方式"] == "架枪":
            return "outpeeked"
        if r["对枪方式"] == "拉出" and base != "aim":
            return "peekedinto"
    if base == "hidden":
        return "slow"
    return base


def round_stats(deaths, round_end, freeze_end, ticks, sid, demo_idx):
    """每回合一行：我的阵营、击杀数、是否阵亡、首杀情况、阵亡是否被补枪、回合输赢。"""
    d = deaths.copy()
    for c in ("attacker_steamid", "user_steamid"):
        d[c] = d[c].astype(str)
    d["round"] = d["total_rounds_played"].astype(int) + 1
    enemy_kills = d[(d.attacker_team_num != d.user_team_num) & d.attacker_steamid.ne("nan") & d.attacker_steamid.ne("0")]
    fe = round_lookup(freeze_end, None)
    me = ticks[ticks["steamid"].astype(str) == sid].set_index("tick")["team_num"].sort_index()
    rows = []
    for re_ in round_end.sort_values("tick").itertuples():
        n = int(re_.total_rounds_played)
        start = fe.get(n - 1)
        if start is None:
            continue
        team = me.asof(start)
        if pd.isna(team) or int(team) not in TEAM_NAME:
            continue
        side = TEAM_NAME[int(team)]
        rk_all = enemy_kills[enemy_kills["round"] == n]
        buy_end = start + 20 * TICKRATE
        if len(rk_all):
            buy_end = min(buy_end, int(rk_all["tick"].min()) - 1)
        snap = ticks[(ticks["tick"] == buy_end) & ticks["team_num"].isin([2, 3])]
        mine_eq = snap[snap["team_num"] == int(team)]["current_equip_value"].mean()
        their_eq = snap[snap["team_num"] != int(team)]["current_equip_value"].mean()
        rk = enemy_kills[enemy_kills["round"] == n].sort_values("tick")
        kills = int((rk.attacker_steamid == sid).sum())
        my_death = rk[rk.user_steamid == sid]
        died = bool((d[(d["round"] == n)].user_steamid == sid).any())
        opening, open_tick = None, None
        if len(rk):
            open_tick = int(rk.iloc[0].tick)
            first = rk.iloc[0]
            opening = "win" if first.attacker_steamid == sid else "loss" if first.user_steamid == sid else None
        traded = None
        if len(my_death):
            k = my_death.iloc[0]
            revenge = rk[(rk.user_steamid == k.attacker_steamid) & (rk.tick > k.tick)
                         & (rk.tick <= k.tick + TRADE_S * TICKRATE)]
            traded = not revenge.empty
        rows.append({"d": demo_idx, "r": n, "side": side, "k": kills, "died": died,
                     "econ": econ_class(mine_eq, n), "eecon": econ_class(their_eq, n),
                     "eq": None if pd.isna(mine_eq) else int(mine_eq), "eeq": None if pd.isna(their_eq) else int(their_eq),
                     "open": opening, "openTick": open_tick, "traded": traded, "won": str(re_.winner) == side})
    return rows


# =============================================================================== extract_pro.py（每个 demo 的部分）
SAMPLE = 32                     # 0.5 秒
POS_FIELDS = ["X", "Y", "Z", "yaw", "pitch", "team_num", "is_alive", "health", "last_place_name",
              "active_weapon_name", "current_equip_value", "armor_value", "has_defuser"]
GRENADES = {"smokegrenade_detonate": "smoke", "flashbang_detonate": "flash",
            "hegrenade_detonate": "he", "inferno_startburn": "molotov"}


def group_of(did):
    """groups.csv 里这个 demo 的组（对标库里已有的 demo），没有就是 user。"""
    g = config.DATA / 'groups.csv'
    if g.exists():
        G = pd.read_csv(g, dtype={'demo': str})
        hit = G.loc[G['demo'] == did, 'group']
        if len(hit):
            return str(hit.iloc[0])
    return 'user'


def _extract_pro(ctx, p, loaded, out, group, user_sid):
    """extract_pro.main 里一个 demo 的循环体。返回 (user 的 add_context 后交火表或 None)。"""
    did = ctx.id
    header, deaths, hurts, fe, re_, ticks, extras = loaded
    fe_map = round_lookup(fe, None)
    ends = re_.sort_values("tick")
    last_end = int(ends["tick"].max())

    # ---------- 回合 ----------
    plants = extras.get("bomb_planted", pd.DataFrame())
    rows = []
    for r in ends.itertuples():
        n = int(r.total_rounds_played); start = fe_map.get(n - 1)
        if start is None:
            continue
        end = int(r.tick)
        pl = plants[(plants["tick"] > start) & (plants["tick"] <= end)] if len(plants) else plants
        plant_tick = int(pl["tick"].iloc[0]) if len(pl) else None
        plant_place = None
        if plant_tick is not None:
            who = str(pl["user_steamid"].iloc[0])
            snap = ticks[(ticks["tick"] == plant_tick) & (ticks["steamid"].astype(str) == who)]
            plant_place = snap["last_place_name"].iloc[0] if len(snap) else None
        buy = start + 20 * TICKRATE
        k_round = deaths[(deaths["tick"] > start) & (deaths["tick"] <= end)]
        if len(k_round):
            buy = min(buy, int(k_round["tick"].min()) - 1)
        snap = ticks[(ticks["tick"] == buy) & ticks["team_num"].isin([2, 3])]
        eq = snap.groupby("team_num")["current_equip_value"].mean()
        rows.append({"demo": did, "round": n, "start_tick": start, "end_tick": end,
                     "dur_s": round((end - start) / TICKRATE, 1), "winner": str(r.winner), "reason": str(r.reason),
                     "plant_tick": plant_tick, "plant_s": None if plant_tick is None else round((plant_tick - start) / TICKRATE, 1),
                     "plant_site": None if plant_place is None else ("A" if "A" in str(plant_place) or "Palace" in str(plant_place) else "B"),
                     "plant_place": plant_place,
                     "t_equip": None if 2 not in eq else int(eq[2]), "ct_equip": None if 3 not in eq else int(eq[3]),
                     "t_econ": None if 2 not in eq else econ_class(eq[2], n),
                     "ct_econ": None if 3 not in eq else econ_class(eq[3], n)})
    rounds = pd.DataFrame(rows)
    if not len(rounds):
        raise FactsError('这个 demo 里没有完整的回合（找不到冻结结束和回合结束）')
    rounds.to_csv(out / "rounds.csv", index=False, encoding=ENC)
    start_of = dict(zip(rounds["round"], rounds["start_tick"]))
    end_of = dict(zip(rounds["round"], rounds["end_tick"]))

    def rnd_of(tick):
        for n, s in start_of.items():
            if s <= tick <= (end_of[n] if end_of[n] != last_end else 10 ** 12):
                return n
        return None

    # ---------- 每 0.5 秒位置 ----------
    want = sorted({t for n, s in start_of.items() for t in range(s, end_of[n] + 1, SAMPLE)})
    pos = p.parse_ticks(POS_FIELDS, ticks=want)
    pos = pos[pos["team_num"].isin([2, 3])].copy()
    tick_round = {t: n for n, s in start_of.items() for t in range(s, end_of[n] + 1, SAMPLE)}
    pos["round"] = pos["tick"].map(tick_round)
    pos["t"] = ((pos["tick"] - pos["round"].map(start_of)) / TICKRATE).round(1)
    pos["side"] = pos["team_num"].map(TEAM_NAME)
    pos["place"] = [fix_place("de_mirage", pl, x, y) for pl, x, y in zip(pos["last_place_name"], pos["X"], pos["Y"])]
    pos.insert(0, "demo", did)
    pos[["demo", "round", "t", "tick", "steamid", "name", "side", "X", "Y", "Z", "yaw", "pitch", "is_alive",
         "health", "place", "active_weapon_name", "current_equip_value", "armor_value", "has_defuser"]] \
        .round({"X": 0, "Y": 0, "Z": 0, "yaw": 1, "pitch": 1}) \
        .to_csv(out / "positions.csv", index=False, encoding=ENC)

    # ---------- 击杀 ----------
    k = deaths.copy()
    for c in ("attacker_steamid", "user_steamid", "assister_steamid"):
        k[c] = k[c].astype(str)
    k["round"] = k["tick"].map(rnd_of)
    k = k[k["round"].notna()].sort_values("tick")
    k["t"] = ((k["tick"] - k["round"].map(start_of)) / TICKRATE).round(1)
    k["attacker_side"] = k["attacker_team_num"].map(TEAM_NAME)
    k["victim_side"] = k["user_team_num"].map(TEAM_NAME)
    k["enemy_kill"] = (k["attacker_team_num"] != k["user_team_num"]) & k["attacker_steamid"].ne("nan")
    k["attacker_place"] = [fix_place("de_mirage", pl, x, y) for pl, x, y in zip(k["attacker_last_place_name"], k["attacker_X"], k["attacker_Y"])]
    k["victim_place"] = [fix_place("de_mirage", pl, x, y) for pl, x, y in zip(k["user_last_place_name"], k["user_X"], k["user_Y"])]
    first = k[k["enemy_kill"]].groupby("round")["tick"].min()
    k["opening"] = k["tick"] == k["round"].map(first)
    traded = []
    ek = k[k["enemy_kill"]]
    for r in k.itertuples():
        if not r.enemy_kill:
            traded.append(None); continue
        rev = ek[(ek["user_steamid"] == r.attacker_steamid) & (ek["tick"] > r.tick) & (ek["tick"] <= r.tick + 5 * TICKRATE)
                 & (ek["attacker_team_num"] == r.user_team_num)]
        traded.append(bool(len(rev)))
    k["traded"] = traded
    k.insert(0, "demo", did)
    k[["demo", "round", "t", "tick", "attacker_name", "attacker_steamid", "attacker_side", "attacker_place",
       "attacker_X", "attacker_Y", "user_name", "user_steamid", "victim_side", "victim_place", "user_X", "user_Y",
       "weapon", "headshot", "thrusmoke", "penetrated", "attackerblind", "distance", "enemy_kill", "opening", "traded"]] \
        .to_csv(out / "kills.csv", index=False, encoding=ENC)

    # ---------- 道具 ----------
    try:
        ug = []
        for ev, kind in GRENADES.items():
            g = p.parse_event(ev, player=["team_num", "last_place_name"])
            if not len(g):
                continue
            g = g.copy(); g["kind"] = kind
            ug.append(g)
        u = pd.concat(ug, ignore_index=True)
        u["round"] = u["tick"].map(rnd_of)
        u = u[u["round"].notna()]
        u["t"] = ((u["tick"] - u["round"].map(start_of)) / TICKRATE).round(1)
        u["side"] = u["user_team_num"].map(TEAM_NAME)
        pts = pos[["X", "Y", "place"]].dropna().drop_duplicates()
        tree_xy = pts[["X", "Y"]].to_numpy()
        land = []
        for x, y in zip(u["x"], u["y"]):
            d = ((tree_xy[:, 0] - x) ** 2 + (tree_xy[:, 1] - y) ** 2)
            land.append(pts["place"].iloc[int(d.argmin())] if len(d) else None)
        u["land_place"] = land
        u["thrower_place"] = u.get("user_last_place_name")
        u.insert(0, "demo", did)
        u[["demo", "round", "t", "tick", "kind", "user_name", "user_steamid", "side", "thrower_place", "x", "y", "z", "land_place"]] \
            .to_csv(out / "util.csv", index=False, encoding=ENC)
    except Exception as e:
        print(f"  道具解析失败 {did}: {e}", flush=True)
        pd.DataFrame(columns=["demo", "round", "t", "tick", "kind", "user_name", "user_steamid", "side", "thrower_place",
                              "x", "y", "z", "land_place"]).to_csv(out / "util.csv", index=False, encoding=ENC)

    # ---------- 每个玩家的交火 ----------
    frames = []
    user_df = None
    names = list_players(ticks)
    for sid in names.index.astype(str):
        try:
            df = extract_duels(deaths, hurts, ticks, sid, fe)
            if not len(df):
                continue
            df = add_context(df, ticks, hurts, extras, fe, sid)
            if sid == user_sid:
                user_df = df.copy()          # build_report 用的是同一份（extract_duels + add_context）
            df["原因"] = [full_reason(r) if r["结果"] == "输" else None for _, r in df.iterrows()]
            df.insert(0, "player", names.get(sid, names.get(int(sid), sid)) if hasattr(names, "get") else sid)
            df.insert(0, "steamid", sid)
            df.insert(0, "demo", did)
            frames.append(df)
        except Exception as e:
            print(f"  交火提取失败 {did} {sid}: {type(e).__name__}: {e}", flush=True)
    if frames:
        dd = pd.concat(frames, ignore_index=True)
        dd["我的报点"] = [fix_place("de_mirage", p_, x, y) for p_, x, y in zip(dd["我的报点"], dd["我X"], dd["我Y"])]
        dd["敌人报点"] = [fix_place("de_mirage", p_, x, y) for p_, x, y in zip(dd["敌人报点"], dd["敌X"], dd["敌Y"])]
        dd["group"] = group
        dd.to_csv(out / "duels.csv", index=False, encoding=ENC)
    else:
        raise FactsError('这个 demo 里一次交火都没有提取到')
    print(f"[{group}] {did}: {len(rounds)} 回合, {len(pos)} 位置样本, {int(k['enemy_kill'].sum())} 击杀, "
          f"{sum(len(f) for f in frames)} 交火", flush=True)
    return user_df


# =============================================================================== sit_parse.py（一个 demo）
def _sit_parse(p, did, out):
    R = pd.read_csv(out / 'rounds.csv')
    K = pd.read_csv(out / 'kills.csv')
    first_kill = K.groupby('round').tick.min().to_dict()
    tick_rows = []
    for r in R.itertuples():
        st = int(r.start_tick)
        buy = st + 20 * TICKRATE
        fk = first_kill.get(r.round)
        if fk is not None and fk == fk:
            buy = min(buy, int(fk) - 1)
        buy = max(buy, st)
        tick_rows.append((r.round, 'fe', st))
        tick_rows.append((r.round, 'buy', buy))
    ticks = sorted({t for _, _, t in tick_rows})
    df = p.parse_ticks(['inventory', 'balance', 'cash_spent_this_round', 'current_equip_value', 'team_num',
                        'armor_value', 'has_helmet', 'has_defuser'], ticks=ticks)
    df = df[df.team_num.isin([2, 3])]
    rows = []
    for rnd, kind, tk in tick_rows:
        s = df[df.tick == tk].copy()
        s['round'] = rnd; s['kind'] = kind
        rows.append(s)
    E = pd.concat(rows, ignore_index=True)
    E['inventory'] = E['inventory'].apply(lambda v: '|'.join(v) if isinstance(v, (list, tuple)) else str(v))
    E['side'] = E.team_num.map({2: 'T', 3: 'CT'})
    E.insert(0, 'demo', did)
    E['steamid'] = E['steamid'].astype(str)
    E[['demo', 'round', 'kind', 'tick', 'steamid', 'name', 'side', 'balance', 'cash_spent_this_round',
       'current_equip_value', 'armor_value', 'has_helmet', 'has_defuser', 'inventory']] \
        .to_csv(out / 'sit_econ.csv', index=False, encoding=ENC)

    H = p.parse_event('player_hurt', player=['team_num'])
    H = H.astype({'attacker_steamid': str, 'user_steamid': str})
    starts = R.start_tick.values; ends = R.end_tick.values; rnds = R['round'].values
    rr = []
    for tk in H.tick.values:
        m = (starts <= tk) & (tk <= ends)
        rr.append(int(rnds[m][0]) if m.any() else None)
    H['round'] = rr
    H = H[H['round'].notna()].copy()
    H.insert(0, 'demo', did)
    keep = ['demo', 'round', 'tick', 'attacker_steamid', 'attacker_team_num', 'user_steamid', 'user_team_num',
            'dmg_health', 'health', 'weapon']
    H[keep].to_csv(out / 'sit_hurt.csv', index=False, encoding=ENC)


# =============================================================================== sit_rounds.py（每回合每方，一个 demo）
RIFLE = {'AK-47', 'M4A1-S', 'M4A4', 'Galil AR', 'FAMAS', 'AUG', 'SG 553', 'AWP', 'SCAR-20', 'G3SG1'}
SMG = {'MAC-10', 'MP9', 'MP7', 'MP5-SD', 'UMP-45', 'P90', 'PP-Bizon', 'XM1014', 'Nova', 'MAG-7', 'Sawed-Off',
       'Negev', 'M249', 'SSG 08'}
UPG_PISTOL = {'Desert Eagle', 'P250', 'Five-SeveN', 'Tec-9', 'CZ75-Auto', 'Dual Berettas', 'R8 Revolver'}
NADES = {'Smoke Grenade', 'Flashbang', 'High Explosive Grenade', 'Molotov', 'Incendiary Grenade', 'Decoy Grenade'}
OPP = {'T': 'CT', 'CT': 'T'}
SIT_COLS = ['demo', 'group', 'scene', 'round', 'side', 'situation', 'sub', 'econ_sit', 'econ_detail', 'buy', 'enemy_buy',
            'equip', 'enemy_equip', 'pre', 'enemy_pre', 'spent', 'spent_ratio', 'n', 'n_rifle', 'n_awp', 'n_smg', 'n_upg',
            'armor', 'helmet', 'nades', 'prev_result', 'prev_plant', 'prev_kills', 'prev_inferred', 'won', 'plant', 'reason',
            'kills', 'deaths', 'dmg', 'half_end', 'start_tick', 'end_tick', 'user_team', 'user_k', 'user_d', 'user_dmg',
            'user_death_tick', 'user_primary', 'user_equip']


def _inv_set(s):
    return set(str(s).split('|')) if isinstance(s, str) else set()


def _n_nades_raw(s):
    return sum(1 for w in str(s).split('|') if w in NADES) if isinstance(s, str) else 0


def _buy_class(eq, nr):
    if eq < 1600 and nr <= 1:
        return 'eco'
    if nr >= 4 or (nr == 3 and eq >= 3500):
        return '长枪'
    return '强起'


def _r2_choice(ratio):
    return '强起' if ratio >= 0.75 else '半起' if ratio >= 0.5 else 'eco'


def _sit_rounds(did, group, user, out, scene=1):
    R = pd.read_csv(out / 'rounds.csv', dtype={'demo': str})
    K = pd.read_csv(out / 'kills.csv', dtype={'demo': str, 'attacker_steamid': str, 'user_steamid': str})
    K['round'] = K['round'].astype(int)
    K['enemy_kill'] = K.enemy_kill.astype(str).str.lower() == 'true'
    E = pd.read_csv(out / 'sit_econ.csv', dtype={'demo': str, 'steamid': str})
    H = pd.read_csv(out / 'sit_hurt.csv', dtype={'demo': str, 'attacker_steamid': str, 'user_steamid': str})
    H['round'] = H['round'].astype(int)

    # 每人每回合：购买
    fe = E[E.kind == 'fe'].set_index(['demo', 'round', 'steamid'])
    B = E[E.kind == 'buy'].copy()
    B['inv'] = B.inventory.apply(_inv_set)
    fe_inv = fe.inventory.apply(_inv_set)
    B['inv_fe'] = [fe_inv.get((d, r, s), set()) for d, r, s in zip(B.demo, B['round'], B.steamid)]
    B['inv_all'] = [a | b for a, b in zip(B.inv, B.inv_fe)]
    B['rifle'] = B.inv_all.apply(lambda s: bool(s & RIFLE))
    B['awp'] = B.inv_all.apply(lambda s: 'AWP' in s)
    B['smg'] = B.inv_all.apply(lambda s: bool(s & SMG) and not (s & RIFLE))
    B['upg_pistol'] = B.inv_all.apply(lambda s: bool(s & UPG_PISTOL))
    B['nades'] = [max(_n_nades_raw(a), _n_nades_raw(fe.inventory.get((d, r, s_), ''))) for a, d, r, s_ in
                  zip(B.inventory, B.demo, B['round'], B.steamid)]
    B['pre'] = B.balance + B.cash_spent_this_round
    B['primary'] = np.where(B.rifle, 'rifle', np.where(B.smg, 'smg', np.where(B.upg_pistol, 'pistol+', 'pistol')))
    B['equip'] = np.maximum(B.current_equip_value, [fe.current_equip_value.get((d, r, s), 0) for d, r, s in
                                                      zip(B.demo, B['round'], B.steamid)])

    # 每人每回合：击杀 / 阵亡 / 伤害
    ek = K[K.enemy_kill]
    kills_p = ek.groupby(['demo', 'round', 'attacker_steamid']).size()
    death_p = K.groupby(['demo', 'round', 'user_steamid']).tick.min()
    H = H.sort_values(['demo', 'round', 'tick'])
    hp = {}; real = []
    for d, r, v, dmg, h in zip(H.demo, H['round'], H.user_steamid, H.dmg_health, H.health):
        key = (d, r, v); prev = hp.get(key, 100)
        real.append(min(dmg, prev)); hp[key] = h
    H['dmg'] = real
    He = H[(H.attacker_team_num != H.user_team_num) & H.attacker_team_num.isin([2, 3])]
    dmg_p = He.groupby(['demo', 'round', 'attacker_steamid']).dmg.sum()
    B['k'] = [int(kills_p.get((d, r, s), 0)) for d, r, s in zip(B.demo, B['round'], B.steamid)]
    B['death_tick'] = [death_p.get((d, r, s), np.nan) for d, r, s in zip(B.demo, B['round'], B.steamid)]
    B['d'] = B.death_tick.notna().astype(int)
    B['surv'] = 1 - B.d
    B['dmg'] = [float(dmg_p.get((d, r, s), 0)) for d, r, s in zip(B.demo, B['round'], B.steamid)]
    B['group'] = group

    # 每回合每方
    T = B.groupby(['demo', 'round', 'side']).agg(
        n=('steamid', 'size'), equip=('equip', 'mean'), pre=('pre', 'mean'), spent=('cash_spent_this_round', 'mean'),
        spent_sum=('cash_spent_this_round', 'sum'), pre_sum=('pre', 'sum'), n_rifle=('rifle', 'sum'), n_awp=('awp', 'sum'),
        n_smg=('smg', 'sum'), n_upg=('upg_pistol', 'sum'), nades=('nades', 'mean'), armor=('armor_value', lambda s: (s > 0).sum()),
        helmet=('has_helmet', 'sum'), kills=('k', 'sum'), deaths=('d', 'sum'), dmg=('dmg', 'sum')).reset_index()
    T['spent_ratio'] = (T.spent_sum / T.pre_sum.clip(lower=1)).round(2)
    T['buy'] = [_buy_class(e, n) for e, n in zip(T.equip, T.n_rifle)]
    T.loc[T['round'].isin([1, 13]), 'buy'] = '手枪'
    R['round'] = R['round'].astype(int)
    RI = R.set_index(['demo', 'round'])
    T['won'] = [RI.winner.get((d, r)) == s for d, r, s in zip(T.demo, T['round'], T.side)]
    T['plant'] = [RI.plant_tick.get((d, r)) == RI.plant_tick.get((d, r)) for d, r in zip(T.demo, T['round'])]
    T['start_tick'] = [int(RI.start_tick[(d, r)]) for d, r in zip(T.demo, T['round'])]
    T['end_tick'] = [int(RI.end_tick[(d, r)]) for d, r in zip(T.demo, T['round'])]
    T['reason'] = [RI.reason[(d, r)] for d, r in zip(T.demo, T['round'])]
    TI = T.set_index(['demo', 'round', 'side'])
    T['enemy_buy'] = [TI.buy.get((d, r, OPP[s])) for d, r, s in zip(T.demo, T['round'], T.side)]
    T['enemy_equip'] = [TI.equip.get((d, r, OPP[s])) for d, r, s in zip(T.demo, T['round'], T.side)]
    T['enemy_pre'] = [TI.pre.get((d, r, OPP[s])) for d, r, s in zip(T.demo, T['round'], T.side)]
    T['group'] = group
    last_round = R.groupby('demo')['round'].max().to_dict()

    # 上一回合（同一方；换边的 R13 不算延续）
    prev_res, prev_plant, prev_kills, prev_inf = [], [], [], []
    for d, r, s in zip(T.demo, T['round'], T.side):
        pr = r - 1
        if r in (1, 13) or (d, pr, s) not in TI.index:
            if r == 2 and (d, 1, s) not in TI.index:  # 缺 R1：用第二回合开局的钱推断手枪局胜负
                mine, theirs = TI.pre.get((d, r, s)), TI.pre.get((d, r, OPP[s]))
                prev_res.append('赢' if mine > theirs else '输'); prev_plant.append(None); prev_kills.append(None)
                prev_inf.append(True); continue
            prev_res.append(None); prev_plant.append(None); prev_kills.append(None); prev_inf.append(False); continue
        prev_res.append('赢' if TI.won[(d, pr, s)] else '输')
        prev_plant.append(bool(TI.plant[(d, pr, s)]))
        prev_kills.append(int(TI.kills[(d, pr, s)])); prev_inf.append(False)
    T['prev_result'] = prev_res; T['prev_plant'] = prev_plant; T['prev_kills'] = prev_kills; T['prev_inferred'] = prev_inf

    sit, sub, econ = [], [], []
    for row in T.itertuples():
        r = row.round
        if r in (1, 13):
            es = '手枪局'
        elif row.buy == '长枪':
            es = '长枪局' if row.enemy_buy == '长枪' else '反eco'
        else:
            es = row.buy
        if r in (1, 13):
            sit.append('手枪局'); sub.append('CT 手枪局' if row.side == 'CT' else 'T 手枪局')
        elif r in (2, 14):
            sit.append('第二回合')
            if row.prev_result == '赢':
                sub.append('反eco'); es = '反eco'
            else:
                c = _r2_choice(row.spent_ratio); sub.append(c); es = '强起' if c in ('强起', '半起') else 'eco'
        elif r in (3, 15):
            sit.append('第三回合')
            p1 = TI.won.get((row.demo, r - 2, row.side)); p2 = TI.won.get((row.demo, r - 1, row.side))
            if p1 is None:  # 缺 R1
                p1 = TI.pre.get((row.demo, r - 1, row.side), 0) > TI.pre.get((row.demo, r - 1, OPP[row.side]), 0)
            sub.append(f"手枪{'赢' if p1 else '输'}-R{r - 1}{'赢' if p2 else '输'}")
        elif r > 24:
            sit.append('加时'); sub.append('')
        else:
            sit.append('常规'); sub.append('')
        econ.append(es)
    T['situation'] = sit; T['sub'] = sub; T['econ_sit'] = econ
    T['econ_detail'] = T.econ_sit
    m = T.econ_sit.isin(['反eco', '强起', 'eco']) & ~T['round'].isin([2, 14])
    T.loc[m, 'econ_detail'] = T.loc[m, 'econ_sit'] + '·对方' + T.loc[m, 'enemy_buy'].astype(str)
    T['half_end'] = [(r in (12, 24)) or r == last_round[d] for d, r in zip(T.demo, T['round'])]

    # 用户所在一方（原脚本只给 groups.csv 里 user 组的 BiuBiuZ 算；这里按 ctx.user）
    U = B[B.steamid == str(user)].set_index(['demo', 'round'])
    T['user_team'] = [U.side.get((d, r)) == s for d, r, s in zip(T.demo, T['round'], T.side)]
    for c in ['k', 'd', 'dmg', 'death_tick', 'primary', 'equip']:
        T['user_' + c] = [U[c].get((d, r)) if ut else None for d, r, ut in zip(T.demo, T['round'], T.user_team)]
    T['scene'] = scene
    T = T.sort_values(['group', 'demo', 'round', 'side'])
    T['equip'] = T.equip.round(0); T['enemy_equip'] = T.enemy_equip.round(0); T['pre'] = T.pre.round(0)
    T['enemy_pre'] = T.enemy_pre.round(0); T['spent'] = T.spent.round(0); T['nades'] = T.nades.round(2)
    T[SIT_COLS].to_csv(out / 'sit.csv', index=False, encoding=ENC)


# =============================================================================== pro_prefire.py（这个用户每次阵亡的"标准位"）
PREFIRE_R = 150.0
PREFIRE_TOPK = 5
PREFIRE_CN = {'BombsiteA': 'A点', 'BombsiteB': 'B点', 'Connector': '拱门', 'Jungle': 'Jungle', 'SnipersNest': 'VIP',
              'Catwalk': 'B小', 'Apartments': 'B二楼', 'TRamp': 'A1', 'TopofMid': '中远', 'PalaceInterior': 'A二楼内',
              'PalaceAlley': 'A二楼外', 'Underpass': '下水道', 'CTSpawn': 'CT出生点', 'Stairs': 'A楼梯', 'Shop': 'B商店',
              'SideAlley': 'B小巷', 'Middle': '中路', 'TSpawn': 'T出生点', 'BackAlley': 'B后巷', 'House': 'B房',
              'Scaffolding': '脚手架', 'Truck': '小推车', 'Ladder': '梯子间', 'TicketBooth': '售票亭', 'Market': 'B市场'}
PREFIRE_REGION = {'BombsiteA': 'A点', 'TRamp': 'A1', 'PalaceInterior': 'A二楼', 'PalaceAlley': 'A二楼',
                  'Connector': '拱门', 'Jungle': 'Jungle', 'TopofMid': '中远', 'Middle': '中路', 'Catwalk': 'B小',
                  'BombsiteB': 'B点', 'SideAlley': 'B小巷', 'CTSpawn': 'CT出生点/A楼梯', 'Stairs': 'CT出生点/A楼梯',
                  'Apartments': 'B二楼', 'Shop': 'B商店', 'Market': 'B市场', 'Truck': '小推车', 'Underpass': '下水道',
                  'SnipersNest': 'VIP', 'Ladder': '梯子间'}
PREFIRE_CTX = {('T', '前期控图'): 'T前期控图', ('T', '进攻打点'): 'T进点', ('T', '下包后'): 'T下包后',
               ('CT', '回防'): 'CT回防', ('CT', '守点'): 'CT守点/补防', ('CT', '转点/补防'): 'CT守点/补防'}


def _standard_spots(did, user, out):
    """{回合: (是否常见开枪位, 说明)}：和 build_report.load_angles 读 prefire_ref.json user_deaths_all 得到的一样，
    只是用职业 + 高分局的聚类中心现算（pro_prefire.py：凶手离前 5 个簇任一中心 ≤R 算常见位）。
    注意（review-equal）：结果只写进 user_duels 的 标准位 / 凶手位置 和 timeline user_fights 的 standard_pos /
    killer_pos_note，目前没有规则、合并、总结或页面读它，为了和旧库 report/duels_de_mirage_*.csv 逐字一致先不改。
    以后要上页面之前必须先改两处：①复盘的是对标库里的 high 场时，聚类要像 pro_prefire.py 算覆盖率那样“留一场”去掉本场；
    ②说明文字改用 config.PLACE_CN（超市，不写 B商店），去掉“占 N%”和“该预瞄”（P6）。"""
    f = config.ANA / 'prefire_ref.json'
    if not f.exists():
        return {}
    ref = json.loads(f.read_text(encoding='utf-8'))
    contexts = ref.get('contexts', {})
    k = pd.read_csv(out / 'kills.csv', encoding=ENC, dtype={'demo': str, 'attacker_steamid': str, 'user_steamid': str})
    k = k[k.enemy_kill == True].copy()  # noqa: E712
    k['round'] = k['round'].astype(int)
    k = k[k.user_steamid == str(user)]
    d = pd.read_csv(out / 'duels.csv', encoding=ENC, dtype={'demo': str, 'steamid': str, '敌人ID': str})
    dl = d[d['结果'] == '输'][['demo', '回合', 'tick', 'steamid', '局势']]
    dl = dl.drop_duplicates(['demo', '回合', 'tick', 'steamid'])
    k = k.merge(dl, left_on=['demo', 'round', 'tick', 'user_steamid'], right_on=['demo', '回合', 'tick', 'steamid'], how='left')
    res = {}
    for row in k.sort_values('tick').itertuples():
        ctx = PREFIRE_CTX.get((row.victim_side, row.局势))
        reg = PREFIRE_REGION.get(row.victim_place)
        info = contexts.get(ctx, {}).get(reg) if ctx and isinstance(reg, str) else None
        if not info or not info.get('clusters'):
            continue
        cls = info['clusters'][:PREFIRE_TOPK]
        cs = np.asarray([c['center'] for c in cls], float)
        dd = np.sqrt(((cs - np.asarray([row.attacker_X, row.attacker_Y], float)) ** 2).sum(1))
        j = int(dd.argmin())
        std = bool(float(dd[j]) <= PREFIRE_R)
        cl = next((c for c in contexts[ctx][reg]['clusters'] if c.get('rank') == j + 1), None)
        if std and cl:
            text = f"常规位：凶手在{cl['place_cn']}，是{ctx}·{reg}时第 {cl['rank']} 常见的开枪位（占 {cl['share']:.0%}），这个角度该预瞄"
        else:
            kp = PREFIRE_CN.get(row.attacker_place, row.attacker_place)
            text = f"非常规位：凶手在{kp or '?'}，不是{ctx}·{reg}时常见的开枪位"
        res[int(row.round)] = (std, text.replace("T下包后", "T守包"))
    return res


# =============================================================================== build_report.py（一场、一个用户）
USER_DUEL_COLS = ['场次', '回合', '阵营', '结果', '开局第几秒', '我的报点', '敌人报点', '敌人', '武器', '爆头', 'tick', '我X', '我Y',
                  '敌X', '敌Y', '看见我的敌人数', '看见敌人时准星偏差°', '左右偏差°', '高低偏差°', '看见到结束秒', '谁先打中', '隔烟',
                  '我朝向夹角°', '敌朝向夹角°', '交火类型', '敌人ID', '开打tick', '对手刚杀过队友', '队友先看见对手', '备注', '局势',
                  '对枪方式', '开打血量', '被闪', '道具掉血', '我速度', '敌速度', '被队友闪', '我方存活', '敌方存活', '我的枪档',
                  '敌人枪档', '首杀对枪', '标准位', '凶手位置', '原因', '责任', '责任说明', '经济', '对方经济']


def _user_duels(ctx, loaded, user_df, sid, out, scene=1):
    header, deaths, hurts, fe, re_, ticks, extras = loaded
    mapname = header.get("map_name")
    if user_df is None or not len(user_df):
        pd.DataFrame(columns=USER_DUEL_COLS).to_csv(out / 'user_duels.csv', index=False, encoding=ENC)
        return 0
    df = user_df.copy()
    df.insert(0, "场次", scene)
    rounds = round_stats(deaths, re_, fe, ticks, sid, scene)
    df["我的报点"] = [fix_place(mapname, p_, x, y) for p_, x, y in zip(df["我的报点"], df["我X"], df["我Y"])]
    df["敌人报点"] = [fix_place(mapname, p_, x, y) for p_, x, y in zip(df["敌人报点"], df["敌X"], df["敌Y"])]
    open_ticks = {(r["d"], r["r"]): r["openTick"] for r in rounds}
    df["首杀对枪"] = [open_ticks.get((d_, r_)) == t_ for d_, r_, t_ in zip(df["场次"], df["回合"], df["tick"])]
    rd_map = {(r["d"], r["r"]): r for r in rounds}
    angles = _standard_spots(ctx.id, sid, out)
    ang = [angles.get(int(r_)) if res == "输" else None for r_, res in zip(df["回合"], df["结果"])]
    df["标准位"] = [a[0] if a else None for a in ang]
    df["凶手位置"] = [a[1] if a else None for a in ang]
    df["原因"] = [full_reason(r) if r["结果"] == "输" else None for _, r in df.iterrows()]
    bl = [blame(r, rd_map.get((r["场次"], r["回合"]))) if r["结果"] == "输" else (None, None) for _, r in df.iterrows()]
    df["责任"] = [b[0] for b in bl]
    df["责任说明"] = [b[1] for b in bl]
    df = relabel(df)
    df["经济"] = [rd_map.get((d_, r_), {}).get("econ") for d_, r_ in zip(df["场次"], df["回合"])]
    df["对方经济"] = [rd_map.get((d_, r_), {}).get("eecon") for d_, r_ in zip(df["场次"], df["回合"])]
    df.to_csv(out / 'user_duels.csv', index=False, encoding=ENC)
    return len(df)


# =============================================================================== 入口
def run(ctx):
    """只用 ctx.dem 生成 work/<id>/base/ 下全部底稿。返回一句中文小结。"""
    out = base_dir(ctx)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    p = DemoParser(str(ctx.dem))
    from .extract import check_demo
    check_demo(ctx, p)                  # 先查地图（只支持 Mirage）和用户在不在 demo 里，不对就直接报中文错误
    loaded = load(p)
    header = loaded[0]
    if header.get("map_name") not in config.SUPPORTED_MAPS:
        raise FactsError(f'目前只支持 Mirage（这个 demo 是 {header.get("map_name")}）')
    ticks = loaded[5]
    names = list_players(ticks)
    user_sid = str(ctx.user) if str(ctx.user) in set(names.index.astype(str)) else None
    group = group_of(ctx.id)
    t1 = time.perf_counter()
    user_df = _extract_pro(ctx, p, loaded, out, group, user_sid)
    t2 = time.perf_counter()
    _sit_parse(p, ctx.id, out)
    _sit_rounds(ctx.id, group, ctx.user, out)
    t3 = time.perf_counter()
    n_user = _user_duels(ctx, loaded, user_df, user_sid, out) if user_sid else _user_duels(ctx, loaded, None, None, out)
    t4 = time.perf_counter()
    print(f'底稿用时：读 demo {t1 - t0:.1f}s，回合/位置/击杀/道具/交火 {t2 - t1:.1f}s，经济局面 {t3 - t2:.1f}s，'
          f'用户交火 {t4 - t3:.1f}s', flush=True)
    who = names.get(int(user_sid)) if user_sid and int(user_sid) in names.index else (user_sid or ctx.user)
    note = '' if user_sid else f'（{ctx.user} 不在这个 demo 里）'
    # 窗口进度卡片上直接显示这句：只写玩家看得懂的（用时、文件名留在日志里）
    return f'{len(pd.read_csv(out / "rounds.csv"))} 回合，{who} 交火 {n_user} 次{note}'


if __name__ == '__main__':
    import argparse
    sys.stdout.reconfigure(encoding='utf-8')
    sys.path.insert(0, str(config.ROOT))
    from engine.ctx import make_ctx
    ap = argparse.ArgumentParser()
    ap.add_argument('demo')
    ap.add_argument('--user', default=config.DEFAULT_USER)
    a = ap.parse_args()
    print(run(make_ctx(a.demo, user=a.user)))
