"""序列设计器纯计算 —— provider 无关。

只做三件事,全部纯函数(无设备、无 IO):
  - estimate_duration(card/clip, overhead)  估时长 + breakdown(公式见设计 4.3)
  - compile_project(project)                压缩成单轨 IR(算法见设计 5)
  - twilight(date, lat, lon)                逐分钟扫太阳高度求日落/昏影/晨光/日出

时长公式与 breakdown 字段是「前后端共享契约」:前端镜像同一套公式,
后端在 /api/designer/estimate /compile 权威重算。Sim 与 Live 网关都直接
委托本模块(provider 无关),因此估时/压缩结果在两种模式下完全一致。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from gateway import models as m
from gateway.sim import astro


# --------------------------------------------------------------------------- #
# 深合并(clip.overrides 覆盖卡片)—— 只覆盖填写的字段
# --------------------------------------------------------------------------- #
def _deep_merge(base: dict, over: dict) -> dict:
    """over 的字段覆盖 base;dict 递归合并;其它(含 list)整体替换。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def effective_card(card: m.Card, overrides: Optional[dict]) -> m.Card:
    """卡片 + clip 实例覆盖 → 生效卡片(不改原卡)。"""
    if not overrides:
        return card
    merged = _deep_merge(card.model_dump(), overrides)
    return m.Card.model_validate(merged)


# --------------------------------------------------------------------------- #
# 估时(公式见设计 4.3)
# --------------------------------------------------------------------------- #
def _ceil_div(a: float, b: float) -> int:
    if b <= 0:
        return 0
    return int(math.ceil(a / b))


