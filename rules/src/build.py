import json, os, sys, re
sys.stdout.reconfigure(encoding="utf-8")
D = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")   # rules/
load = lambda n: json.load(open(os.path.join(D, n), encoding="utf-8"))

head = load("header.json")
tail = load("tail.json")
rules = []
for i in range(1, 7):
    rules += load(f"p{i}.json")

CATS = ["开枪与准星", "站位与拉枪", "团队配合", "道具", "时机与转点", "下包后·回防·保枪", "经济与势头", "合理代价判定", "全场汇总"]
rules.sort(key=lambda r: CATS.index(r["category"]))
ids = [r["id"] for r in rules]
assert len(ids) == len(set(ids)), "duplicate id"
FIELDS = ["id", "name", "kind", "category", "old_ids", "trigger", "data", "blame", "template", "alt", "bench",
          "fires_match3", "matches_review", "limits", "plain", "example", "review_cmp"]
for r in rules:
    assert set(r) == set(FIELDS), (r["id"], set(FIELDS) ^ set(r))
    # P6 check on player-facing example: no percent / degree / decimal coordinates
    assert "%" not in r["example"] and "°" not in r["example"], r["id"]
    # every rule id referenced in text must exist
    for ref in re.findall(r"\b(?:SHOT|POS|TEAM|UTIL|TIME|POST|ECO|COST|SUM)-(?:G)?\d+\b", " ".join(str(v) for k, v in r.items() if k not in ("old_ids", "matches_review", "review_cmp"))):
        if ref not in ids:
            print("WARN unknown ref", r["id"], ref)
for k, v in head["merge_layer"].items():
    for ref in re.findall(r"\b(?:SHOT|POS|TEAM|UTIL|TIME|POST|ECO|COST|SUM)-(?:G)?\d+\b", v):
        if ref not in ids:
            print("WARN merge ref", k, ref)

id_map = {}
for r in rules:
    for o in r["old_ids"]:
        id_map[o] = r["id"]
for d in head["deleted"]:
    id_map[d["id"]] = "已删除"
id_map["POS-NR"] = "并入“暂时没法规则化的判断”"
id_map["LIMIT-01"] = "并入合并层 M3/M4 和“暂时没法规则化的判断”"

doc = dict(head)
doc["note_impl"] = "rules_v1 写于原型阶段，当时设想由 AI 把事实连成句子；引擎实现后改为固定模板拼句，不调用任何 AI（见 engine/compose.py）。文中“第 3 场”指示例 8d9b0b8f。"
doc["n_rules"] = len(rules)
doc["rules"] = rules
doc["match3_deaths"] = tail["match3_deaths"]
doc["not_rulable"] = tail["not_rulable"]
doc["thresholds_to_decide"] = tail["thresholds_to_decide"]
doc["id_map"] = id_map
os.makedirs(OUT, exist_ok=True)
json.dump(doc, open(os.path.join(OUT, "rules_v1.json"), "w", encoding="utf-8", newline="\n"), ensure_ascii=False, indent=1)

# ---------------- markdown ----------------
L = []
w = L.append
cnt = {}
for r in rules:
    cnt[r["category"]] = cnt.get(r["category"], 0) + 1
w("# CS2 Mirage 复盘规则 v1（给 BiuBiuZ 审阅）")
w("")
w("> 注：rules_v1 写于原型阶段，当时设想由 AI 把事实连成句子；引擎实现后改为固定模板拼句，不调用任何 AI（见 engine/compose.py）。文中“第 3 场”指示例 8d9b0b8f。")
w("")
w("## 这份规则怎么用")
w("")
w("复盘分两步。第一步由脚本做：按下面每条规则，从 demo 里算出事实（谁在哪、谁先打中、身上还有什么道具、人数几打几），判断这条规则有没有命中、责任算谁，并且从规则里取出固定的“还可以…”。同一个 demo 不管跑多少次、交给哪个 AI，算出来的结论都一样。第二步才交给 AI：AI 只负责把脚本给出的事实和判断连成通俗的话，不能自己下新结论、不能改责任，也不能自己编建议。")
w("")
w(f"规则一共 {len(rules)} 条，由评审后的 86 条合并而来：去掉 2 条已删除的、2 条说明性条目，把 14 处重复（同一件事被两三个脚本各说一遍）合成一条，统一了编号、报点和用词。好的操作的编号带 G（例如 SHOT-G1）。每条都在第 3 场（23 回合）上核对过，并和之前多轮人工修正的复盘（review.json）逐条对比。")
w("")
w("每条规则下面写了：什么时候算命中（通俗说法）、责任算谁、用第 3 场真实命中填好的固定句式、“还可以…”、第 3 场命中了哪些回合（格式：回合-秒）、和人工复盘的对比、局限。完整的触发条件、数据来源和对标数字在 `rules/rules_v1.json` 里。")
w("")
w("各类条数：" + "，".join(f"{c} {cnt[c]} 条" for c in CATS if c in cnt) + "。")
w("")
w("### 判断时遵守的原则")
w("")
for p in head["principles"]:
    w(f"- {p}")
