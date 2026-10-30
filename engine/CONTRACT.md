# 复盘引擎约定（所有模块共同遵守）

> 本文写于整理成仓库之前：文中 `pro/data/`、`pro/analysis/` 现在是 `bench/data/`、`bench/`；`pro/cache`、`pro/hltv`、`report/cache` 是本地放 demo 的目录，不随仓库发布。

目标：把一个 Mirage demo 丢进窗口，固定流程跑完，产出复盘页。同一个 demo 每次跑出来结论和文字完全一样（确定性），不依赖任何 AI。

## 依据（按优先级）

1. `rules/decisions_v1.md`：用户拍板，覆盖 rules_v1 里对应的内容。
2. `rules/rules_v1.json` 和 `rules_v1.md`：68 条规则。
   - 每条规则的 trigger、blame、template、alt、fires_match3（第 3 场的期望命中，作测试基准）。
   - common：C-TIME、C-PLACE、C-ALIVE、C-STILL、C-HIT、C-TARGET、C-SEEN、C-RADAR、C-SMOKE、C-HEAR、C-FIGHT、C-ECO、C-GUN。
   - merge_layer：M1–M7。
   - principles：P1–P6。
   - thresholds_to_decide：没在 decisions 里改的，用"现在用的值"。
3. 原型代码 `pro/analysis/match3/rules_proto/*.py`（早期原型，不随仓库发布）：按类别写的，已在第 3 场跑过。
   - 原型依赖 `_cache/`（`_parse_ticks.py`、`_sight_cache.py`、`_cache_build.py` 生成）。
   - 原型也读 `timeline.json`、`economy.json`、`shooting.json`。
4. 早期提取脚本 `pro/analysis/match3/`（不随仓库发布，引擎里的 facts/ 由它们参数化而来）：
   - `extract_match3.py` → `timeline.json`、`match_overview.json`
   - `econ_match3.py` → `economy.json`
   - `shots_match3.py` → `shooting.json`
   - `extract_clips.py` → `clips.json`
   - `build_match.py` + `match_template.html` → 页面
5. 用户写作规则：作者自己整理的复盘写作要求，内容已在 rules_v1 principles 里。要点：
   - 中文报点：超市，不写 B 商店；VIP；B 小；B 二楼；A1；中远；拱门……
   - 不写坐标、速度数字、角度、百分比。
   - 不用"对拉/对架"。
   - 不写语音/报点建议。
   - 不引用教学/视频，不说教（应该/必须）；"还可以…"给多一个思路。
   - 只用玩家当时能知道的信息判断，但页面上不写"你当时不知道/雷达上能看到/他只剩 X 血"。
   - 可见性和补枪一律 3D + spotted，不能只用平面距离。
   - 不重复 K/D/A、ADR，不和队友对手比数字。

## 目录

```
cs2-duel-review/
  bench/                  对标库（匿名化）：groups.csv、duels_*、positions_*、sit_rounds.csv、prefire_ref.json、shots_cache/
  run.py                  命令行入口：py -I run.py <demo路径> [--user <steamid>] [--force]
  engine\
    CONTRACT.md           本文件
    config.py             路径、默认用户、读 thresholds.json、place 中文名
    thresholds\           全部阈值（rules_v1 现用值 + decisions 覆盖），规则代码不得硬编码阈值
      common.json         C-* 口径用的（common.py 负责）
      <类别>.json          每个规则模块自己的（shot.json、pos.json……，各模块负责自己那份）
    ctx.py                Ctx：一次运行的上下文（见下）
    pipeline.py           STAGES 列表 + run(ctx, progress_cb)；每步有缓存，--force 重跑
    facts\                从 demo 算事实（由现有提取脚本参数化而来，输出格式不变）
      base.py             → work/<key>/base/*.csv：这一场的底稿（回合、位置、击杀、道具、交火、经济局面、用户交火），
                            只从本 demo 算；以前要靠批量脚本事先"入库"的表，现在每场自己生成
      extract.py          → work/<id>/timeline.json、match_overview.json
      economy.py          → economy.json
      shooting.py         → shooting.json
      cache.py            → work/<id>/cache/（原型用的 ticks/sight 等 pickle，格式不变）
    common.py             C-* 口径的唯一实现（原型里重复的实现合并到这里）
    rules\
      __init__.py         ALL = [shot, pos, team, util, timing, post, eco]；run_all(ctx) -> list[Hit]
      shot.py  pos.py  team.py  util.py  timing.py  post.py  eco.py   （eco 含 COST-01）
    merge.py              M1–M7：hits → 每回合定稿的 moments / 主因 / 你的部分
    compose.py            → work/<id>/review.json（schema 与现有 pro/analysis/match3/review.json 完全一致）
    summary.py            全场：headline、momentum、weaknesses、strengths（SUM-01/02/03、ECO-06..09、TIME-09 等）
    validate.py           检查 review.json 所有文字（见下），不通过就报错退出
    clips.py              → clips.json（extract_clips.py 参数化）
    build.py              → report/<id>.html（build_match.py 参数化；地图底图 cells 存到 engine/assets/mirage_cells.json）
    assets\
  app\
    server.py             放 demo 的窗口：本地 http 服务 127.0.0.1:8770
    index.html
  启动复盘窗口.bat         双击启动 server 并打开浏览器
  work\<key>\             每次复盘的中间结果（key 见下）
  report\<key>.html       产出页面
```

