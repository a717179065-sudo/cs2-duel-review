# -*- coding: utf-8 -*-
"""复盘引擎的全局配置：目录、默认用户、demo id、报点中文名、阈值读取。

所有路径都从这里取，模块里不要写死绝对路径；ROOT 是仓库根目录（engine/ 的上一级）。
对标数据目录默认是 <仓库>/bench，可以用环境变量 CS2_REVIEW_BENCH 换到别处。
"""
import hashlib
import json
import os
import re
from pathlib import Path

# ---------------------------------------------------------------- 目录
ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / 'engine'
WORK = ROOT / 'work'                      # 每个 demo 的中间结果：WORK / <id>
REPORT = ROOT / 'report'                  # 产出页面 report/<id>.html；用户 demo 在 report/cache
RULES_DIR = ROOT / 'rules'                # rules_v1.json / decisions_v1.md（只读）
# rules_v1.json 只读：审核单（手册 v1）新增的规则编号放在这里，merge / validate / compose 当作 rules_v1 的一部分。
# after = 排在哪条规则后面（rule_idx 用）
EXTRA_RULES = [
    {'id': 'SHOT-06', 'after': 'SHOT-05', 'name': '对枪动作',
     'plain': '你的每一次被枪打死都按对枪动作给一个结论：动作对、枪没中就是枪法；动作有错写错在哪一步（审核单第 2 条，手册 1.13）'},
]
THRESHOLDS = ENGINE / 'thresholds'        # 阈值 *.json（按文件名合并进 ctx.th）
# 对标库（只读，已匿名化）：高分局 11 场 + 职业 12 场 + 默认用户自己的其他场，说明见 bench/README.md
BENCH = Path(os.environ.get('CS2_REVIEW_BENCH') or ROOT / 'bench')
DATA = BENCH / 'data'                     # groups.csv、duels_<demo>.csv、positions_<demo>.csv
ANA = BENCH                               # sit_rounds.csv、prefire_ref.json
BENCH_CACHE = BENCH / 'shots_cache'       # 对标 demo 的开枪事件缓存 <demo>.fire/.hurt.parquet、<demo>_active.json
# 可选：本地放的对标 demo 原文件（pro/cache/1-*.dem、pro/hltv/<demo>.dem，不随仓库发布）。
# 只有对标缓存缺条目时才会去解析，没有就跳过。
PRO = ROOT / 'pro'

# ---------------------------------------------------------------- 用户 / demo
DEFAULT_USER = '76561199042148360'        # BiuBiuZ
DEFAULT_USER_NAME = 'BiuBiuZ'
# 默认用户的旧多场报告里这几场是“第几场”（页面标题“BiuBiuZ 第 3 场复盘”、窗口历史用）。原来每次从
# report/duels_de_mirage_BiuBiuZ.csv 按击杀 tick 反查，现在写死在这里，页面标签不再依赖那份旧库文件
LEGACY_MATCH_NO = {'51920faa': 5, '8d9b0b8f': 3, 'a32a2106': 2, 'bd125285': 4}
TICKRATE = 64
SUPPORTED_MAPS = ('de_mirage',)
COMPRESSED = ('.gz', '.zst', '.bz2')      # 支持的压缩 demo 后缀

_FACEIT = re.compile(r'^1-([0-9a-f]{8})-', re.I)


def demo_id(path):
    """demo 的 id：FACEIT 文件名 1-<8位hex>-....dem(.zst) 取那 8 位；其他文件取文件内容 sha1 前 8 位。"""
    path = Path(path)
    m = _FACEIT.match(path.name)
    if m:
        return m.group(1).lower()
    digest = _sha1(path)
    # 改过名的 FACEIT demo：和 report/cache、pro/cache 里大小一样、内容一样的 demo 用同一个 id
    # （对标库按这个 id 存，"对标里排除本场"要靠它认出来）
    size = path.stat().st_size
    known_all = sorted((REPORT / 'cache').glob('1-*.dem')) + sorted((PRO / 'cache').glob('1-*.dem'))
    for known in known_all:
        km = _FACEIT.match(known.name)
        try:
            if km and known.resolve() != path.resolve() and known.stat().st_size == size and _sha1(known) == digest:
                return km.group(1).lower()
        except OSError:
            continue
    return digest[:8]