w("")
w("### 所有规则共用的口径")
w("")
for k, v in head["common"].items():
    w(f"- **{k}**：{v}")
w("")
w("### 多条规则命中同一件事时怎么合并")
w("")
for k, v in head["merge_layer"].items():
    w(f"- **{k}**：{v}")
w("")
w("## 规则")
w("")
for ci, c in enumerate(CATS, 1):
    rs = [r for r in rules if r["category"] == c]
    if not rs:
        continue
    w(f"### {ci}. {c}")
    w("")
    for r in rs:
        merged = "；由 " + "、".join(r["old_ids"]) + " 合并" if len(r["old_ids"]) > 1 else ("；原编号 " + r["old_ids"][0] if r["old_ids"][0] != r["id"] else "")
        w(f"#### {r['id']} · {r['name']}")
        w("")
        w(f"*{r['kind']}{merged}*")
        w("")
        w(f"- **什么时候算命中**：{r['plain']}")
        w(f"- **责任**：{r['blame']}")
        w(f"- **固定句式示例**：{r['example']}")
        w(f"- **还可以…**：{r['alt']}")
        fires = "；".join(r["fires_match3"]) if r["fires_match3"] else "没有命中"
        w(f"- **第 3 场命中**：{fires}")
        w(f"- **和人工复盘对比**：{r['review_cmp']}")
        w(f"- **局限**：{r['limits']}")
        w("")
w("## 第 3 场每次阵亡最后算在哪条规则上")
w("")
w("按合并层 M1 的顺序，每次阵亡只取一条做主因，其他命中写成补充。第 3 场你一共阵亡 16 次：")
w("")
w("| 阵亡（凶手，凶手位置） | 主因规则 | 这次死的责任 | 补充 |")
w("|---|---|---|---|")
for d in tail["match3_deaths"]:
    w(f"| {d['death']} | {d['main']} | {d['blame']} | {d['also']} |")
w("")
w("合计：你的责任 11 次（其中 R14、R18 是小问题），合理代价 3 次（其中 R12 带小问题），和队友分担 2 次（R19 队友为主、R20 各占一份）。")
w("")
w("## 暂时没法规则化的判断")
w("")
w("下面这些是人工复盘里有、但规则现在判断不了的内容，大多缺的是地图几何（墙、高度、掩体、点位）或声音数据。规则里不会出现这些判断，AI 讲解时也不能自己补。")
w("")
for i, n in enumerate(tail["not_rulable"], 1):
    w(f"{i}. **{n['topic']}**")
    w(f"   - 人工复盘里的例子：{n['review']}")
    w(f"   - 为什么现在做不了：{n['why']}")
    w(f"   - 以后怎么补：{n['fix']}")
w("")
w("## 需要你拍板的阈值")
w("")
w("下面这些数字是我们按第 3 场和对标数据先定的，没有把握，需要你凭经验定。改了以后脚本重跑，结论会跟着变。")
w("")
w("| # | 要定的事 | 现在用的值 | 影响的规则 | 说明 |")
w("|---|---|---|---|---|")
for i, t in enumerate(tail["thresholds_to_decide"], 1):
    w(f"| {i} | {t['item']} | {t['now']} | {t['rules']} | {t['note']} |")
w("")
w("## 旧编号对照")
w("")
w("只列编号变了或合并了的；没列出的（例如 SHOT-01、POS-02、ECO-01）编号不变。")
w("")
for r in rules:
    olds = [o for o in r["old_ids"] if o != r["id"]]
    if olds:
        w(f"- {r['id']} ← " + "、".join(olds))
w("- 已删除：POS-09、POS-10；POS-NR 并入“暂时没法规则化的判断”；LIMIT-01 并入合并层 M3/M4 和“暂时没法规则化的判断”")
w("")
md = "\n".join(L)
open(os.path.join(OUT, "rules_v1.md"), "w", encoding="utf-8", newline="\n").write(md)
print("rules", len(rules), "md chars", len(md))
