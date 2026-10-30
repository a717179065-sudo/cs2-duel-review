"""
给复盘页的每个关键时刻切一段 2D 回放：时刻前 8 秒到后 2 秒，所有人的位置、朝向、存活，以及开枪、命中、击杀、道具。
由 pro/analysis/match3/extract_clips.py 参数化而来，输出格式不变。

pipeline 里调用 run(ctx)；单独调试：
    py -I engine/clips.py <work 目录> <demo 路径> [--user STEAMID]
输入: <work>/review.json（关键时刻的秒数）、<work>/timeline.json（回合开始 tick）、原始 demo
输出: <work>/clips.json  {"round-秒": {"round", "t", "t0", "dt", "players": [...], "frames": [[[x, y, yaw, alive, hp], ...], ...], "events": [...]}}
"""
import json
import sys
from pathlib import Path

TICK = 64
BEFORE, AFTER, STEP = 8.0, 2.0, 8          # 秒、秒、每隔几 tick 取一帧（8 tick = 1/8 秒）
SMOKE_S, FIRE_S = 18.0, 7.0
# 开枪事件里不算"开枪"的武器（刀、道具、包）
NOT_GUN = "knife|grenade|flashbang|molotov|incgrenade|decoy|smoke|c4"


def windows(review, timeline):
    """每个关键时刻的切片窗口：(key, 回合, 秒, 起始 tick, 结束 tick)，按 review 里的顺序"""
    start = {r["round"]: r["key_ticks"]["round_start"] for r in timeline["rounds"]}
    tick = timeline.get("meta", {}).get("tickrate") or TICK
    wins = []
    for r in review["rounds"]:
        for m in r["moments"]:
            if m["t"] < 0:
                continue                      # 买枪阶段的时刻没有可回放的画面
            t0 = start[r["round"]] + int((m["t"] - BEFORE) * tick)
            t1 = start[r["round"]] + int((m["t"] + AFTER) * tick)
            wins.append((f'{r["round"]}-{m["t"]}', r["round"], m["t"], t0, t1))
    return wins, tick


def extract(dem, user, work):
    """切片主体：读 work 里的 review/timeline，解析 demo，写 work/clips.json，返回段数"""
    import pandas as pd
    from demoparser2 import DemoParser

    work = Path(work)
    review = json.loads((work / "review.json").read_text(encoding="utf-8"))
    timeline = json.loads((work / "timeline.json").read_text(encoding="utf-8"))
    wins, tick = windows(review, timeline)
    out = {}
    if wins:
        ticks = sorted({t for _, _, _, a, b in wins for t in range(a, b + 1, STEP)})
        p = DemoParser(str(dem))
        df = p.parse_ticks(["X", "Y", "yaw", "is_alive", "health", "team_num"], ticks=ticks)
        df["steamid"] = df["steamid"].astype(str)

        def ev(name, **kw):
            e = p.parse_event(name, **kw)
            return e if isinstance(e, pd.DataFrame) else pd.DataFrame()
        fire = ev("weapon_fire")
        hurt = ev("player_hurt")
        death = ev("player_death")
        smoke = ev("smokegrenade_detonate")
        fireb = ev("inferno_startburn")
        flash = ev("flashbang_detonate")
        he = ev("hegrenade_detonate")
        for d in (fire, hurt, death, smoke, fireb, flash, he):
            for c in ("user_steamid", "attacker_steamid"):
                if c in d:
                    d[c] = d[c].astype(str)

        for key, rnd, t, a, b in wins:
            w = df[(df.tick >= a) & (df.tick <= b)]
            first = w[w.tick == w.tick.min()]
            players = [{"id": s, "name": n, "side": "CT" if tn == 3 else "T", "me": s == str(user)}
                       for s, n, tn in first[["steamid", "name", "team_num"]].itertuples(index=False)]
            order = [pl["id"] for pl in players]
            frames = []
            for tk, g in w.groupby("tick"):
                g = g.set_index("steamid")
                frames.append([[int(g.at[s, "X"]), int(g.at[s, "Y"]), int(g.at[s, "yaw"]), int(bool(g.at[s, "is_alive"])), int(g.at[s, "health"])]
                               if s in g.index else None for s in order])
            sec = lambda tk, a=a: round((tk - a) / tick, 2)   # 相对切片开头的秒数
            events = []
            if len(fire):
                f = fire[(fire.tick >= a) & (fire.tick <= b) & ~fire.weapon.str.contains(NOT_GUN, case=False, na=False)]
                events += [{"k": "shot", "s": sec(x.tick), "p": x.user_steamid} for x in f.itertuples()]
            if len(hurt):
                h = hurt[(hurt.tick >= a) & (hurt.tick <= b) & (hurt.attacker_steamid != "nan")]
                events += [{"k": "hit", "s": sec(x.tick), "a": x.attacker_steamid, "v": x.user_steamid, "d": int(x.dmg_health)} for x in h.itertuples()]
            if len(death):
                d = death[(death.tick >= a) & (death.tick <= b)]
                events += [{"k": "kill", "s": sec(x.tick), "a": x.attacker_steamid, "v": x.user_steamid, "w": x.weapon, "hs": bool(x.headshot)} for x in d.itertuples()]
            for frame, kind, dur in ((smoke, "smoke", SMOKE_S), (fireb, "fire", FIRE_S), (flash, "flash", 0.4), (he, "he", 0.4)):
                if not len(frame):
                    continue
                g = frame[(frame.tick >= a - int(dur * tick)) & (frame.tick <= b)]
                events += [{"k": kind, "s": sec(x.tick), "e": round(dur, 1), "x": int(x.x), "y": int(x.y)} for x in g.itertuples()]
            # 同一秒的事件保持上面拼接的顺序（sorted 是稳定排序）
            out[key] = {"round": rnd, "t": t, "t0": round(t - BEFORE, 2), "dt": STEP / tick, "players": players, "frames": frames,
                        "events": sorted(events, key=lambda e: e["s"])}
    (work / "clips.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(out)


def run(ctx):
    """pipeline 阶段：切 2D 回放"""
    n = extract(ctx.dem, ctx.user, ctx.work)
    return f"{n} 段回放"


def main():
    import argparse
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("dem")
    ap.add_argument("--user", default=None, help="steamid，默认 engine/config.py 的 DEFAULT_USER")
    a = ap.parse_args()
    if a.user is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from engine.config import DEFAULT_USER
        a.user = DEFAULT_USER
    n = extract(Path(a.dem), a.user, Path(a.work))
    f = Path(a.work) / "clips.json"
    print(f"{n} 段回放，{f.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
