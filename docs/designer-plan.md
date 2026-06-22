已掌握全部必要事实。现在产出统一设计方案。

---

# 星枢「卡片 → 时间轴 → 单轨序列 → NINA 执行」统一设计方案

> 适用代号:星枢 nina-web;目标台:130apo(NINA 2.x + ninaAPI 2.2.11.1,base `http://100.107.140.109:1888/v2/api`)。
> 全程对 130apo 真机【只读】:仅 GET 探查 / 读源码 / 前端编排 / 估时;一切 POST / load / start / set-target 等写操作,先在空闲盒子或 sim 验证后再放行,且受 `allow_sequence_start` 与控制权锁双重门控。

---

## 1. 总览与核心理念

四步心智模型,贯穿全方案,也是页面三大分区的来源:

```
①卡片库 Cards        ②多轨时间轴 Timeline          ③压缩单轨 Compile        ④下发执行 Execute
┌──────────┐  拖放    ┌────────────────────────┐  压缩   ┌──────────────┐  生成    ┌─────────────┐
│ 拍摄/初始 │ ──────▶ │ 轨0 ▓▓▓░░▓▓▓             │ ─────▶ │ 单轨 ▓▓▓▓▓▓▓ │ ──────▶ │ NINA JSON   │
│ 终止/操作 │         │ 轨1   ▓▓▓▓                │  检测   │ (理论不重叠) │  load   │ +start(护栏) │
│ 复用模板  │         │ 轨2      ▓▓░░▓▓           │  重叠回退│              │         │             │
└──────────┘         └─横轴=日落..日出+暮光虚线─┘        └──────────────┘         └─────────────┘
```

- **① 卡片库**:可复用的「拍摄意图」最小单元。卡片只描述「做什么 + 拍多少」,不含「何时」。四类:拍摄卡 / 初始卡 / 终止卡 / 操作卡。
- **② 多轨时间轴**:把卡片拖到某条轨道的某个时刻,卡片按「预期总时长」展开成一条**序列条(clip)**;横轴是今晚真实天文时间(日落 20:02 → 次日日出 06:28,暮光虚线 21:35 / 04:55)。多轨用于「方案探索」:把不同目标、不同分组并排比对、对齐天象窗口。
- **③ 压缩成单轨**:探索定稿后,把所有轨道按时间归并到**一条最终轨**。这一步做重叠检测——理论上排好就不重叠;若重叠则定位冲突并提示回第二部分修改。
- **④ 下发执行**:把最终单轨翻译成 NINA 高级序列 JSON(Newtonsoft 全套),经 `POST /sequence/load` 灌入内存,再(护栏放行后)`GET /sequence/start` 让 NINA 自主执行。

关键概念边界(贯穿全方案):
- **卡片 = 模板**(可复用,无时间);**序列条 clip = 卡片的一次时间轴实例**(有起止时刻、可双击改参数、时长随参数实时变)。同一张卡片可在时间轴上实例化多次。
- **时长是估算,不是承诺**。横轴上的长度由静态公式算出(第 8 章详述漂移应对);NINA 实际执行受对焦/抖动/翻转/云影响会漂。时间轴是「计划意图」,不是「保证时刻表」。
- **「何时开拍」靠时间锚定节点表达**(WaitForTime / TimeCondition),不是靠时间轴像素位置硬塞——像素位置只是编辑期的可视化,编译时翻译成显式的等待/时间条件节点交给 NINA。

---

## 2. 数据模型(前后端契约)

新增一组「设计器」模型,**与现有 `SequencePlan/SequenceTarget/SequenceExposure`(`backend/gateway/models.py:307-344`)并存**:设计器模型负责编辑期(卡片/轨道/工程),编译产物 `CompiledSequence` 是中间表示(IR),最终由后端 IR→NINA JSON。现有 `SequencePlan` 继续作为「执行态/sim 引擎」契约,编译器可在 sim 模式下把 IR 降级成 `SequencePlan` 喂给 `sim/engine.py` 的 `_run_sequence` 做预演。

放置位置:`backend/gateway/models.py` 末尾新增下列 Pydantic 模型(全部新字段,向后兼容,旧端点不受影响)。

### 2.1 Card(卡片,判别联合 `kind`)

所有卡片共享:`id`(uuid)、`kind`(判别字段)、`label`(用户起的名)、`color`(时间轴配色,可空走默认)、`notes`。

```jsonc
// kind = "capture" 拍摄卡(最重要)
{
  "id": "c_8f3a",
  "kind": "capture",
  "label": "M31 LRGB",
  "image_type": "LIGHT",            // LIGHT|DARK|FLAT|BIAS  ← ImageType 五常量
  "target": {                        // 仅 image_type=LIGHT 必填;校准帧为 null
    "name": "M 31",
    "ra_hours": 0.712,               // 十进制小时(UI/估时用),编译时拆六十进制
    "dec_degrees": 41.269,           // 十进制度(可负;编译拆 NegativeDec+DMS)
    "rotation": 0.0                  // PositionAngle
  },
  "exposures": [                     // 多滤镜=多行,每行→一个 SmartExposure
    { "filter": "L", "exposure_s": 300, "gain": -1, "offset": -1, "bin": 1, "count": 20 },
    { "filter": "R", "exposure_s": 300, "gain": -1, "offset": -1, "bin": 1, "count": 10 }
  ],
  "dither_every": 2,                 // → DitherAfterExposures.AfterExposures;0=关
  "af_interval_min": 60,             // → AutofocusAfterTimeTrigger.Amount;0=关
  "af_on_temp_delta": 5.0,           // → AutofocusAfterTemperatureChangeTrigger.Amount;0=关
  "meridian_flip": true              // → 该 Exposure 容器挂 MeridianFlipTrigger
  // 注:gain/offset=-1 = 跟随相机/Profile 默认(哨兵值,非缺失)
}
```

