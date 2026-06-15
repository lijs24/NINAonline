"""合成成像 —— 用 numpy 造一帧"看起来像深空照"的灰度图。

设计目标(给模拟引擎用):
- 焦点误差越大,星点越胖 → HFR 越大(驱动自动对焦 V 曲线);
- 曝光/增益越高,背景与星点越亮、噪声越大;
- 不同目标(seed)星场不同,部分目标叠加一团星云;
- 输出 16-bit 单色 ndarray,可拉伸成 8-bit PNG,并给出直方图与 HFR/星数估计。
"""
from __future__ import annotations

import io
import math

import numpy as np
from PIL import Image

# 预览尺寸(与"传感器"无关,固定小尺寸省 CPU)
PREVIEW_W = 1024
PREVIEW_H = 683

# 带星云的目标(按名字粗匹配)
_NEBULA_TARGETS = ("M8", "M42", "M16", "M17", "M20", "NGC", "IC", "Nebula", "星云")


def _star_positions(seed: int, n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    xs = rng.uniform(0, PREVIEW_W, n)
    ys = rng.uniform(0, PREVIEW_H, n)
    # 亮度幂律分布:少量亮星 + 大量暗星
    mags = rng.power(0.35, n)
    flux = 200 + mags * 60000
    return xs, ys, flux


def render_frame(seed: int, exposure_s: float, gain: int,
                 focus_error: float, target: str = "",
                 guide_rms: float = 0.0, temperature: float = -10.0) -> np.ndarray:
    """返回 uint16 (H,W) 数组。focus_error: 偏离最佳焦点的步数(任意单位)。"""
    h, w = PREVIEW_H, PREVIEW_W
    rng = np.random.default_rng(seed ^ int(exposure_s * 1000) ^ gain)

    # 背景:随曝光/增益升高;温度越高暗噪越大
    sky = 600 + exposure_s * 40 + gain * 8
    therm = max(0.0, (temperature + 20) * 30)
    img = np.full((h, w), sky + therm, dtype=np.float32)

    # 星点 PSF:基础 sigma 1.1px,焦点误差与导星 RMS 让它变胖
    base_sigma = 1.1 + abs(focus_error) / 90.0 + guide_rms * 0.6
    base_sigma = min(base_sigma, 9.0)

    n_stars = 280
    xs, ys, flux = _star_positions(seed, n_stars)
    flux = flux * (0.6 + exposure_s / 120.0) * (0.6 + gain / 200.0)

    # 在每颗星周围画高斯;只渲染局部窗口加速
    rad = int(max(4, base_sigma * 3))
    yy, xx = np.mgrid[-rad:rad + 1, -rad:rad + 1]
    for x, y, f in zip(xs, ys, flux):
        ix, iy = int(x), int(y)
        x0, x1 = max(0, ix - rad), min(w, ix + rad + 1)
        y0, y1 = max(0, iy - rad), min(h, iy + rad + 1)
        if x0 >= x1 or y0 >= y1:
            continue
        gx = xx[: y1 - y0, : x1 - x0] + (ix - rad) - (ix - rad)
        # 局部高斯核(用相对坐标)
        ly, lx = np.mgrid[y0 - iy: y1 - iy, x0 - ix: x1 - ix]
        psf = np.exp(-(lx * lx + ly * ly) / (2 * base_sigma * base_sigma))
        img[y0:y1, x0:x1] += f * psf

    # 星云(可选):柔和的椭圆团 + 丝缕
    if any(t.lower() in target.lower() for t in _NEBULA_TARGETS) and target:
        cx, cy = w * 0.5, h * 0.52
        gy, gx = np.mgrid[0:h, 0:w]
        r2 = ((gx - cx) / (w * 0.33)) ** 2 + ((gy - cy) / (h * 0.30)) ** 2
        neb = np.exp(-r2 * 1.6) * (3500 + exposure_s * 50)
        # 加一点结构
        neb *= (0.7 + 0.3 * np.sin((gx + gy) / 40.0))
        img += np.clip(neb, 0, None)

    # 噪声:读噪 + 散粒噪声(近似)
    img += rng.normal(0, 8 + gain * 0.05, (h, w))
    img += rng.normal(0, np.sqrt(np.clip(img, 0, None)) * 0.5)

    return np.clip(img, 0, 65535).astype(np.uint16)


def estimate_hfr_stars(focus_error: float, guide_rms: float,
                       exposure_s: float, gain: int) -> tuple[float, int]:
    """便宜的解析估计,免去真star detection。"""
    hfr = 1.4 + abs(focus_error) / 110.0 + guide_rms * 0.8
    hfr = round(min(hfr, 12.0), 2)
    # 焦点越差/曝光越短,能检出的星越少
    base = 240
    stars = int(base * math.exp(-abs(focus_error) / 1600.0)
                * min(1.0, 0.4 + exposure_s / 60.0))
    return hfr, max(3, stars)


def histogram(arr: np.ndarray, bins: int = 128) -> list[int]:
    hist, _ = np.histogram(arr, bins=bins, range=(0, 65535))
    return hist.astype(int).tolist()


def stretch_to_png(arr: np.ndarray, low_pct: float = 0.5,
                   high_pct: float = 99.7) -> bytes:
    """百分位拉伸成 8-bit PNG。"""
    lo = np.percentile(arr, low_pct)
    hi = np.percentile(arr, high_pct)
    if hi <= lo:
        hi = lo + 1
    stretched = np.clip((arr.astype(np.float32) - lo) / (hi - lo), 0, 1)
    # 轻度 gamma 提暗部
    stretched = np.power(stretched, 0.75)
    img8 = (stretched * 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(img8, mode="L").save(buf, format="PNG")
    return buf.getvalue()


def thumbnail_png(arr: np.ndarray, width: int = 240) -> bytes:
    lo, hi = np.percentile(arr, 0.5), np.percentile(arr, 99.7)
    if hi <= lo:
        hi = lo + 1
    img8 = (np.clip((arr.astype(np.float32) - lo) / (hi - lo), 0, 1) * 255).astype(np.uint8)
    im = Image.fromarray(img8, mode="L")
    h = int(width * im.height / im.width)
    im = im.resize((width, h))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=80)
    return buf.getvalue()
