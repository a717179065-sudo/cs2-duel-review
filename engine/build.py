"""
单场复盘页：把 work/<id> 里的逐回合复盘（review.json）、时间线（timeline.json）、整场数据（match_overview.json）、
2D 回放切片（clips.json）和 Mirage 地图轮廓拼成一页 report/<id>.html，另写一份 work/<id>/meta.json 给窗口的历史列表用。
由早期原型 build_match.py 参数化而来，页面数据格式不变。

pipeline 里调用 run(ctx)；单独调试：
    py -I engine/build.py <work 目录> <输出 html> [--id 8d9b0b8f] [--dem demo路径]
地图轮廓：engine/assets/mirage_cells.json（从 report/de_mirage_BiuBiuZ.html 里一次性取出的格子），不再依赖那份报告。
"""
import datetime
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TEMPLATE = HERE / "page_template.html"
CELLS = {"de_mirage": HERE / "assets" / "mirage_cells.json"}


def load_cells(map_name):
    """地图轮廓格子：{"grid": 格子边长, "cells": [[gx, gy], ...]}"""
    f = CELLS.get(map_name)
    if f is None:
        raise RuntimeError(f"没有 {map_name} 的地图轮廓，目前只支持 Mirage")
    d = json.loads(f.read_text(encoding="utf-8"))
    return {"grid": d["grid"], "cells": d["cells"]}


def load_clips(f):
    """2D 回放切片（clips.py 生成）；key 统一成 "回合-秒"，秒用最短写法，和页面里的 `${round}-${+t}` 对上"""
    if not f.exists():
        return {}
    clips = json.loads(f.read_text(encoding="utf-8"))
    return {f"{c['round']}-{float(c['t']):g}": c for c in clips.values()}


def compact_round(tl, me):
    """地图和标题要用的那一小部分时间线；me = 用户在本场的名字"""
    u = tl["econ"]["user"]
    death = tl.get("user_death")
    return {
        "round": tl["round"], "side": tl["side"], "score": tl["score_before"], "result": tl["result"],
        "reason": tl.get("reason_cn"), "dur": tl.get("dur"),
        "buy": {"ours": tl["econ"].get("our_buy"), "theirs": tl["econ"].get("enemy_buy"),
                "primary": u.get("primary"), "pistol": u.get("pistol"), "armor": u.get("armor"), "helmet": u.get("helmet"),
                "nades": u.get("nades") or [], "money": u.get("start_money")},
        "path": [[p["t"], p["X"], p["Y"]] for p in tl["user_path"]],
        "util": [[x["kind"], x["land_xy"][0], x["land_xy"][1], x["t"]] for x in tl["user_util"] if x.get("land_xy")],
        "kills": [[k["victim_xy"][0], k["victim_xy"][1], k["t"], k["victim"], k["weapon"]] for k in tl["kills"]
                  if k.get("killer_is_user", k["killer"] == me) and not k.get("teamkill") and not k.get("post_round")],
        "death": None if not death else {"t": death["t"], "tick": death["tick"], "xy": death["xy"], "place": death.get("place_cn"),
                                         "killer": death["killer"], "kxy": death["killer_xy"], "kplace": death.get("killer_place_cn"),
                                         "weapon": death["weapon"], "traded": death.get("traded")},
        "plant": None if not tl.get("plant") else {"t": tl["plant"]["t"], "site": tl["plant"]["site"], "xy": tl["plant"]["xy"]},
        "start_tick": tl["key_ticks"]["round_start"],
        "me": tl.get("user_summary", {}),
    }


def page_title(m):
    """浏览器标签上的标题：有场次号用场次号，没有就用 demo id"""
    if m.get("match_no") is not None:
        return f"{m['user']} 第 {m['match_no']} 场复盘"
    return f"{m['user']} {m.get('demo', '')} 复盘".replace("  ", " ")


def make_meta(rid, m, overview, dem=None):
    """给窗口历史列表的小摘要：id、地图、日期、比分、输赢、上下半场、用户"""
    my, op = (int(x) for x in str(m["final_score"]).split(":"))
    bs = overview.get("user", {}).get("by_side", {})
    meta = {"id": rid, "demo": m.get("demo"), "map": m["map"], "user": m["user"], "user_steamid": m.get("user_steamid"),
            "match_no": m.get("match_no"), "score": m["final_score"],
            "result": "赢" if my > op else "输" if my < op else "平",
            "start_side": m.get("user_start_side"), "rounds": m.get("rounds"),
            "halves": {s: {"won": bs[s]["won"], "lost": bs[s]["rounds"] - bs[s]["won"]} for s in sorted(bs) if bs[s]["rounds"]},
            "overtime": ({"won": ot["won"], "lost": ot["rounds"] - ot["won"]}
                         if (ot := overview.get("user", {}).get("overtime")) else None),
            "date": m.get("date"), "report": f"{rid}.html"}
    # 比赛日期 demo 里没有；退而求其次用 demo 文件的修改日期（同一个文件每次一样）
    if dem is not None and Path(dem).exists():
        meta["file_date"] = datetime.date.fromtimestamp(Path(dem).stat().st_mtime).isoformat()
    return meta


def build(work, out, rid=None, dem=None):
    """拼页面：读 work 里的 json，写 out（html）和 work/meta.json，返回 (回合数, meta)"""
    work, out = Path(work), Path(out)
    rid = rid or work.name
    review = json.loads((work / "review.json").read_text(encoding="utf-8"))
    timeline = json.loads((work / "timeline.json").read_text(encoding="utf-8"))
    overview = json.loads((work / "match_overview.json").read_text(encoding="utf-8"))
    tl_by = {r["round"]: r for r in timeline["rounds"]}
    m = timeline["meta"]
    rounds = [dict(rv, **{"tl": compact_round(tl_by[rv["round"]], m["user"])}) for rv in review["rounds"]]
    data = {"meta": {"match_no": m.get("match_no"), "map": m["map"], "user": m["user"], "score": m["final_score"],
                     "start_side": m["user_start_side"], "rounds": m["rounds"], "demo": m.get("demo")},
            "summary": review["summary"], "rounds": rounds,
            "user": overview["user"], "board": overview["scoreboard"],
            "map": load_cells(m["map"]),
            "clips": load_clips(work / "clips.json")}
    tpl = TEMPLATE.read_text(encoding="utf-8")
    # 用户的叫法：Shop / Market 一律叫"超市"（上游已经改过，这里再兜一次底）
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("B 商店", "超市").replace("B商店", "超市")
    title = page_title(m).replace("&", "&amp;").replace("<", "&lt;")
    html = tpl.replace("<title>__TITLE__</title>", f"<title>{title}</title>").replace("/*__DATA__*/null", blob)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    meta = make_meta(rid, m, overview, dem)
    (work / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    return len(rounds), meta


def report_path(rid):
    return ROOT / "report" / f"{rid}.html"


def run(ctx):
    """pipeline 阶段：生成页面 report/<key>.html（key：默认用户 = demo id，别的用户 = <id>_<steamid>）"""
    key = getattr(ctx, 'key', None) or ctx.id
    out = report_path(key)
    n, _ = build(ctx.work, out, key, ctx.dem)
    return f"{n} 个回合 → {out}（{out.stat().st_size // 1024} KB）"


def main():
    import argparse
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("out")
    ap.add_argument("--id")
    ap.add_argument("--dem")
    a = ap.parse_args()
    out = Path(a.out)
    n, _ = build(Path(a.work), out, a.id, a.dem)
    print(f"{n} 个回合 → {out}（{out.stat().st_size // 1024} KB）")


if __name__ == "__main__":
    main()