```jsonc
// kind = "startup" 初始卡(勾选项)
{
  "id": "c_init", "kind": "startup", "label": "今晚开台",
  "checks": {
    "wait_for_dusk": true,           // WaitForTime(DuskProvider, MinutesOffset)
    "dusk_offset_min": -10,          // 黄昏前/后偏移(负=提前)
    "wait_for_clock": null,          // 或绝对钟点 {"h":21,"m":0,"s":0}(TimeProvider);与 dusk 二选一
    "unpark": true,                  // UnparkScope
    "cool_camera": true,             // CoolCamera
    "cool_to_c": -10,                // 目标温度
    "cool_duration_min": 2.0,        // CoolCamera.Duration(分钟,渐变)
    "connect_all": false,            // ConnectAllEquipment(可选)
    "initial_autofocus": false,      // 开台先做一次 RunAutofocus
    "start_guiding": false,          // 全局起手导星(通常放各目标 Starting 内)
    "force_calibration": false       // StartGuiding.ForceCalibration
  }
}
```

```jsonc
// kind = "teardown" 终止卡(勾选项)
{
  "id": "c_end", "kind": "teardown", "label": "收台",
  "parallel": true,                  // true→ParallelContainer 并行收尾;false→串行 EndAreaContainer.Items
  "checks": {
    "stop_guiding": true,            // StopGuiding
    "park": true,                    // ParkScope
    "warm_camera": true,             // WarmCamera
    "warm_duration_min": 10.0,       // WarmCamera.Duration(分钟,渐变)
    "find_home": false,              // FindHome
    "set_tracking_off": false,       // SetTracking(关)
    "disconnect_all": false
  }
}
```

```jsonc
// kind = "op" 操作小卡(单一动作)
{
  "id": "c_af", "kind": "op", "label": "重对焦",
  "op": "autofocus",                 // autofocus|switch_filter|recalibrate_guiding|
                                     // center|unpark|park|cool|warm|wait|find_home|set_tracking
  "params": {                        // 按 op 取用
    "filter": "Ha",                  // switch_filter: 名+槽位
    "filter_position": 4,
    "force_calibration": true,       // recalibrate_guiding
    "duration_min": 2.0,             // cool/warm
    "wait_min": 5.0,                 // wait → WaitForTimeSpan.Time
    "wait_clock": null               // 或绝对钟点 → WaitForTime(TimeProvider)
  }
}
```

### 2.2 Clip(时间轴上的卡片实例)

```jsonc
{
  "id": "clip_001",
  "card_id": "c_8f3a",               // 引用卡片库
  "track_id": "t0",
  "start_s": 5700,                   // 相对「时间轴零点」的秒数(零点见 Project.timeline_origin)
  "duration_s": 7860,               // 由估时公式算出(只读字段,后端/前端可重算)
  "overrides": {                     // 双击编辑产生的实例级覆盖(不改卡片库原卡)
    "exposures": [ { "filter":"L", "count": 12 } ]   // 仅覆盖填写的字段,深合并
  },
  "anchor": "snap",                  // snap=吸附前一条(无缝接续) | gap=留间隔(需等待)
                                     // | clock=绝对钟点开拍 | dusk/dawn=天象锚
  "anchor_clock": null,              // anchor=clock 时 {"h":21,"m":30}
  "anchor_offset_min": 0             // anchor=dusk/dawn 时相对天象偏移
}
```

`duration_s` 是派生量:`effective_card = deepMerge(card, overrides)` 后过估时公式(第 4.3、依据「时间成本模型」)。`anchor` 决定 clip 起点如何确定,也决定编译时插不插等待节点(第 4、6 章)。

### 2.3 Track / Project / CompiledSequence

```jsonc
// Track
{ "id": "t0", "name": "主镜 LRGB", "muted": false, "order": 0, "clips": ["clip_001","clip_002"] }

// Project(整份编排工程,即「设计器文档」)
{
  "id": "proj_2026-06-18",
  "name": "0618 夜",
  "date": "2026-06-18",              // 决定暮光/日出日落
  "site": { "lat": 25.065, "lon": 101.538, "elev": 2000 },  // 从 /profile/show 取一次,缓存
  "timeline_origin_iso": "2026-06-18T20:00:00+08:00",       // 横轴零点(通常≈日落)
  "timeline_end_iso":   "2026-06-19T06:30:00+08:00",        // 横轴末(≈日出)
  "cards":  [ /* Card[] 卡片库 */ ],
  "tracks": [ /* Track[] */ ],
  "overhead": {                      // 估时开销表(可调默认,见第 4.3)
    "readout_download_s": 5, "dither_s": 12, "filter_switch_s": 3,
    "autofocus_s": 90, "startup_overhead_s": 180, "meridian_flip_s": 120,
    "park_s": 45, "unpark_s": 20
  }
}

// CompiledSequence(③压缩产物 = 中间表示 IR,单轨有序)
{
  "ok": true,
  "project_id": "proj_2026-06-18",
  "overlaps": [],                    // 非空=压缩失败,每项 {clip_a, clip_b, at_s, overlap_s}
  "items": [                         // 单轨、按 start_s 升序、互不重叠
    { "clip_id":"clip_init", "card_kind":"startup", "start_s":0,    "duration_s":40,
      "anchor":"dusk", "anchor_offset_min":-10, "resolved": { /* 见下 */ } },
    { "clip_id":"clip_001",  "card_kind":"capture", "start_s":40,   "duration_s":7860,
      "anchor":"snap", "resolved": { /* 拍摄卡解析后的曝光/触发/坐标 */ } },
    { "clip_id":"clip_gap",  "card_kind":"_wait",   "start_s":7900, "duration_s":600,
      "anchor":"gap", "wait_kind":"timespan", "wait_s":600 },        // 间隔→自动插入的等待项
    { "clip_id":"clip_end",  "card_kind":"teardown","start_s":8500, "duration_s":610 }
  ],
  "totals": { "frames": 30, "light_s": 9000, "wall_clock_s": 11500,
              "ends_at_iso":"2026-06-19T...", "fits_in_night": true }
}
```

`CompiledSequence` 是「时间轴语义」与「NINA 树」之间的解耦层:前端只产出 IR,**NINA Newtonsoft JSON 的脏活全在后端**(第 6 章),前端永不直接拼 `$type/$ref`。

---

## 3. 第一部分 — 卡片库 UI

