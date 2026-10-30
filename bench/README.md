# bench/ · 对标库（只读，已匿名化）

引擎运行时读的对标数据。来源：11 场 FACEIT 高分局 Mirage、12 场职业比赛 Mirage（demo id 就是对阵，例如 `vitality-vs-9z`），以及默认用户自己的其他几场。原始 demo 不随仓库发布，这里只留引擎真正要读的字段。

| 路径 | 谁读 | 内容 |
|---|---|---|
| `data/groups.csv` | facts/base.py、facts/shooting.py、facts.other_reviews | 每个 demo 属于哪一组：high / pro / user |
| `data/duels_<demo>.csv` | facts/shooting.py（开枪动作对标）、facts.other_reviews | 每次交火一行：回合、双方报点、谁先打中、对枪方式、局势…… |
| `data/positions_<demo>.csv.gz` | facts/extract.py（坐标 → 报点名的参考格子） | 每 0.5 秒每人一行，只留 `X,Y,Z,place` 四列 |
| `sit_rounds.csv` | facts/economy.py（买法胜率对标） | 每回合每方的买法、局势、输赢 |
| `prefire_ref.json` | facts/base.py（常见开枪位聚类） | 只留 `contexts`（各局面下凶手开枪位的聚类中心） |
| `shots_cache/<demo>.fire.parquet`、`<demo>.hurt.parquet`、`<demo>_active.json` | facts/shooting.py | 对标 demo 的 weapon_fire / player_hurt 事件和"开打时手里的武器"缓存，省得每次重新解析 demo。用 parquet / JSON 存，不含 pickle；`_active.json` 每行 `[tick, steamid, 武器, 是否开镜]`，武器为 `null` 表示不知道 |

匿名化规则（生成这份数据时一次性做完，同一个人在所有文件里一致，所以各表之间还能按 steamid 对上）：

- 玩家名：高分局 → `高分玩家NN`，职业 → `职业选手NN`；默认用户自己比赛里的其他人 → `队友A–D` / `对手1–5`（每场内部一致）。默认用户本人保留。
- steamid：换成假的 17 位数字 `9000000000000NNNN`（保持原来的大小顺序），默认用户本人保留。
- 去掉了原始缓存里的 demo 文件路径，以及引擎不读的列和表。

已知限制：

- 因为 steamid 是假的，引擎"对标里去掉被复盘的人"和"用户其他场"只对默认用户（以及不在对标库里的玩家）生效；复盘库里的某位高分玩家或职业选手时，他在库里其他场的数据会留在对标统计里，"其他场"那一组也不会出现。
- `groups.csv` 里默认用户的 `demo1` 有交火表和位置表，但没有开枪事件缓存（原始 demo 早就没有了），所以开枪对标那一步每次会提示"找不到 demo1 的 demo 文件，跳过"。这和开发时的输出一致，不影响其他场。

换一份对标库：设环境变量 `CS2_REVIEW_BENCH=<目录>`，目录结构和这里一样即可。
对标缓存缺条目（例如对标 demo 换了版本）时，引擎会去 `pro/cache/1-<id>*.dem` 或 `pro/hltv/<id>.dem` 找原 demo 补算；找不到就跳过那一条，不会报错。