def output_key(did, user):
    """一次复盘的输出名（work/<key>/、report/<key>.html）：默认用户就是 demo id（旧链接不变），
    别的用户加 _<steamid>，同一个 demo 换个人复盘不会覆盖。"""
    user = str(user)
    return did if user == DEFAULT_USER else f'{did}_{user}'


def _sha1(path):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 报点中文名（rules_v1 common C-PLACE）
# C-PLACE 原文：Shop=超市（不再写"B 商店"），Scaffolding=跳台，PalaceAlley、House 去掉英文括号。
# 不在 C-PLACE 列表里的：PalaceInterior 和 Palace 同为 A 二楼；SideAlley 沿用各原型的 B 小巷；
# Market 是 Shop 的另一种叫法；TSpawn=T 出生点。
PLACE_CN = {
    'TopofMid': '中远', 'Connector': '拱门', 'SnipersNest': 'VIP', 'Apartments': 'B 二楼', 'Palace': 'A 二楼',
    'PalaceInterior': 'A 二楼', 'TRamp': 'A1', 'Jungle': 'Jungle', 'Shop': '超市', 'Market': '超市',
    'Underpass': '下水道', 'Truck': '小推车', 'CTSpawn': 'CT 出生点', 'Stairs': 'A 楼梯', 'BackAlley': 'B 后巷',
    'Middle': '中路', 'Scaffolding': '跳台', 'Catwalk': 'B 小', 'Ladder': '梯子间', 'BombsiteA': 'A 点',
    'BombsiteB': 'B 点', 'SideAlley': 'B 小巷', 'TSpawn': 'T 出生点', 'PalaceAlley': 'A 二楼外巷',
    'House': 'T 方房区',
}


def place_cn(place, default=None):
    """last_place_name → 中文报点；空值返回 default，没收录的原样返回。"""
    if place is None or (isinstance(place, float) and place != place) or place == '':
        return default
    return PLACE_CN.get(place, place)


# ---------------------------------------------------------------- 半场 / 加时
# 常规每半场 half_rounds 回合（MR12），加时每半场 ot_half_rounds 回合（MR3）；值放在 thresholds/common.json，
# 这里只给默认值。事实层（economy）和规则层（common.half）都用这一套。
HALF_ROUNDS = 12
OT_HALF_ROUNDS = 3


def half_of(r, hr=HALF_ROUNDS, ot=OT_HALF_ROUNDS):
    """第几个半场（0 上半场、1 下半场、2 起是加时的每个半场）"""
    r = int(r)
    if r <= 2 * hr:
        return (r - 1) // hr
    return 2 + (r - 2 * hr - 1) // ot


def half_end(r, hr=HALF_ROUNDS, ot=OT_HALF_ROUNDS):
    """r 所在半场按赛制的最后一回合"""
    h = half_of(r, hr, ot)
    return hr * (h + 1) if h < 2 else 2 * hr + ot * (h - 1)


def half_spans(n_rounds, hr=HALF_ROUNDS, ot=OT_HALF_ROUNDS):
    """1..n_rounds 按半场切成 [(第一回合, 最后一回合)]（最后一段截到 n_rounds）"""
    out, r = [], 1
    while r <= n_rounds:
        e = min(half_end(r, hr, ot), n_rounds)
        out.append((r, e))
        r = e + 1
    return out


def halves_th(th):
    """从 ctx.th 取 (half_rounds, ot_half_rounds)"""
    c = (th or {}).get('common', {})
    return int(c.get('half_rounds', HALF_ROUNDS)), int(c.get('ot_half_rounds', OT_HALF_ROUNDS))


# ---------------------------------------------------------------- 阈值
def load_thresholds(folder=THRESHOLDS):
    """把 thresholds/*.json 全部读进来，按文件名（不含 .json）分组：th['common']、th['shot']……"""
    th = {}
    folder = Path(folder)
    if folder.exists():
        for f in sorted(folder.glob('*.json')):
            th[f.stem] = json.loads(f.read_text(encoding='utf-8'))
    return th