落地位置:新建 `frontend/nina-designer.html`,与现有 `nina-sequence.html`(运行监视/简版编辑器)并存;在 `nina-theme.js` 的 `NAV`(`frontend/nina-theme.js:49`)加 `["设计","/designer","Designer"]`,在 `backend/app.py` 的 `PAGES`(`app.py:25`)加 `"/designer": "nina-designer.html"`。沿用星图册宪法:无卡片视觉(讽刺的是「拍摄卡片」是逻辑卡片,UI 用发丝线块 `.tgt/.exp` 呈现,**不画液态玻璃卡**)、全衬线、唯一金色强调、底部状态行不弹窗。

布局:`.col-rail`(主区 + 280px 右栏)。左主区上半=卡片库(横向滚动的发丝线块列表),下半=时间轴(第 4 章);右栏=当前选中卡片/clip 的字段表单(`.kv/.k/.v` + `input.fld/select.fld`)。

### 各类卡片表单

| 卡片 | UI 区块 | 字段(控件) |
|---|---|---|
| **拍摄卡** | 段头 `.sec-h`「拍摄」+ 类型切换 | `image_type` 段控(LIGHT/DARK/FLAT/BIAS)。选 LIGHT 才显示「目标」子区:`name`(text)+ 检索按钮(复用 `GET /api/framing/search?q=` 回填 RA/Dec,见「现有网站架构」)、`ra_hours`/`dec_degrees`(`fmtRA/fmtDec` 显示)、`rotation`。「曝光行」表 `.exp表`:每行 filter(select,选项来自滤轮配置)/exposure_s/gain/offset/bin/count,行尾「+ 滤镜行」。底部触发:`dither_every`、`af_interval_min`、`af_on_temp_delta`、`meridian_flip`(`.toggle`)。 |
| **初始卡** | 段头「初始 · 勾选要做的」 | 一组 `.toggle`:☐到黄昏开拍(`wait_for_dusk` + 偏移 `dusk_offset_min`)/ ☐绝对钟点(`wait_for_clock`)/ ☐解锁赤道仪 Unpark / ☐相机制冷(`cool_to_c` + `cool_duration_min`)/ ☐连接所有设备 / ☐开台先对焦 / ☐起手导星(+ 强制校准)。 |
| **终止卡** | 段头「终止 · 勾选收尾」 | `.toggle`:☐停止导星 / ☐停靠 Park / ☐相机回温(`warm_duration_min`)/ ☐回 Home / ☐关跟踪 / ☐断开设备;顶部 ☐并行收尾(`parallel`,park 与 warm 同时)。 |
| **操作卡** | 段头「操作 · 单一动作」 | `op` 下拉(自动对焦/换滤镜/重校导星/居中/解锁/停靠/制冷/回温/等待/回Home/跟踪);按选择动态显示 params(换滤镜→filter+槽位;重校导星→force_calibration;制冷回温→duration_min;等待→wait_min 或 wait_clock)。 |

交互:内存单一事实对象 `proj`(对标 `nina-sequence.html` 的 `plan`),`render()` 重建 DOM,`data-*` 委托 `onchange/onclick` 双向写回(沿用现有惯例)。卡片库每张卡一个发丝线块 + 右上「复制 / 删除 / 拖到时间轴」。「+ 新建卡片」按 kind 用 `defaultCard(kind)` 工厂建空卡。卡片库与 clip 实例分离:改卡片库原卡**不**自动改已落在时间轴上的 clip(clip 有自己的 `overrides` 快照);提供「同步此卡所有实例」显式按钮。

---

## 4. 第二部分 — 时间轴交互(类 Premiere)

### 4.1 横轴渲染(日落—日出 + 暮光虚线)

- 横轴范围 `[timeline_origin .. timeline_end]`,默认 = 当日 **日落 → 次日日出**。今晚(2026-06-18, 25.065N/101.538E, 北京时)实算:日落≈20:02、天文昏影 Dusk≈21:35、天文晨光 Dawn≈04:55、日出≈06:28(依据「时间成本模型」)。
- 算法**纯前端 JS**,直译 `backend/gateway/sim/astro.py` 现有 `sun_radec/sun_altitude/gmst_hours/julian_date`(已存在,函数签名见 `astro.py:8-89`)。在浏览器逐分钟扫描太阳高度、对目标高度线性插值求穿越点,`target_alt` 参数化:日落/日出 −0.833°、民用 −6°、航海 −12°、天文 −18°。
- 暮光用**竖直虚线**标注:实线=日落/日出,虚线=天文昏影/晨光(`var(--hair)` 细线;暗夜核心段 `[Dusk..Dawn]` 用极淡背景高亮)。整点刻度 + 标签用 `var(--celestial)`。
- 站点坐标:后端一次性从 ninaAPI `GET /profile/show` 读 `AstrometrySettings.{Latitude,Longitude,Elevation}` 透传(`reference/ninaAPI/.../Profile.cs:75,108`);sim 用配置默认 25.065/101.538/2000。**前端不硬编码**。
- 画布:沿用 canvas 套路(dpr + rAF,参考 `nina-mount.html` SkyChart);clip 条用 DOM 绝对定位块(便于双击/拖拽/文字)叠在 canvas 网格上。像素↔时间:`pxPerSec = canvasWidth / (end-origin)`,`x = (start_s)*pxPerSec`。

### 4.2 拖放 → 序列条(无 HTML5 拖拽先例,自建 pointer)

全站无 HTML5 drag 先例(「现有网站架构」明确),用 **pointerdown/move/up + setPointerCapture**(移植 `nina-mount.html` 的拖拽手法):

1. 卡片库块 `pointerdown` → 创建「拖影」跟随光标;`pointermove` 实时把光标 x 投影成 `start_s`、命中某条轨道。
2. `pointerup` 落在某轨某时刻 → 新建 `Clip{card_id, track_id, start_s, duration_s=estimate(card)}`,推入 `track.clips`,触发该轨**重排**(4.4)。
3. 已有 clip 的拖动同理:`pointermove` 改 `start_s`(实时碰撞预演,见 4.4),`pointerup` 落定。

### 4.3 序列条长度 = 估时(双击编辑实时变长)

时长公式(依据「时间成本模型」`duration_formula`,前端静态估算):

```
T_clip = T_startup_if_dso
       + Σ_filter [ count×(exposure_s + readout_download_s)
                  + floor(count/dither_every)×dither_s
                  + filter_switch_s ]                       // 每个 SmartExposure 1 次切换
       + ceil( total_exposure_min / af_interval_min ) × autofocus_s   // 时间触发对焦
       (+ meridian_flip_s 若该窗口跨子午线,按需 0/1 次)
```

