"""nina_seq.py — 把 CompiledSequence(IR)编译成 NINA 高级序列 JSON(Newtonsoft 全套)。

【只读】仅生成 + 自检,不下发、不启动。所有 $type / 字段 / 默认值 派生自 130apo 真实序列
(nina_seq_templates.json,由 高级序列20260526.json 抽取),禁止手敲 $type 后缀。

核心机制(全部以真样本实测为准):
- 每个带 $type 的对象都分配字符串 $id(含集合包装 / provider / 坐标 / Filter 等),
  唯独 Strategy(SequentialStrategy/ParallelStrategy)不带 $id。
- 集合 Items/Conditions/Triggers/ExposureInfoList = {$id,$type(ObservableCollection),$values:[...]}。
- Parent:Items/Conditions/Triggers 的子节点 → 所属容器 $id 的 {$ref};
  TriggerRunner.Parent = null;Root.Parent = null;值对象(Target/坐标/Filter…)无 Parent。
- 根必须是 SequenceRootContainer,Items 恰 3 项:StartArea / TargetArea / EndArea。
"""
import copy
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from . import models as m

_TPL_PATH = os.path.join(os.path.dirname(__file__), "nina_seq_templates.json")
with open(_TPL_PATH, encoding="utf-8") as _f:
    _TPL = json.load(_f)

_PROTO: Dict[str, Any] = _TPL["node_protos"]
_COLL: Dict[str, str] = _TPL["collection_types"]
_PROVIDERS: List[str] = _TPL["providers"]
_ID_EXEMPT = set(_TPL["id_exempt"])                       # {SequentialStrategy, ParallelStrategy}
_PROV = {p.split(",")[0].split(".")[-1]: p for p in _PROVIDERS}   # 短名 -> 完整 $type

VALID_IMAGE_TYPES = {"LIGHT", "DARK", "FLAT", "BIAS", "SNAPSHOT"}


