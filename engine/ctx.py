# -*- coding: utf-8 -*-
"""Ctx：一次复盘运行的上下文（demo 路径、id、用户、工作目录、阈值、带缓存的读取）。

用法：
    from engine.ctx import make_ctx
    ctx = make_ctx(r'report/cache/1-8d9b0b8f-....dem')        # 也可以是 .dem.zst/.gz/.bz2
    tl = ctx.json('timeline.json')          # 读 work/<id>/timeline.json（内存缓存，同一个 ctx 只读一次）
    T = ctx.cache('ticks')                  # 读 work/<id>/cache/ticks.pkl
    ctx.th['common']['...']                 # thresholds/common.json

---------------------------------------------------------------- 可用数据（API 一览）
work/<key>/ 下（key = config.output_key：默认用户就是 demo id，别的用户 <id>_<steamid>；ctx.json(name) 读 .json；其他用 ctx.path(name)）：
  base/*.csv           facts/base.py      这一场的底稿（回合、位置、击杀、道具、交火、经济局面、用户交火），只从本 demo 算
  timeline.json        facts/extract.py   逐回合时间线（原 pro/analysis/match3/timeline.json 同格式）
  match_overview.json  facts/extract.py   每回合一行 + 用户整场数据 + 双方计分板 + data_issues
  match_overview.csv   facts/extract.py   同上的回合表（utf-8-sig）
  economy.json         facts/economy.py   经济层：rules（实测经济规则）/ bench（高分、职业对标）/ rounds
  economy_out.txt      facts/economy.py   经济层打印版
  shooting.json        facts/shooting.py  开枪动作：by_class / by_mode（本场 user_m3 / 用户其他场 user_other /
                                          high / pro）+ match3_duels（本场逐条；键名沿用原格式，"m3" = 本场）
  shooting_out.txt     facts/shooting.py  开枪层打印版
  _shots_all.csv       facts/shooting.py  所有交火第一枪明细（本场 + 对标）
  match.dem            ctx.make_ctx       只有输入是压缩包时才有：解压后的 demo

work/<id>/cache/ 下（facts/cache.py 生成；内容和格式与原型读的 rules_proto/_cache 等完全一样，只是文件名去掉 id）：
  名字          文件               原型里的文件                                  生成脚本 → 读它的原型
  ticks         ticks.pkl          _cache/ticks_8d9b0b8f.pkl                    _parse_ticks.py → 开枪与准星.py
  sight         sight.pkl          _cache/sight_m3.pkl                          _sight_cache.py（目前没有原型读）
  sight_s4      sight_s4.pkl       _cache/sight_8d9b0b8f_s4.pkl                 站位与视线.parse_demo(step=4) → 站位与视线.py
  daoju         daoju.pkl          _cache/daoju_m3.pkl                          _cache_build.py → 道具.py
  pp_ticks      pp_ticks.pkl       _cache/pp_m3_ticks.pkl                       _parse_下包后_回防_保枪.py → 下包后_回防_保枪.py
  ticks8        ticks8.csv         _cache/ticks8.csv                            _cache/parse_cache.py → 好的操作.py、时机与转点.py
  ev_<事件>     ev_<事件>.csv      _cache/ev_<事件>.csv（12 个事件，见 cache.py）  _cache/parse_cache.py → 同上
  grid          grid.parquet       _work/grid_8d9b0b8f.parquet                  团队配合._parse → 团队配合.py
  team_ev       team_ev.pkl        _work/ev_8d9b0b8f.pkl                        团队配合._parse → 团队配合.py
  shots_ev      shots_ev.pkl       match3/_shots_cache/8d9b0b8f.pkl             shots_match3.load_events → facts/shooting.py
  shots_active  shots_active.pkl   match3/_shots_cache/8d9b0b8f_active.pkl      shots_match3.active_at → facts/shooting.py
  ctx.cache('ticks8', dtype=..., keep_default_na=False) 这类 csv 的读法参数照原型传；
  ctx.cache('grid') 返回 DataFrame（read_parquet）；.pkl 用 pickle.load。
"""
import bz2
import gzip
import json
import pickle
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import config