- 拍摄卡(LIGHT/DSO):`T_startup = startup_overhead_s`(Center+RunAutofocus+StartGuiding ≈180s)。校准帧无 startup、无 dither、无 filter 切换(单滤镜)、无 af。
- 初始卡:仅计实耗(Unpark≈20s + CoolCamera.Duration);`WaitForTime`(到黄昏/钟点)是**时间轴定位非机时**,不计入 clip 长度,只决定 clip 在横轴起点。
- 终止卡:`parallel` → `max(park_s, warm_duration_min×60)`;串行 → 求和。
- 操作卡:按 op 查表(autofocus 90 / center 45 / switch_filter 3 / park 45 / cool|warm=duration / wait=wait_min×60 …)。`wait` 这类纯等待也不计机时、只占位。
- 开销表 `Project.overhead` 全部**可调默认**;能从卡片读到的(exposure_s/count/dither_every/af_interval_min/duration)直接读,读不到的用经验默认并暴露给用户微调。

**双击编辑实时变长**:双击 clip → 右栏弹出该 clip 的实例编辑(写入 `clip.overrides`,不改卡片库);任一字段 `onchange` → `recompute(clip)` 重算 `duration_s` → clip 块宽度立即变 → **触发本轨重排**(后面的 clip 若吸附会跟着平移)。减少 count → 变短 → 后续吸附条左移。

### 4.4 同轨不重叠(强排到后面)+ 异轨可重叠 + 吸附 + 间隔

核心约束的算法实现:

**碰撞检测**:两 clip 重叠 ⟺ 同轨 且 `a.start < b.start+b.dur && b.start < a.start+a.dur`。异轨不检测(允许重叠)。

**吸附阈值** `SNAP_PX = 8`(像素),换算 `SNAP_S = SNAP_PX / pxPerSec`。

**拖动落定算法(`placeClip(track, clip, desiredStart)`)**:
```
1. 候选 start = desiredStart。
2. 吸附:在同轨找最近的 clip 边界(前一条的 end、后一条的 start);
   若 |start - 边界| <= SNAP_S 则 start = 边界(吸附 = 无缝接续)。
3. 同轨防重叠(强排到后面,像 PR ripple-to-after):
   按 start 排序同轨其余 clip;若候选 [start, start+dur] 与某条 e 重叠,
   则 start = e.end(推到该条之后),重复检查直到无重叠。
   (可选 push 模式:被顶到的后续条整体右移 dur,保持彼此间隔;
    默认 ripple-insert 模式:只把当前条放到空档/末尾。由 4.4 末「待拍板」决定。)
4. 写回 clip.start_s。重算本轨所有「吸附链」:凡 anchor=snap 的 clip,
   其 start = 前一条 end(级联);anchor=gap 的保持其与前条的间隔不变。
```

**吸附 vs 间隔的语义**(决定编译,第 6 章):
- **吸附**(两条首尾贴合,`gap < SNAP_S`)→ clip.anchor=`snap` → 编译时**不插任何等待节点**,父容器 Sequential,Items 顺序即执行顺序,天然顺接。
- **间隔**(两条之间留空 `gap_s = next.start - prev.end > 0`)→ 在两条之间生成一个 `_wait` 项 → 编译成 `WaitForTimeSpan{Time = gap_s/60 分钟}`(相对延时)。
- **绝对钟点/天象锚**(clip.anchor=`clock`/`dusk`/`dawn`)→ 编译成 `WaitForTime{SelectedProvider=TimeProvider/DuskProvider/DawnProvider, Hours/Minutes/Seconds 或 MinutesOffset}`。第一条初始卡通常 anchor=`dusk`,offset −10。

**视觉反馈**:拖动中实时显示吸附参考线(金色细线)、被强排时 clip 闪一下并 `Ops.status('info','已排到前一序列之后')`;异轨重叠不阻止、不提示。

---

## 5. 第三部分 — 压缩成单轨

「探索用多轨,执行只能一轨」——把 N 条轨道归并成一条最终轨。

**合并算法 `compile(project) -> CompiledSequence`**(前端先做预检,后端权威重算):
```
1. 收集所有未 muted 轨道的所有 clip,解析每个 clip 的绝对开始时刻:
   - anchor=clock/dusk/dawn → 用 astro 把天象/钟点解析成绝对秒(相对 timeline_origin)。
   - anchor=snap/gap → 用其在本轨内已排定的 start_s。
2. 按 (绝对 start_s, 然后 track.order) 升序排序,得到候选单轨序列 cand[]。
3. 重叠检测:遍历 cand,若 cand[i].start < cand[i-1].end → 记一条 overlap
   {clip_a:cand[i-1], clip_b:cand[i], at_s, overlap_s}。
4. 若 overlaps 非空 → ok=false,返回 overlaps;前端在时间轴上把冲突两条红描边
   (var(--err)),状态行提示「压缩失败:M31 与 M42 在 23:14 重叠 8min,请回时间轴调整」,
   并提供「跳到冲突处」。不生成 NINA JSON。
5. 若无重叠 → 在相邻 clip 间按 gap 生成等待项(_wait):
   gap<=SNAP_S→吸附不插;gap>0→WaitForTimeSpan;首条/锚定条→WaitForTime。
6. 归类装入三段式骨架(第 6 章):startup 卡→StartAreaContainer;
   capture/op→TargetAreaContainer(LIGHT 各成 DeepSkyObjectContainer,
   校准/操作卡按规则);teardown 卡→EndAreaContainer。
7. 计算 totals(总帧/总曝光/落幕时刻/是否 fits_in_night:ends_at <= 日出)。
8. 返回 CompiledSequence(items 单轨有序 + totals)。
```

**最终序列预览**:压缩成功后,右栏/弹层显示「最终单轨」只读预览——一条满宽时间轴条带 + 文字大纲(树形:开始 → 目标1[Starting/Exposure/Endding] → 等待 → 目标2 … → 结束),底部 totals(共 N 帧 / 纯曝光 X h / 收台于 HH:MM / ✅在日出前结束 或 ⚠超出日出 1.2h)。预览旁两个动作:「生成 NINA 序列(load)」「另存为模板」。