def _short(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    return node.get("$type", "").split(",")[0].split(".")[-1]


def _clone(name: str) -> Dict[str, Any]:
    """克隆某类型的真实原型(深拷贝)。原型自带正确 $type / 默认字段 / 空集合壳。"""
    if name not in _PROTO:
        raise KeyError(f"nina_seq: 未知原型 {name!r}(不在真样本类型表里)")
    return copy.deepcopy(_PROTO[name])


def _provider(short: str) -> Dict[str, str]:
    if short not in _PROV:
        raise KeyError(f"nina_seq: 未知 DateTimeProvider {short!r}")
    return {"$type": _PROV[short]}


# --------------------------------------------------------------------------- #
# 坐标:十进制 → 六十进制分量(Dec 负号只靠 NegativeDec,DMS 取绝对值)
# --------------------------------------------------------------------------- #
def _ra_hms(ra_hours: float) -> Tuple[int, int, float]:
    ra = float(ra_hours) % 24.0
    h = int(ra)
    mf = (ra - h) * 60.0
    mn = int(mf)
    sec = round((mf - mn) * 60.0, 5)
    return h, mn, sec


def _dec_dms(dec_deg: float) -> Tuple[bool, int, int, float]:
    neg = float(dec_deg) < 0
    a = abs(float(dec_deg))
    d = int(a)
    mf = (a - d) * 60.0
    mn = int(mf)
    sec = round((mf - mn) * 60.0, 5)
    return neg, d, mn, sec


def _input_coords(ra_hours: float, dec_deg: float) -> Dict[str, Any]:
    c = _clone("InputCoordinates")
    h, mn, s = _ra_hms(ra_hours)
    c["RAHours"], c["RAMinutes"], c["RASeconds"] = h, mn, s
    neg, dd, dm, ds = _dec_dms(dec_deg)
    c["NegativeDec"], c["DecDegrees"], c["DecMinutes"], c["DecSeconds"] = neg, dd, dm, ds
    return c


def _binning(b: Any) -> Dict[str, Any]:
    bm = _clone("BinningMode")
    try:
        n = int(b or 1)
    except (TypeError, ValueError):
        n = 1
    bm["X"], bm["Y"] = n, n
    return bm


def _filter_info(name: str, filters: Optional[List[dict]]) -> Dict[str, Any]:
    """构造 FilterInfo:克隆真实原型(保留 _focusOffset/FlatWizard 等字段),覆盖 _name/_position。
    position 优先从滤镜配置取;取不到则保留原型值(下发前应改从 NINA 滤镜配置回填)。"""
    fi = _clone("FilterInfo")
    if name:
        fi["_name"] = name
    if filters:
        for f in filters:
            if name and (f.get("name") == name or f.get("_name") == name):
                pos = f.get("position", f.get("_position"))
                if pos is not None:
                    fi["_position"] = int(pos)
                break
    return fi


# --------------------------------------------------------------------------- #
# 卡片 → NINA 节点
# --------------------------------------------------------------------------- #
def _smart_exposure(ex: dict, dither_every: int, image_type: str,
                    filters: Optional[List[dict]]) -> Dict[str, Any]:
    """一个滤镜行 → SmartExposure(LoopCondition + DitherAfterExposures + SwitchFilter + TakeExposure)。
    关抖动(dither_every<=0)仍【保留】DitherAfterExposures 并置 AfterExposures=0
    —— 省略会让 start 校验 NPE(GetDitherAfterExposures 解引用 null)。"""
    se = _clone("SmartExposure")
    se["Name"] = "智能曝光"

    lc = _clone("LoopCondition")
    lc["Iterations"] = int(ex.get("count", 1) or 0)
    lc["CompletedIterations"] = 0
    se["Conditions"]["$values"] = [lc]

    dither = _clone("DitherAfterExposures")
    dither["AfterExposures"] = int(dither_every or 0)
    se["Triggers"]["$values"] = [dither]

    sw = _clone("SwitchFilter")
    sw["Filter"] = _filter_info(ex.get("filter", "") or "", filters)

    te = _clone("TakeExposure")
    te["ExposureTime"] = float(ex.get("exposure_s", 0) or 0)
    te["Gain"] = int(ex.get("gain", -1))
    te["Offset"] = int(ex.get("offset", -1))
    te["Binning"] = _binning(ex.get("bin", 1))
    te["ImageType"] = image_type
    te["ExposureCount"] = 0                       # 张数由 LoopCondition.Iterations 决定

    se["Items"]["$values"] = [sw, te]
    return se


def _capture_light(r: dict, filters: Optional[List[dict]]) -> Dict[str, Any]:
    tgt = r.get("target") or {}
    ra = tgt.get("ra_hours", 0) or 0
    dec = tgt.get("dec_degrees", 0) or 0

    dso = _clone("DeepSkyObjectContainer")
    dso["Name"] = tgt.get("name") or "目标"
    dso["Target"]["TargetName"] = tgt.get("name", "") or ""
    dso["Target"]["PositionAngle"] = float(tgt.get("rotation", 0) or 0)
    dso["Target"]["InputCoordinates"] = _input_coords(ra, dec)
    dso["ExposureInfoList"]["$values"] = []

    # Starting:解锁 → 对中(继承本目标坐标)→ 对焦 → 导星
    starting = _clone("SequentialContainer")
    starting["Name"] = "Starting"
    center = _clone("Center")
    center["Inherited"] = True
    center["Coordinates"] = _input_coords(ra, dec)
    starting["Items"]["$values"] = [
        _clone("UnparkScope"), center, _clone("RunAutofocus"), _clone("StartGuiding"),
    ]

    # Exposure:每滤镜行一个 SmartExposure;容器触发按需挂
    exposure = _clone("SequentialContainer")
    exposure["Name"] = "Exposure"
    trigs: List[Dict[str, Any]] = []
    if r.get("meridian_flip"):
        trigs.append(_clone("MeridianFlipTrigger"))
    if float(r.get("af_interval_min", 0) or 0) > 0:
        t = _clone("AutofocusAfterTimeTrigger")
        t["Amount"] = float(r["af_interval_min"])
        trigs.append(t)
    if float(r.get("af_on_temp_delta", 0) or 0) > 0:
        t = _clone("AutofocusAfterTemperatureChangeTrigger")
        t["Amount"] = float(r["af_on_temp_delta"])
        trigs.append(t)
    exposure["Triggers"]["$values"] = trigs
    dither_every = int(r.get("dither_every", 0) or 0)
    exposure["Items"]["$values"] = [
        _smart_exposure(ex, dither_every, "LIGHT", filters)
        for ex in (r.get("exposures") or [])
    ]

    endding = _clone("SequentialContainer")
    endding["Name"] = "Endding"
    endding["Items"]["$values"] = [_clone("StopGuiding")]

    dso["Items"]["$values"] = [starting, exposure, endding]
    return dso


def _capture_calibration(r: dict, filters: Optional[List[dict]]) -> Dict[str, Any]:
    """暗/平/偏置场:裸 TakeExposure 包 SequentialContainer + LoopCondition(无 Target/Center/Guiding/Dither)。
    【未在真样本中取样,形态系源码推定;下发前须在 NINA UI 目检】。"""
    image_type = r.get("image_type", "DARK")
    rows = r.get("exposures") or [{}]

    def _row(ex: dict) -> Dict[str, Any]:
        sc = _clone("SequentialContainer")
        sc["Name"] = f"{image_type} {ex.get('filter','') or ''}".strip()
        lc = _clone("LoopCondition")
        lc["Iterations"] = int(ex.get("count", 1) or 0)
        lc["CompletedIterations"] = 0
        sc["Conditions"]["$values"] = [lc]
        items: List[Dict[str, Any]] = []
        if image_type == "FLAT" and ex.get("filter"):
            sw = _clone("SwitchFilter")
            sw["Filter"] = _filter_info(ex.get("filter", "") or "", filters)
            items.append(sw)
        te = _clone("TakeExposure")
        te["ExposureTime"] = float(ex.get("exposure_s", 0) or 0)
        te["Gain"] = int(ex.get("gain", -1))
        te["Offset"] = int(ex.get("offset", -1))
        te["Binning"] = _binning(ex.get("bin", 1))
        te["ImageType"] = image_type
        te["ExposureCount"] = 0
        items.append(te)
        sc["Items"]["$values"] = items
        return sc

    if len(rows) == 1:
        return _row(rows[0])
    outer = _clone("SequentialContainer")
    outer["Name"] = image_type
    outer["Items"]["$values"] = [_row(ex) for ex in rows]
    return outer


def _build_capture(r: dict, filters: Optional[List[dict]]) -> Dict[str, Any]:
    it = (r.get("image_type") or "LIGHT").upper()
    if it == "LIGHT":
        return _capture_light(r, filters)
    return _capture_calibration(r, filters)


def _build_startup(r: dict) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    ck = r.get("checks") or {}
    if ck.get("wait_for_dusk"):
        w = _clone("WaitForTime")
        w["SelectedProvider"] = _provider("DuskProvider")
        w["Hours"], w["Minutes"], w["Seconds"] = 0, 0, 0
        w["MinutesOffset"] = int(ck.get("dusk_offset_min", 0) or 0)
        items.append(w)
    elif ck.get("wait_for_clock"):
        c = ck["wait_for_clock"] or {}
        w = _clone("WaitForTime")
        w["SelectedProvider"] = _provider("TimeProvider")
        w["Hours"] = int(c.get("h", 0) or 0)
        w["Minutes"] = int(c.get("m", 0) or 0)
        w["Seconds"] = int(c.get("s", 0) or 0)
        w["MinutesOffset"] = 0
        items.append(w)
    if ck.get("unpark"):
        items.append(_clone("UnparkScope"))
    if ck.get("cool_camera"):
        c = _clone("CoolCamera")
        c["Temperature"] = float(ck.get("cool_to_c", -10) or -10)
        c["Duration"] = float(ck.get("cool_duration_min", 0) or 0)
        items.append(c)
    if ck.get("initial_autofocus"):
        items.append(_clone("RunAutofocus"))
    if ck.get("start_guiding"):
        g = _clone("StartGuiding")
        g["ForceCalibration"] = bool(ck.get("force_calibration", False))
        items.append(g)
    return items


def _build_teardown(r: dict) -> List[Dict[str, Any]]:
    ck = r.get("checks") or {}
    inner: List[Dict[str, Any]] = []
    if ck.get("stop_guiding"):
        inner.append(_clone("StopGuiding"))
    if ck.get("park"):
        inner.append(_clone("ParkScope"))
    if ck.get("warm_camera"):
        w = _clone("WarmCamera")
        w["Duration"] = float(ck.get("warm_duration_min", 0) or 0)
        inner.append(w)
    if ck.get("find_home"):
        inner.append(_clone("FindHome"))
    if ck.get("set_tracking_off"):
        st = _clone("SetTracking")
        st["TrackingMode"] = 5            # 真样本里关跟踪用的取值
        inner.append(st)
    if not inner:
        return []
    if r.get("parallel"):
        par = _clone("ParallelContainer")
        par["Name"] = "并行指令集"
        par["Items"]["$values"] = inner
        return [par]
    return inner


def _build_op(r: dict) -> Optional[Dict[str, Any]]:
    op = r.get("op")
    p = r.get("params") or {}
    if op == "autofocus":
        return _clone("RunAutofocus")
    if op == "switch_filter":
        sw = _clone("SwitchFilter")
        sw["Filter"] = _filter_info(p.get("filter", "") or "", None)
        if p.get("filter_position") is not None:
            sw["Filter"]["_position"] = int(p["filter_position"])
        return sw
    if op == "recalibrate_guiding":
        g = _clone("StartGuiding")
        g["ForceCalibration"] = True
        return g
    if op == "center":
        return _clone("Center")
    if op == "unpark":
        return _clone("UnparkScope")
    if op == "park":
        return _clone("ParkScope")
    if op == "find_home":
        return _clone("FindHome")
    if op == "set_tracking":
        return _clone("SetTracking")
    if op == "cool":
        c = _clone("CoolCamera")
        c["Duration"] = float(p.get("duration_min", 0) or 0)
        if p.get("cool_to_c") is not None:
            c["Temperature"] = float(p["cool_to_c"])
        return c
    if op == "warm":
        w = _clone("WarmCamera")
        w["Duration"] = float(p.get("duration_min", 0) or 0)
        return w
    if op == "wait":
        if p.get("wait_clock"):
            c = p["wait_clock"]
            w = _clone("WaitForTime")
            w["SelectedProvider"] = _provider("TimeProvider")
            w["Hours"] = int(c.get("h", 0) or 0)
            w["Minutes"] = int(c.get("m", 0) or 0)
            w["Seconds"] = int(c.get("s", 0) or 0)
            w["MinutesOffset"] = 0
            return w
        w = _clone("WaitForTimeSpan")
        w["Time"] = float(p.get("wait_min", 0) or 0)
        return w
    return None


def _build_wait(item: m.CompiledItem) -> Dict[str, Any]:
    kind = getattr(item, "wait_kind", "") or "timespan"
    if kind == "timespan":
        w = _clone("WaitForTimeSpan")
        w["Time"] = round(float(getattr(item, "wait_s", 0) or 0) / 60.0, 4)   # 分钟
        return w
    w = _clone("WaitForTime")
    if kind == "dusk":
        w["SelectedProvider"] = _provider("DuskProvider")
        w["Hours"] = w["Minutes"] = w["Seconds"] = 0
        w["MinutesOffset"] = int(getattr(item, "anchor_offset_min", 0) or 0)
    elif kind == "dawn":
        w["SelectedProvider"] = _provider("DawnProvider")
        w["Hours"] = w["Minutes"] = w["Seconds"] = 0
        w["MinutesOffset"] = int(getattr(item, "anchor_offset_min", 0) or 0)
    else:  # clock:把相对秒折算回当日钟点不可靠,退化为 TimeProvider 占位(0)
        w["SelectedProvider"] = _provider("TimeProvider")
        w["Hours"] = w["Minutes"] = w["Seconds"] = 0
        w["MinutesOffset"] = 0
    return w


# --------------------------------------------------------------------------- #
# 组装 + $id/$ref 后处理
# --------------------------------------------------------------------------- #
def _assign_ids(root: Dict[str, Any]) -> None:
    """先序遍历,给每个带 $type 的对象分配字符串 $id;Strategy 不分配。"""
    counter = [0]

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            if "$type" in n and _short(n) not in _ID_EXEMPT and "$id" not in n:
                counter[0] += 1
                n["$id"] = str(counter[0])
            for k, v in list(n.items()):
                if k in ("Parent", "$id", "$type", "$ref"):
                    continue
                walk(v)
        elif isinstance(n, list):
            for c in n:
                walk(c)

    walk(root)


def _set_parents(root: Dict[str, Any]) -> None:
    """Items/Conditions/Triggers 的每个子节点 Parent = 所属容器 $id 的 {$ref}。
    递归进容器与子容器;TriggerRunner / Root 的 Parent 保持 null(原型已是 null)。"""
    def walk(container: Dict[str, Any]) -> None:
        cid = container.get("$id")
        for key in ("Items", "Conditions", "Triggers"):
            coll = container.get(key)
            if isinstance(coll, dict):
                for child in coll.get("$values", []):
                    if isinstance(child, dict):
                        child["Parent"] = {"$ref": cid}
                        walk(child)
        # 触发器的 TriggerRunner 是个容器(自身 Parent 保持 null),
        # 但其内 payload(Dither/RunAutofocus/StartGuiding…)的 Parent 要指向 runner 的 $id
        runner = container.get("TriggerRunner")
        if isinstance(runner, dict):
            walk(runner)

    walk(root)


# --------------------------------------------------------------------------- #
# 公开入口
# --------------------------------------------------------------------------- #
def build_root(compiled: m.CompiledSequence, site: Optional[dict] = None,
               filters: Optional[List[dict]] = None,
               name: str = "星枢序列") -> Dict[str, Any]:
    """CompiledSequence(IR)→ 完整 SequenceRootContainer dict(含 $id/$ref,可 json.dumps)。"""
    root = _clone("SequenceRootContainer")
    root["Name"] = name
    root["Parent"] = None

    start = _clone("StartAreaContainer"); start["Name"] = "开始"
    target = _clone("TargetAreaContainer"); target["Name"] = "目标"
    end = _clone("EndAreaContainer"); end["Name"] = "结束"
    for c in (start, target, end):
        c["Items"]["$values"] = []
    root["Items"]["$values"] = [start, target, end]

    for item in (compiled.items or []):
        kind = item.card_kind
        r = item.resolved or {}
        if kind == "startup":
            start["Items"]["$values"].extend(_build_startup(r))
        elif kind == "teardown":
            end["Items"]["$values"].extend(_build_teardown(r))
        elif kind == "_wait":
            target["Items"]["$values"].append(_build_wait(item))
        elif kind == "capture":
            target["Items"]["$values"].append(_build_capture(r, filters))
        elif kind == "op":
            node = _build_op(r)
            if node is not None:
                target["Items"]["$values"].append(node)

    _assign_ids(root)
    _set_parents(root)
    return root


# 允许出现的 $type 全集(递归收集自原型 + provider);用于自检白名单
def _allowed_types() -> set:
    fulls = set(_PROVIDERS)

    def collect(n: Any) -> None:
        if isinstance(n, dict):
            if "$type" in n:
                fulls.add(n["$type"])
            for k, v in n.items():
                if k != "Parent":
                    collect(v)
        elif isinstance(n, list):
            for c in n:
                collect(c)

    for proto in _PROTO.values():
        collect(proto)
    for v in _COLL.values():
        fulls.add(v)
    return fulls


_ALLOWED = _allowed_types()


def validate(root: Dict[str, Any]) -> dict:
    """静态自检(只读)。load 返回 200 ≠ 序列正确,故下发前必过此关 + 回读 /sequence/json 比对。"""
    errors: List[str] = []
    ids: List[str] = []
    refs: List[str] = []
    n_nodes = [0]
    smart_count = [0]
    smart_with_dither = [0]

    def walk(n: Any, path: str) -> None:
        if isinstance(n, dict):
            if "$type" in n:
                n_nodes[0] += 1
                t = n["$type"]
                if t not in _ALLOWED:
                    errors.append(f"{path}: 未知 $type «{t}»(可能被 NINA 静默降级为 Unknown 节点)")
                if _short(n) not in _ID_EXEMPT and "$id" not in n:
                    errors.append(f"{path}: 缺 $id(短名 {_short(n)})")
                if "ImageType" in n and n["ImageType"] not in VALID_IMAGE_TYPES:
                    errors.append(f"{path}: ImageType «{n['ImageType']}» 非法")
                if _short(n) == "SmartExposure":
                    smart_count[0] += 1
                    trigs = (n.get("Triggers") or {}).get("$values", [])
                    if any(_short(t) == "DitherAfterExposures" for t in trigs):
                        smart_with_dither[0] += 1
                    else:
                        errors.append(f"{path}: SmartExposure 缺 DitherAfterExposures(start 校验会 NPE)")
                # 这些触发器靠 TriggerRunner 内的指令执行动作,runner 为空 = 触发后什么也不做
                if _short(n) in ("DitherAfterExposures", "AutofocusAfterTimeTrigger",
                                 "AutofocusAfterTemperatureChangeTrigger", "RestoreGuiding"):
                    runner_items = ((n.get("TriggerRunner") or {}).get("Items") or {}).get("$values", [])
                    if not runner_items:
                        errors.append(f"{path}: {_short(n)} 的 TriggerRunner 为空——触发后不执行任何动作")
            if "$id" in n:
                ids.append(n["$id"])
            if "$ref" in n:
                refs.append(n["$ref"])
            for k, v in n.items():
                if k in ("$id", "$type", "$ref"):
                    continue
                walk(v, f"{path}.{k}")
        elif isinstance(n, list):
            for i, c in enumerate(n):
                walk(c, f"{path}[{i}]")

    walk(root, "$")

    # $id 唯一
    dup = {x for x in ids if ids.count(x) > 1}
    if dup:
        errors.append(f"$id 重复:{sorted(dup)}")
    # $ref 可解析
    idset = set(ids)
    bad = [r for r in refs if r not in idset]
    if bad:
        errors.append(f"$ref 悬空(指向不存在的 $id):{sorted(set(bad))}")
    # 根三段式
    if _short(root) != "SequenceRootContainer":
        errors.append(f"根不是 SequenceRootContainer(是 {_short(root)})")
    else:
        areas = [_short(x) for x in (root.get("Items") or {}).get("$values", [])]
        if areas != ["StartAreaContainer", "TargetAreaContainer", "EndAreaContainer"]:
            errors.append(f"Root.Items 应为 [Start/Target/End]AreaContainer,实际 {areas}")
    if root.get("Parent") is not None:
        errors.append("Root.Parent 应为 null")

    return {
        "ok": not errors,
        "errors": errors,
        "stats": {
            "nodes": n_nodes[0],
            "ids": len(ids),
            "refs": len(refs),
            "smart_exposures": smart_count[0],
            "smart_with_dither": smart_with_dither[0],
        },
    }


def build_and_validate(compiled: m.CompiledSequence, site: Optional[dict] = None,
                       filters: Optional[List[dict]] = None,
                       name: str = "星枢序列") -> Tuple[Dict[str, Any], dict]:
    root = build_root(compiled, site=site, filters=filters, name=name)
    return root, validate(root)