- `<id>`：FACEIT 文件名 `1-<8位hex>-....dem` 取那 8 位；否则取文件 sha1 前 8 位（改过名的 report/cache、pro/cache 里的 demo 认回原 id）。
- `<key>`（config.output_key）：默认用户就是 `<id>`；别的用户 `<id>_<steamid>`，同一个 demo 换人复盘不会覆盖。
- 任何 Mirage demo 都能直接复盘：这一场的数据全部由 base 阶段从 demo 算出。其他比赛的对标数据（pro/data/*、groups.csv、
  sit_rounds.csv 的 high/pro 行、thresholds 里的对标数）是只读的静态参考，用的时候排除本场（本场也在对标库里时）和用户本人（对标库 steamid 已匿名，只对默认用户和不在库里的人生效）。
- "用户其他场" = 静态库（groups.csv 所有组的 pro/data/duels_<id>.csv）里有同一个 steamid 的其他场，去掉本场（没有就不写）。
  不扫 work/，所以 shooting.json 不随“之前复盘过哪些场”变化。user_other 只进 shooting.json / shooting_out.txt，规则和总结不读它。
- 页面标题的“第 N 场”只给默认用户的 4 场旧 demo（config.LEGACY_MATCH_NO），不再读 report/duels_de_mirage_*.csv。
- 半场：常规 MR12、加时每半场 MR3（thresholds/common.json 的 half_rounds / ot_half_rounds；config.half_of / common.half）。
- 第 3 场 = `8d9b0b8f`。
- demo 文件：自己放在 `report/cache/*.dem`（不随仓库发布）；对标库里的 11 场 FACEIT 高分局 demo 放进 `pro/cache/*.dem` 后也能当新 demo 复盘（换 --user）。
- 只支持 Mirage：base 一开始调用 extract.check_demo，其他地图在第一步就报"目前只支持 Mirage"。
- 用户不在 demo 里：报错，并列出 demo 里的玩家名和 steamid。
- 默认用户在 config.DEFAULT_USER（BiuBiuZ）；窗口和 run.py 都从那里取。

## Ctx

```python
@dataclass
class Ctx:
    dem: Path; id: str; user: str; work: Path; force: bool = False; key: str   # work = work/<key>
    def json(self, name) -> dict      # 读 work/<id>/<name>，带内存缓存
    def cache(self, name) -> object   # 读 work/<id>/cache/<name>.pkl
    th: dict                          # thresholds/*.json 合并：th['common'][...]、th['shot'][...]
```

规则模块只通过 ctx 读数据，不得写死 demo 路径、8d9b0b8f、用户名或第 3 场的回合号。

## Hit（每条规则命中输出一个）

```python
{
  "rule": "SHOT-01",              # rules_v1 的 id
  "kind": "问题" | "好" | "判定" | "势头" | "思路",
  "round": 3, "t": 44.0, "tick": 12345 | None,
  "scope": "moment" | "round" | "match",
  "subject": "BiuBiuZ",           # 主角（犯错或打得好的人）
  "is_user": True,                # subject 是不是用户
  "blame": "你的责任" | "小问题" | "队友或团队" | "合理代价" | "你的责任 + 队友或团队" | "—",
  "death_t": 44.4 | None,         # 这条和哪次阵亡有关（M1 用）
  "death_of": "BiuBiuZ" | None,
  "merge_key": "util:123" | None, # M5：同一颗道具/同一串连杀/同一次转点
  "facts": {...},                 # 填句子用到的全部值（秒、报点中文、人名、武器、钱数、人数）
  "text": "第 44 秒在……",          # 按 rules_v1 template 填好的句子
  "alt": "还可以……" | "",         # 只取规则 alt，按阵营/地点选分支
  "kb": [],
  "evidence": "……",               # 一句话证据，同样遵守 P6
}
```

- `blame` 只能用上面这几个值。
- `kind = 好` 的 blame 为 "—"。
- `facts` 里的每个数字和人名都必须能在 demo 数据里找到。validate 会反查：`text`、`evidence`、`alt` 里出现的秒数、人名、钱数、人数，都必须在 facts 里。

## review.json

- 字段和现有 `pro/analysis/match3/review.json` 完全一致，match_template.html 不改也能渲染。
- 每个 moment 多一个字段 `rule`（规则 id），moment.kind 用现有取值。
- 好操作 moment 可以多一个可选字段 `also`（改进建议，页面标“还可以：”），`better` 只放“为什么好”。match_template.html 不认 `also`，忽略即可。
- 所有文字由模板拼出来，不调用 AI。

## validate.py 检查项

1. P6 用词：
   - 不出现：坐标式数字、"速度"加数字、"°"、"度"（角度）、"%"、对拉、对架、B 商店、英文报点名；
   - 不出现：教学、视频、应该、必须、上帝视角、你当时不知道、雷达上能看到、残血、只剩 X 血（对方血量）；
   - 不出现：报点、喊、约好、沟通、交流（作建议时）。
2. 人名都在本场玩家名单里。
3. "第 N 秒"在本回合的 hit facts 里有对应（±0.5 秒）。
4. "$钱数"在 facts 或 economy 里。
5. "NvM"人数和 C-ALIVE 一致。
6. 每个 moment 都有 rule，并且这个 rule 在 rules_v1 里。

不通过时打印回合、字段、原文和原因，返回非零退出码。

## 确定性

- 不用 random 和当前时间。
- 排序都显式写，遍历 dict 时先 sorted。
- 同一个 demo 跑两次，review.json 的字节完全一样。

## pipeline 阶段名（窗口显示进度用）

| 阶段 | 显示名 |
|---|---|
| base | 读取比赛数据 |
| extract | 解析 demo |
| cache | 准备帧数据 |
| economy | 经济 |
| shooting | 开枪 |
| rules | 跑规则 |
| compose | 写复盘 |
| validate | 检查文字 |
| clips | 切 2D 回放 |
| build | 生成页面 |