**重叠回退**:压缩只读不破坏多轨编排;用户改完多轨再压一次。压缩结果不自动覆盖多轨工程(`Project` 与 `CompiledSequence` 分离存储)。

---

## 6. NINA 执行架构(IR → NINA JSON → 灌入 → 启动)

### 6.1 选定通路与退路

| | 方案 | 说明 | 取舍 |
|---|---|---|---|
| **选定** | **通路1:`POST /v2/api/sequence/load`(完整树)** | 后端把 `CompiledSequence` IR 编译成完整 `SequenceRootContainer` JSON(Newtonsoft 全套)作 body 提交;NINA 用 `SequenceJsonConverter` 反序列化 + `SetAdvancedSequence` 装入内存。随后(护栏放行)`GET /sequence/start`。 | 全程 HTTP、无需 130apo 文件系统访问;一次装入任意复杂多目标;与 live.py 网关风格一致,仅新增一个 POST。端点已实测存活、版本 2.2.11.1 匹配、有 636KB 真实样本可做模板。**JSON 生成是硬骨头但可控**。 |
| **退路 A** | **通路3:写 .json 到 NINA 序列目录 + `GET /sequence/load?sequenceName=`** | 生成同样的 JSON 落盘到 `Documents\N.I.N.A\`(经 SSH/SMB),`GET /sequence/list-available` 确认后按名加载。 | 序列在 NINA 桌面端可见/可二次编辑/留档;需文件系统写权限。作为「想留痕」或通路1调试期的旁路。 |
| **退路 B** | **通路4:桌面 UI 手动加载** | 星枢只生成并提供 .json 下载,人在 130apo NINA 桌面端手动 Load + Run。 | 零 API 风险、人工把关;非自动化,仅最终降级。 |
| **不选** | 通路2:骨架 load + `set-target` | set-target 只能改已存在第 index 个目标、不能增减目标数;曝光/滤镜仍靠脆弱 `/sequence/edit`。 | 目标数固定场景的降风险变体,但本特性目标数可变,放弃。 |

### 6.2 IR → NINA 节点映射(后端编译器,依据「JSON生成规格」)

后端新增 `backend/gateway/nina_seq.py`:`def build_root(compiled: CompiledSequence, site, filters) -> dict`,**以真实样本 `/tmp/nina-seq/seq.json` 为模板派生**,绝不凭空手写 `$type`。脚手架硬规则:
- 全局递增**字符串** `$id` 计数器,先序遍历分配;`Strategy` 与 DateTimeProvider 无 `$id`。
- `$type` = `"命名空间.类名, 程序集"`;程序集后缀精确:容器/指令/条件/触发器=`NINA.Sequencer`;InputTarget/InputCoordinates=`NINA.Astrometry`;FilterInfo/BinningMode/FlatWizardFilterSettings/AsyncObservableCollection=`NINA.Core`;ObservableCollection 包装=`System.ObjectModel`(元素接口仍带 `NINA.Sequencer`,泛型记号 `` `1[[...]] `` 原样)。
- 所有集合 = `{$id,$type(ObservableCollection),$values:[]}` 三件套,空集合也不省。
- `Parent` = 指向直接父容器 `$id` 的 `{$ref}`;根 `Parent=null`;遍历跳过 Parent 边防死循环。
- 根必须是 `SequenceRootContainer`,`Root.Items` 恰好 3 项有序:StartAreaContainer / TargetAreaContainer / EndAreaContainer。

卡片 → 节点:

| IR 项 | NINA 节点 |
|---|---|
| startup 卡 | `StartAreaContainer.Items`:按 checks 放 `WaitForTime`(DuskProvider/MinutesOffset 或 TimeProvider/HMS)+ `UnparkScope` (+ `CoolCamera{Duration}` + `RunAutofocus` + `StartGuiding{ForceCalibration}`) |
| capture LIGHT | `DeepSkyObjectContainer{Target=InputTarget{TargetName,PositionAngle,InputCoordinates(六十进制)}}` → 3 子 `SequentialContainer`:**Starting**(UnparkScope→Center{Inherited=true,坐标拷贝}→RunAutofocus→StartGuiding)/ **Exposure**(每滤镜行一个 `SmartExposure`:Conditions=[`LoopCondition{Iterations=count}`]、Items=[`SwitchFilter{Filter=FilterInfo}`,`TakeExposure{ExposureTime,Gain,Offset,Binning,ImageType="LIGHT",ExposureCount=0}`]、Triggers=[`DitherAfterExposures{AfterExposures=dither_every}`];容器 Triggers 按需挂 `AutofocusAfterTimeTrigger{Amount=af_interval_min}` / `AutofocusAfterTemperatureChangeTrigger{Amount=af_on_temp_delta}` / `MeridianFlipTrigger`)/ **Endding**(StopGuiding) |
| capture DARK/FLAT/BIAS | 裸 `TakeExposure{ImageType}` 包进 `SequentialContainer + LoopCondition{count}`,无 Target/Center/Guiding/Dither |
| op 卡 | 单指令:autofocus→`RunAutofocus{Attempts=3}`;switch_filter→`SwitchFilter{Filter}`;recalibrate→`StartGuiding{ForceCalibration=true}`;center→`Center`;wait→`WaitForTimeSpan{Time}` 或 `WaitForTime`;cool/warm→`CoolCamera/WarmCamera{Duration}` |
| teardown 卡 | `EndAreaContainer` →(parallel)`ParallelContainer{Strategy=Parallel}`:`StopGuiding`∥`ParkScope`∥`WarmCamera{Duration}`(+FindHome/SetTracking) |
| `_wait`(间隔) | `WaitForTimeSpan{Time=gap_min}`,插在前一项之后(放 TargetAreaContainer.Items 相应位置) |

### 6.3 时间锚定落地