def estimate_duration(card: m.Card, overhead: m.Overhead) -> m.EstimateResult:
    """单张(生效)卡片的估时 + 分解。kind 分派。

    返回 EstimateResult{duration_s, breakdown}。duration_s = breakdown 各机时项之和
    (含 wait_s,纯等待虽不占机时但占时间轴长度)。
    """
    b = m.EstimateBreakdown()
    o = overhead

    if card.kind == "capture":
        is_light = (card.image_type or "LIGHT").upper() == "LIGHT"
        if is_light:
            b.startup_s = o.startup_overhead_s
        total_exposure_s = 0.0
        for ex in card.exposures:
            cnt = max(0, int(ex.count))
            b.frames += cnt
            b.exposure_s += cnt * ex.exposure_s
            b.readout_s += cnt * o.readout_download_s
            total_exposure_s += cnt * ex.exposure_s
            # 每个 SmartExposure(每滤镜行)切一次滤镜;校准帧单滤镜也按一次算
            b.filter_switch_s += o.filter_switch_s
            # 抖动:仅 LIGHT 且开启;每 dither_every 张一次
            if is_light and card.dither_every and card.dither_every > 0:
                b.dither_s += (cnt // card.dither_every) * o.dither_s
        # 时间触发自动对焦:按总曝光分钟 / 间隔向上取整
        if is_light and card.af_interval_min and card.af_interval_min > 0:
            n_af = _ceil_div(total_exposure_s / 60.0, card.af_interval_min)
            b.autofocus_s = n_af * o.autofocus_s
        # 中天翻转:此处不知窗口是否跨子午,按卡片标记当 0/1 次(前端可据 astro 决定是否传)
        if is_light and card.meridian_flip:
            b.meridian_flip_s = o.meridian_flip_s

    elif card.kind == "startup":
        c = card.checks or {}
        if c.get("unpark"):
            b.overhead_s += o.unpark_s
        if c.get("cool_camera"):
            b.overhead_s += float(c.get("cool_duration_min", 0.0)) * 60.0
        if c.get("initial_autofocus"):
            b.overhead_s += o.autofocus_s
        # WaitForTime(到黄昏/钟点)是时间轴定位,不计机时

    elif card.kind == "teardown":
        c = card.checks or {}
        park = o.park_s if c.get("park") else 0.0
        warm = float(c.get("warm_duration_min", 0.0)) * 60.0 if c.get("warm_camera") else 0.0
        if card.parallel:
            b.overhead_s += max(park, warm)
        else:
            b.overhead_s += park + warm

    elif card.kind == "op":
        op = card.op or ""
        p = card.params or {}
        if op == "autofocus":
            b.overhead_s += o.autofocus_s
        elif op == "center":
            b.overhead_s += o.park_s            # 居中(plate-solve)经验同 park 量级
        elif op == "switch_filter":
            b.overhead_s += o.filter_switch_s
        elif op == "recalibrate_guiding":
            b.overhead_s += o.startup_overhead_s   # 重校导星约一个起手量级
        elif op in ("park", "find_home"):
            b.overhead_s += o.park_s
        elif op == "unpark":
            b.overhead_s += o.unpark_s
        elif op in ("cool", "warm"):
            b.overhead_s += float(p.get("duration_min", 0.0)) * 60.0
        elif op == "wait":
            if p.get("wait_clock"):
                b.wait_s += 0.0                  # 绝对钟点等待长度由时间轴定位决定,不计机时
            else:
                b.wait_s += float(p.get("wait_min", 0.0)) * 60.0
        elif op == "set_tracking":
            b.overhead_s += 0.0

    duration = (b.startup_s + b.exposure_s + b.readout_s + b.dither_s
                + b.filter_switch_s + b.autofocus_s + b.meridian_flip_s
                + b.overhead_s + b.wait_s)
    return m.EstimateResult(duration_s=round(duration, 1), breakdown=b)


def estimate_clip(project: m.Project, clip: m.Clip) -> m.EstimateResult:
    """时间轴 clip 的估时:取其引用卡片 + overrides 后过公式。"""
    card = _find_card(project, clip.card_id)
    if card is None:
        return m.EstimateResult()
    return estimate_duration(effective_card(card, clip.overrides), project.overhead)


def _find_card(project: m.Project, card_id: str) -> Optional[m.Card]:
    for c in project.cards:
        if c.id == card_id:
            return c
    return None


# --------------------------------------------------------------------------- #
# 暮光(逐分钟扫太阳高度)
# --------------------------------------------------------------------------- #
def _origin_tz(date: str, lon: float) -> timezone:
    """无显式时区时,用经度估算本地时区(经度/15 小时取整),供 ISO 输出带偏移。"""
    hours = round(lon / 15.0)
    hours = max(-12, min(14, hours))
    return timezone(timedelta(hours=hours))


def _find_crossing(lat: float, lon: float, start: datetime, hours: float,
                   target_alt: float, going_down: bool) -> Optional[datetime]:
    """从 start 起逐分钟扫描,找太阳高度穿越 target_alt 的时刻。
    going_down=True 找由上而下穿越(日落/入夜);False 找由下而上(日出/出夜)。
    命中后对该分钟区间线性插值到秒。无穿越返回 None。"""
    step = timedelta(minutes=1)
    n = int(hours * 60)
    prev_t = start
    prev_a = astro.sun_altitude(lat, lon, start)
    for i in range(1, n + 1):
        t = start + step * i
        a = astro.sun_altitude(lat, lon, t)
        crossed = (prev_a > target_alt >= a) if going_down else (prev_a < target_alt <= a)
        if crossed:
            span = (a - prev_a)
            frac = (target_alt - prev_a) / span if span != 0 else 0.0
            return prev_t + step * frac
        prev_t, prev_a = t, a
    return None


def twilight(date: str, lat: float, lon: float) -> m.TwilightResult:
    """求当晚 日落 / 天文昏影Dusk / 天文晨光Dawn / 次日日出。

    扫描窗口:本地当日 12:00 → 次日 12:00(覆盖整夜)。
    日落/日出 target_alt=-0.833°(含大气折射+日面半径);昏影/晨光=-18°(天文暮光)。
    """
    tz = _origin_tz(date, lon)
    try:
        y, mo, d = (int(x) for x in date.split("-"))
        base = datetime(y, mo, d, 12, 0, 0, tzinfo=tz)
    except (ValueError, AttributeError):
        base = datetime.now(tz).replace(hour=12, minute=0, second=0, microsecond=0)

    sun_alt = -0.833
    astro_alt = -18.0

    def _iso(dt: Optional[datetime]) -> Optional[str]:
        return dt.astimezone(tz).isoformat(timespec="seconds") if dt else None

    sunset = _find_crossing(lat, lon, base, 24.0, sun_alt, going_down=True)
    sunrise = _find_crossing(lat, lon, base, 24.0, sun_alt, going_down=False)
    dusk = _find_crossing(lat, lon, base, 24.0, astro_alt, going_down=True)
    dawn = _find_crossing(lat, lon, base, 24.0, astro_alt, going_down=False)
    return m.TwilightResult(date=date, sunset=_iso(sunset), dusk=_iso(dusk),
                            dawn=_iso(dawn), sunrise=_iso(sunrise))


# --------------------------------------------------------------------------- #
# 压缩成单轨(算法见设计 5)
# --------------------------------------------------------------------------- #
SNAP_S = 8.0          # 吸附阈值(秒);压缩侧用绝对秒,前端按 pxPerSec 换算


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """容错解析 ISO 时刻:兼容 JS toISOString() 的 'Z' 后缀
    (Python 3.9 的 datetime.fromisoformat 不认 'Z',会抛 ValueError)。"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _origin_dt(project: m.Project) -> Optional[datetime]:
    return _parse_iso(project.timeline_origin_iso)


def _resolve_start_s(project: m.Project, clip: m.Clip, origin: Optional[datetime]) -> float:
    """解析 clip 绝对开始秒(相对 timeline_origin)。
    snap/gap → 用已排定 start_s;clock/dusk/dawn → 用 astro 解析成相对秒。"""
    anc = clip.anchor or "snap"
    if anc in ("snap", "gap"):
        return clip.start_s
    if origin is None:
        return clip.start_s
    if anc == "clock" and clip.anchor_clock:
        ck = clip.anchor_clock
        target = origin.replace(hour=int(ck.get("h", 0)), minute=int(ck.get("m", 0)),
                                second=int(ck.get("s", 0)), microsecond=0)
        if target < origin:                 # 跨夜:钟点在零点之后的次日
            target += timedelta(days=1)
        return (target - origin).total_seconds()
    if anc in ("dusk", "dawn"):
        tw = twilight(project.date, project.site.lat, project.site.lon)
        ref_iso = tw.dusk if anc == "dusk" else tw.dawn
        if ref_iso:
            ref = datetime.fromisoformat(ref_iso) + timedelta(minutes=clip.anchor_offset_min)
            return (ref - origin).total_seconds()
    return clip.start_s


def _section_of(kind: str) -> str:
    if kind in ("startup", "teardown"):
        return kind
    if kind == "op":
        return "op"
    return "capture"


def compile_project(project: m.Project) -> m.CompiledSequence:
    """多轨归并 → 单轨有序 IR + 重叠检测 + 间隔等待项 + totals。"""
    origin = _origin_dt(project)

    # 1) 收集未 muted 轨的所有 clip,解析绝对 start_s + 估时 duration
    entries: list[dict] = []
    for tr in project.tracks:
        if tr.muted:
            continue
        for clip in tr.clips:
            card = _find_card(project, clip.card_id)
            if card is None:
                continue
            est = estimate_duration(effective_card(card, clip.overrides), project.overhead)
            start = _resolve_start_s(project, clip, origin)
            entries.append({
                "clip": clip, "card": card, "track_order": tr.order,
                "start": start, "dur": est.duration_s, "est": est,
                "eff": effective_card(card, clip.overrides),
            })

    # 2) 按 (绝对 start, track.order) 升序
    entries.sort(key=lambda e: (e["start"], e["track_order"]))

    # 3) 重叠检测(相邻)
    overlaps: list[m.CompiledOverlap] = []
    for i in range(1, len(entries)):
        prev, cur = entries[i - 1], entries[i]
        prev_end = prev["start"] + prev["dur"]
        if cur["start"] < prev_end - SNAP_S:        # 吸附容差内不算重叠
            overlaps.append(m.CompiledOverlap(
                clip_a=prev["clip"].id, clip_b=cur["clip"].id,
                at_s=round(cur["start"], 1),
                overlap_s=round(prev_end - cur["start"], 1)))

    if overlaps:
        return m.CompiledSequence(ok=False, project_id=project.id, overlaps=overlaps,
                                  items=[], totals=m.CompiledTotals())

    # 4) 无重叠:生成单轨 items,相邻间隔插 _wait
    items: list[m.CompiledItem] = []
    frames = 0
    light_s = 0.0
    prev_end: Optional[float] = None
    for e in entries:
        clip, card, eff, est = e["clip"], e["card"], e["eff"], e["est"]
        start = e["start"]
        # 间隔 → _wait(吸附阈值内不插)
        if prev_end is not None:
            gap = start - prev_end
            if gap > SNAP_S:
                items.append(m.CompiledItem(
                    clip_id=f"wait_before_{clip.id}", card_kind="_wait", section="capture",
                    label="等待", start_s=round(prev_end, 1), duration_s=round(gap, 1),
                    anchor=clip.anchor, wait_kind=_wait_kind(clip), wait_s=round(gap, 1)))
        items.append(m.CompiledItem(
            clip_id=clip.id, card_id=card.id, card_kind=card.kind, label=card.label,
            start_s=round(start, 1), duration_s=round(e["dur"], 1),
            section=_section_of(card.kind), anchor=clip.anchor,
            anchor_offset_min=clip.anchor_offset_min,
            resolved=_resolve_card(eff)))
        frames += est.breakdown.frames
        light_s += est.breakdown.exposure_s
        prev_end = start + e["dur"]

    # 5) totals
    totals = m.CompiledTotals(frames=frames, light_s=round(light_s, 1))
    if items:
        first_start = items[0].start_s
        last_end = items[-1].start_s + items[-1].duration_s
        totals.wall_clock_s = round(last_end - first_start, 1)
        if origin is not None:
            starts = origin + timedelta(seconds=first_start)
            ends = origin + timedelta(seconds=last_end)
            totals.starts_at_iso = starts.isoformat(timespec="seconds")
            totals.ends_at_iso = ends.isoformat(timespec="seconds")
            night_end = _parse_iso(project.timeline_end_iso)
            if night_end is not None:
                totals.fits_in_night = ends <= night_end

    return m.CompiledSequence(ok=True, project_id=project.id, overlaps=[],
                              items=items, totals=totals)


def _wait_kind(clip: m.Clip) -> str:
    anc = clip.anchor or "gap"
    if anc in ("clock", "dusk", "dawn"):
        return anc
    return "timespan"


def _resolve_card(card: m.Card) -> dict[str, Any]:
    """卡片 → 预览/编译用的解析字典(provider 无关,不拼 NINA $type)。"""
    if card.kind == "capture":
        return {
            "image_type": card.image_type,
            "target": card.target.model_dump() if card.target else None,
            "exposures": [ex.model_dump() for ex in card.exposures],
            "dither_every": card.dither_every,
            "af_interval_min": card.af_interval_min,
            "af_on_temp_delta": card.af_on_temp_delta,
            "meridian_flip": card.meridian_flip,
        }
    if card.kind in ("startup", "teardown"):
        return {"checks": card.checks, "parallel": card.parallel}
    if card.kind == "op":
        return {"op": card.op, "params": card.params}
    return {}