@dataclass
class Ctx:
    dem: Path                    # 实际解析的 .dem（压缩包会先解压到 work/<id>/match.dem）
    id: str                      # demo id（config.demo_id）
    user: str                    # 用户 steamid（字符串）
    work: Path                   # work/<key>
    force: bool = False          # True：忽略已有产出，全部重算
    src: Path = None             # 用户给的原始路径（可能是压缩包）
    key: str = None              # 输出名（config.output_key）：默认用户 = id，别的用户 = <id>_<steamid>；report/<key>.html
    th: dict = field(default_factory=config.load_thresholds)
    _json: dict = field(default_factory=dict, repr=False)
    _cache: dict = field(default_factory=dict, repr=False)

    # ---------- 路径
    def path(self, name):
        """work/<id>/<name>"""
        return self.work / name

    def cache_path(self, name):
        """work/<id>/cache/<name>；不带后缀时依次找 .pkl / .parquet / .csv（都没有就返回 .pkl 路径）。"""
        d = self.work / 'cache'
        if Path(name).suffix:
            return d / name
        for ext in ('.pkl', '.parquet', '.csv'):
            if (d / (name + ext)).exists():
                return d / (name + ext)
        return d / (name + '.pkl')

    # ---------- 读取（带内存缓存）
    def json(self, name):
        """读 work/<id>/<name>（不带后缀时补 .json），同一个 ctx 只读一次。"""
        if not Path(name).suffix:
            name += '.json'
        if name not in self._json:
            with open(self.work / name, encoding='utf-8') as f:
                self._json[name] = json.load(f)
        return self._json[name]

    def cache(self, name, **read_kw):
        """读 work/<id>/cache/<name>：.pkl → pickle.load，.parquet → read_parquet，.csv → read_csv(**read_kw)。
        不带读法参数时结果缓存在内存里（调用方不要原地改它）。"""
        key = name if not read_kw else None
        if key is not None and key in self._cache:
            return self._cache[key]
        p = self.cache_path(name)
        if p.suffix == '.pkl':
            with open(p, 'rb') as f:
                obj = pickle.load(f)
        else:
            import pandas as pd
            obj = pd.read_parquet(p) if p.suffix == '.parquet' else pd.read_csv(p, **read_kw)
        if key is not None:
            self._cache[key] = obj
        return obj

    def forget(self, name=None):
        """清掉内存缓存（某个阶段重写了文件之后调用）。"""
        if name is None:
            self._json.clear(); self._cache.clear()
        else:
            self._json.pop(name, None); self._json.pop(name + '.json', None); self._cache.pop(name, None)


def _decompress(src, dst):
    """把 .zst/.gz/.bz2 解压到 dst（先写 .part 再改名，中途失败不会留下半个文件）。"""
    tmp = dst.with_suffix('.part')
    suf = src.suffix.lower()
    with open(src, 'rb') as fi, open(tmp, 'wb') as fo:
        if suf == '.zst':
            import zstandard
            zstandard.ZstdDecompressor().copy_stream(fi, fo)
        elif suf == '.gz':
            with gzip.GzipFile(fileobj=fi) as g:
                shutil.copyfileobj(g, fo, 1 << 20)
        elif suf == '.bz2':
            with bz2.BZ2File(fi) as b:
                shutil.copyfileobj(b, fo, 1 << 20)
        else:
            raise ValueError(f'不认识的压缩格式：{src.name}')
    tmp.replace(dst)


def make_ctx(dem, user=config.DEFAULT_USER, force=False, work_root=config.WORK):
    """根据 demo 路径建 Ctx：算 id 和输出名 key、建 work/<key>/、压缩包解压成 work/<id>/match.dem（解压过就复用，
    同一个 demo 不同用户共用这一份）。"""
    src = Path(dem)
    if not src.exists():
        raise FileNotFoundError(f'找不到 demo 文件：{src}')
    did = config.demo_id(src)
    key = config.output_key(did, user)
    work = Path(work_root) / key
    (work / 'cache').mkdir(parents=True, exist_ok=True)
    real = src
    if src.suffix.lower() in config.COMPRESSED:
        shared = Path(work_root) / did
        shared.mkdir(parents=True, exist_ok=True)
        real = shared / 'match.dem'
        if force or not real.exists() or real.stat().st_mtime < src.stat().st_mtime:
            _decompress(src, real)
    return Ctx(dem=real, id=did, user=str(user), work=work, force=force, src=src, key=key)