- **到点开拍**:序列首个 `WaitForTime`(StartAreaContainer 内)= 整条序列开拍时刻。`SelectedProvider`=TimeProvider(Hours/Minutes/Seconds)或 DuskProvider/DawnProvider(MinutesOffset)。
- **相对间隔**:`WaitForTimeSpan{Time=分钟 double}`。
- **吸附**:不插节点。
- **按时结束某目标**:在该 DSO 的 **Exposure 容器 Conditions** 挂 `TimeCondition{Hours/Minutes/Seconds/MinutesOffset, SelectedProvider}`(无 ErrorBehavior/Attempts,Parent=$ref 该容器)。来源:用户给该 clip 设了「在 HH:MM 前结束」或下一 clip 的绝对起点。
- **按高度结束**:`AltitudeCondition / SunAltitudeCondition`(对照样本 Comparator 取值)。
- **拍 N 张** ≠ 时间:`LoopCondition.Iterations`(在 SmartExposure.Conditions),不是 `TakeExposure.ExposureCount`(后者生成时置 0)。

### 6.4 坐标转换

UI 用十进制(小时/度);编译时拆六十进制:RA 小时→`RAHours+RAMinutes+RASeconds`;Dec→`NegativeDec(bool)` + `DecDegrees/DecMinutes/DecSeconds`(**绝对值**,负号只靠 NegativeDec 表达)。Center/WaitForAltitude/AltitudeCondition 各持独立 InputCoordinates 拷贝(各带自己 `$id`),`Center.Inherited=true` 仍要写出坐标。

### 6.5 2.2.11.1 的限制对方案的影响

1. **版本绑定**:`$type` 携程序集+类名,必须以本机真实样本 `/tmp/nina-seq/seq.json` 派生;NINA 升级后模板可能需重生成。→ 编译器把 `$type` 字符串集中到一张「类型表」常量,便于一处改;落地前用 6.6 自检。
2. **无「追加目标」API**:`set-target` 只能改已存在目标,`load` 只能整树替换。→ 星枢每次「下发」都是**整树 load 替换内存序列**,不做增量;符合「压缩成单轨一次性下发」的产品形态。
3. **load 不落盘不自动运行 + 运行中 load 会 400**:→ 下发前先用 `start` 返回的 409 探测「是否空闲」(见 7.2);运行中禁止 load。装入后单独 `start`。
4. **start 绕过星枢赤道仪护栏**:`GET /sequence/start` 让 NINA 自主执行 GOTO/中天翻转,完全绕过 `_slew_safety`。→ `start` 默认受 `allow_sequence_start`(`settings`,对应 `live.py:686` 已有判断)关闭;开放前必须人工确认 NINA 端中天翻转/限位/安全监视器已配置(130apo 真机,撞镜/打腿风险)。
5. **底层 API 无鉴权、CORS `*`**:1888 对 Tailscale 内任意来源开放写端点。→ 星枢前端是唯一入口、自带控制权锁门控(第 7.3);此为既有风险面,方案不放大它(星枢只在持锁 + 护栏开时才转发 load/start)。
6. **`live.py` 现 BUG**(`live.py:679` 用 `info.get('IsRunning')` 判运行态,但 `/sequence/state` 顶层是数组、无该字段,`seq.running` 恒 False)→ 顺带修:运行态改用 `start` 的 409 或订阅 WS 事件 `SEQUENCE-STARTING/FINISHED`(第 7.2)。

### 6.6 下发前自检(只读约束内)

编译出 JSON 后,后端做静态校验再考虑发:① 合法 JSON;② 所有 `$id` 全局唯一且为字符串;③ 所有 `$ref` 可解析到某 `$id`;④ `Root.Items` 恰 3 项且类型有序;⑤ 无 `UnknownSequenceItem`(类型表白名单校验);⑥ ImageType ∈ {LIGHT,FLAT,DARK,BIAS,SNAPSHOT}。**首次真正写入**:先在空闲盒子/或落盘 `Documents\N.I.N.A\` 测试文件在 NINA UI 手动 Load 看有无 Unknown 节点(退路 A/B),确认无误再开 `POST /sequence/load`,且赤道仪 park、序列空闲、受控时段、用一份明确无 GOTO 的最小序列先试。

---

## 7. 后端 API 面

分层沿用:`api/__init__.py` 薄路由 → Gateway(`base.py` 抽象 / `sim/engine.py` / `live.py`)→ `models.py` 契约(「现有网站架构」)。新方法须 `base.py` 声明 + sim 实现 + live 映射。

### 7.1 新增端点

| 端点 | 方法 | 读/写 | 请求 | 响应 | 衔接 |
|---|---|---|---|---|---|
| `/api/designer/site` | GET | 只读 | — | `{lat,lon,elev,source}` | live→ninaAPI `GET /profile/show` 读 AstrometrySettings 缓存;sim→配置默认。供横轴/暮光 |
| `/api/designer/twilight?date=` | GET | 只读 | date | `{sunset,dusk,dawn,sunrise, civil/nautical, midnight_alt}`(ISO) | 后端 astro 算(或纯前端算,后端仅兜底);只读 |
| `/api/designer/project` | GET | 只读 | — | `Project`(最近一次保存) | 读编排工程(草稿持久化在后端 config/json,非 130apo) |
| `/api/designer/project` | POST | 写(designer 域) | `Project` | `{ok}` | 保存编排草稿;**不触碰 130apo**,只落星枢本地;走 `_guard(domain="sequence")` 协作锁 |
| `/api/designer/estimate` | POST | 只读 | `Project` 或 `Clip` | `{duration_s, breakdown{...}}` | 后端权威重算时长(与前端公式一致),不落盘 |
| `/api/designer/compile` | POST | 只读 | `Project` | `CompiledSequence`(含 overlaps/totals) | 压缩+重叠检测+生成 IR;不触设备 |
| `/api/designer/preview-nina` | POST | 只读 | `CompiledSequence` | `{json, validation:{ok,errors[]}}` | IR→NINA JSON + 6.6 自检;**不发给 NINA**,仅返回供预览/下载 |
| `/api/sequence/plan` | POST | 写(sequence 域) | `SequencePlan`(降级)或 `CompiledSequence` | `{ok}` | **复用现有端点**;live `set_plan` 现返回 unsupported(`live.py:693`),本方案改为:live 下把 IR→NINA JSON→`POST /sequence/load`(护栏 + 锁 + 空闲探测后) |
| `/api/sequence/action` | POST | 写(sequence 域) | `{action:start/stop/reset, ...}` | `{ok}` | **复用现有**(`live.py:683`);start 仍受 `allow_sequence_start` |

设计原则:`/api/designer/*` 全部是「前端编排 + 估算 + 编译」,**零设备写**(只读约束天然满足);唯一真正写 130apo 的是 `/api/sequence/plan`(=load)与 `/api/sequence/action`(=start/stop),它们走既有 `sequence` 域护栏。

### 7.2 live provider 衔接(含修 BUG)

- `base.py` 新增:`get_site()`、`estimate_duration()`、`compile_project()`、`build_nina_json()`、`load_sequence(json)`。
- `live.py`:
  - `get_site` → `GET /profile/show`(`_get` 解 `.Response` 信封)。
  - `load_sequence` → 先 **空闲探测**:`GET /sequence/start` 若返回 409「already running」即判运行中,拒绝 load;否则 `POST /sequence/load` body=NINA JSON。(注:探测不真启动——若 409 说明在跑;若非 409 需谨慎,见风险。更稳妥用 WS 事件 `SEQUENCE-STARTING/FINISHED` 维护运行态标志。)
  - **修 `live.py:679`**:`get_sequence` 不再用 `info.get('IsRunning')`(`/sequence/state` 顶层是数组无此字段);改用 WS 事件维护的 `_seq_running` 标志或 409 探测填 `seq.running`。
  - `set_plan`(`live.py:693`)由 `unsupported` 改为:`build_nina_json(IR)` → `load_sequence`(护栏 + 锁通过时)。
- `sim/engine.py`:`compile_project`/`estimate` 纯算;`load_sequence` 把 IR 降级成 `SequencePlan` 喂 `_run_sequence` 做可视化预演(开发主路径,不碰真机)。

### 7.3 控制权 Holder 衔接

- 编辑/估算/编译/预览(`/api/designer/*` 只读项)**不需要持锁**——任何监控者都能排布、看时长。
- 保存草稿 `POST /api/designer/project`、下发 `POST /api/sequence/plan`、`start/stop` → 写前 `if(!Ops.requireControl('sequence'))return;`(前端);后端 `_do_action` / `_guard(domain="sequence")` 自动校验只读硬禁 + 域解禁 + 协作锁(失败 423)。
- `start` 再叠一层 `allow_sequence_start` 服务端开关(护栏)。顶栏 `#ops-ctrl` 显示主控状态。

---

## 8. 边界与风险及应对

| 风险 | 影响 | 应对 |
|---|---|---|
| **中天翻转打断时序** | 每 Exposure 容器挂 MeridianFlipTrigger,过子午自动翻转 ~120s,实际比估时长;翻转还重新 plate-solve + RestoreGuiding。 | 时间轴对每个 LIGHT clip 用 astro 算「目标过中天时刻」,若落在 clip 窗口内,估时**+meridian_flip_s(120s)**并在条上画一道竖线标记「翻转」。提醒用户翻转后窗口右侧可能挤压后续吸附条。 |
| **目标未升起 / 已落下** | 拖到目标不可见的时段,NINA 会卡在 WaitForAltitude 或拍不到。 | 时间轴对每个 LIGHT clip 叠画**该目标高度曲线 / 可拍窗口**(高于 `WaitForAltitude.Data.Offset`,默认 30°/15° 可配);clip 落在窗口外时红描边 + 状态行警告「M42 在 22:10 尚未升过 30°」。压缩时把这类标为 warning(不阻止,但提示)。 |
| **自动对焦/抖动让实际时长漂移** | af 次数取决于实际温变;dither settle 受视宁度;估时只能近似。 | 估时表全部**可调**;clip 上显示「估 ±」区间(乐观/保守两值);文档明示时间轴是计划意图非保证。下发后用 WS 事件实时校正(运行监视页,复用 `nina-sequence.html` 进度区)。 |
| **压缩后仍重叠** | 异轨可重叠,压缩到单轨可能撞。 | `compile` 返回 overlaps,前端红描边冲突两条 + 状态行精确报「X 与 Y 在 HH:MM 重叠 N min」+「跳到冲突处」;**不生成 JSON、不下发**,强制回第二部分改。 |
| **网站算的时长 ≠ NINA 实际** | 累积误差可能让序列超出日出。 | totals 报 `fits_in_night` 与「收台于 HH:MM」;超日出则 ⚠。下发后以 NINA 实际事件为准(估时仅规划);可在最后 Exposure 容器挂 `SunAltitudeCondition`(天亮停)做安全兜底,而非依赖估时精度。 |
| **运行中 load 报 400** | NINA 在跑时灌序列失败。 | 下发前空闲探测(409)/ WS 运行态标志;运行中按钮置灰 + 提示「序列运行中,先停止再下发」。 |
| **start 绕过赤道仪护栏(撞镜/打腿)** | NINA 自主 GOTO/翻转,130apo 真机。 | `allow_sequence_start` 默认关;开放前 checklist:NINA 端中天翻转/限位/安全监视器已配;首发用无 GOTO 最小序列、park 态、受控时段。前端 start 二次确认弹层(状态行 + 显式确认)。 |
| **版本/类型不匹配静默降级** | `$type` 错→`UnknownSequenceItem`,不报错但功能丢。 | 编译器类型表白名单 + 6.6 自检;首发经退路 A/B 在 NINA UI 目检无 Unknown 节点。 |
| **草稿持久化位置** | 编排工程不应写 130apo。 | `Project` 草稿落星枢本地(`config/` 旁 json),与 130apo 隔离;只有最终 NINA JSON 才(可选)落 `Documents\N.I.N.A\`(退路 A)。 |

---

## 9. 分阶段实施计划

> 标 ⚠️ 的是**写 130apo 的操作点**,均须先在 sim / 空闲盒子验证、遵守只读约束。

| 阶段 | 交付物 | 依赖 | 写操作点 |
|---|---|---|---|
| **MVP-0 脚手架** | 新 `nina-designer.html` + NAV/PAGES 注册;空三分区布局(卡片库/时间轴/右栏);`proj` 内存对象 + render + pointer 委托 | 现有 theme/路由 | 无 |
| **MVP-1 卡片库** | 四类卡片表单 + 默认工厂 + 复制/删除;`Card` schema;framing 检索回填 RA/Dec | `GET /api/framing/search`(现成) | 无 |
| **MVP-2 横轴+暮光** | astro.py 直译 JS;日落/暮光/日出虚线;`GET /api/designer/site`(从 /profile/show 取站点) | astro.py(现成函数);ninaAPI `/profile/show`(只读) | 无(只读 GET) |
| **MVP-3 时间轴拖放+估时** | pointer 拖放生成 clip;估时公式 + 可调 overhead 表;clip 宽度=时长;双击编辑实时变长;同轨防重叠/吸附/间隔;`Clip/Track` schema;`POST /api/designer/estimate` | 估时模型 | 无 |
| **MVP-4 压缩单轨** | `POST /api/designer/compile`;多轨归并 + 重叠检测 + 回退提示;最终单轨预览 + totals + fits_in_night | MVP-3 | 无 |
| **增强-5 IR→NINA JSON(只读预览)** | 后端 `nina_seq.py` 编译器(以 seq.json 派生);`POST /api/designer/preview-nina` + 6.6 自检;前端「下载 .json」 | `/tmp/nina-seq/seq.json` 模板;`reference/nina/NINA.Sequencer/` | 无(只生成不下发) |
| **增强-6 退路验证(关键闸门)** | 把生成的 .json 在 NINA 桌面端手动 Load,目检无 Unknown 节点;校准帧/Parallel 收尾形态实测(样本全 LIGHT,需补验) | 增强-5;130apo 桌面访问 | ⚠️ 落盘 `Documents\N.I.N.A\` 测试文件(退路 A)— **先做这步才能继续** |
| **增强-7 草稿持久化 + 模板** | `GET/POST /api/designer/project`;另存为模板;`GET /api/sequence/templates` | control 锁 | 写星枢本地(非 130apo) |
| **增强-8 下发(load)** | live `set_plan` 改为 IR→JSON→`POST /sequence/load`;空闲探测;前端「生成 NINA 序列」按钮(持锁) | 增强-6 通过 | ⚠️ `POST /sequence/load` — 须 6 通过 + 空闲 + park + 受控时段 |
| **增强-9 启动+监视** | `start/stop` 复用 `/api/sequence/action`;修 `live.py:679` IsRunning BUG(改 WS 事件);运行监视区复用 nina-sequence | 增强-8;`allow_sequence_start` | ⚠️ `GET /sequence/start` — 须护栏开 + 翻转/限位/安全监视器已配 + 二次确认 |

**关键路径**:MVP-0→4 全程零写、可纯前端 + sim 完成,先把「卡片→时间轴→压缩」体验做扎实并在 sim 跑通(`sim/engine._run_sequence` 预演)。增强-6 是不可跳过的安全闸门:**未在 NINA UI 目检通过前,不开 load/start**。

---

## 10. 待用户拍板的关键取舍

1. **同轨拖放冲突策略:Ripple-insert(只把当前条排到空档/末尾)还是 Push(顶动后续条整体右移,保持彼此间隔)?** PR 默认 ripple/insert,但天文「吸附链」语义下 push 更直观(改前面自动顺移后面)。影响 4.4 `placeClip` 与吸附链级联。**建议默认 push(吸附链整体平移),Alt 拖拽切 ripple。**

2. **页面形态:新建 `nina-designer.html` 独立页,还是重构现有 `nina-sequence.html`?** 独立页风险低、可与现监视器并存(推荐 MVP 走独立页);长期可让 designer 编排 + sequence 监视。**建议:独立 `/designer` 页,sequence 页保留为运行监视。**

3. **`start` 自动执行的开放尺度:始终人工在 NINA 桌面端 Run(只用星枢生成+load),还是开放星枢一键 start(受 `allow_sequence_start` + 二次确认 + 护栏)?** 关系到 130apo 真机自动 GOTO/翻转的撞镜风险与「网站真正执行」目标的达成度。**建议:MVP 阶段只 load 不 start(退路 B 手动 Run);待 NINA 端安全机制确认 + 受控时段实测后再分级开放 start。**

4. **校准帧(DARK/FLAT/BIAS)与 Parallel 收尾的形态**:样本 seq.json 15 目标全是 LIGHT+SmartExposure,暗/平/偏置与 ParallelContainer 收尾是源码 + 少量推定。**是否在增强-6 闸门额外补一组校准帧 + 并行收尾的最小序列在 NINA UI 目检?**(建议补,避免投产才发现形态错。)

---

关键文件路径(均绝对,供落地):
- 前端新页:`/Volumes/d/WORK/asiairbridge_local/nina-web/frontend/nina-designer.html`(参照 `nina-sequence.html`、拖拽移植 `nina-mount.html`)
- 路由注册:`/Volumes/d/WORK/asiairbridge_local/nina-web/backend/app.py:25`(PAGES)、`/Volumes/d/WORK/asiairbridge_local/nina-web/frontend/nina-theme.js:49`(NAV)
- 模型扩展:`/Volumes/d/WORK/asiairbridge_local/nina-web/backend/gateway/models.py:307-344`(在 `SequenceExposure/Target/Plan` 旁新增 Card/Clip/Track/Project/CompiledSequence)
- 暮光算法源:`/Volumes/d/WORK/asiairbridge_local/nina-web/backend/gateway/sim/astro.py:8-89`(直译 JS)
- live 映射 + 待修 BUG:`/Volumes/d/WORK/asiairbridge_local/nina-web/backend/gateway/live.py:675-696`(get_sequence 的 IsRunning 误判、sequence_action 的 set_plan=unsupported)
- 新编译器:`/Volumes/d/WORK/asiairbridge_local/nina-web/backend/gateway/nina_seq.py`(新建,IR→NINA JSON)
- NINA JSON 模板/规格依据:`/tmp/nina-seq/seq.json`、`/tmp/skel.json`(已校验 64 id 骨架)、`/Volumes/d/WORK/asiairbridge_local/reference/nina/NINA.Sequencer/`、`/Volumes/d/WORK/asiairbridge_local/reference/ninaAPI/ninaAPI/WebService/V2/Application/Sequence.cs`
- 站点坐标来源:ninaAPI `GET /profile/show` → `reference/ninaAPI/ninaAPI/WebService/V2/Application/Profile.cs:75,108`