"""轻量天文换算 —— 模拟引擎够用即可,不追求历表级精度。"""
from __future__ import annotations

import math
from datetime import datetime, timezone


def julian_date(dt: datetime) -> float:
    dt = dt.astimezone(timezone.utc)
    y, mo = dt.year, dt.month
    d = (dt.day + (dt.hour + (dt.minute + dt.second / 60) / 60) / 24)
    if mo <= 2:
        y -= 1
        mo += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (mo + 1)) + d + b - 1524.5


def gmst_hours(dt: datetime) -> float:
    """格林尼治平恒星时(小时)。"""
    jd = julian_date(dt)
    t = (jd - 2451545.0) / 36525.0
    gmst_deg = (280.46061837 + 360.98564736629 * (jd - 2451545.0)
                + 0.000387933 * t * t - t * t * t / 38710000.0)
    return (gmst_deg % 360.0) / 15.0


def lst_hours(longitude_deg: float, dt: datetime | None = None) -> float:
    dt = dt or datetime.now(timezone.utc)
    return (gmst_hours(dt) + longitude_deg / 15.0) % 24.0


def radec_to_altaz(ra_hours: float, dec_deg: float, lat_deg: float,
                   lon_deg: float, dt: datetime | None = None) -> tuple[float, float]:
    """返回 (altitude_deg, azimuth_deg)。方位角自北向东。"""
    lst = lst_hours(lon_deg, dt)
    ha = math.radians((lst - ra_hours) * 15.0)
    dec = math.radians(dec_deg)
    lat = math.radians(lat_deg)
    sin_alt = math.sin(dec) * math.sin(lat) + math.cos(dec) * math.cos(lat) * math.cos(ha)
    sin_alt = max(-1.0, min(1.0, sin_alt))
    alt = math.asin(sin_alt)
    cos_az = (math.sin(dec) - math.sin(alt) * math.sin(lat)) / (math.cos(alt) * math.cos(lat) + 1e-9)
    cos_az = max(-1.0, min(1.0, cos_az))
    az = math.acos(cos_az)
    if math.sin(ha) > 0:
        az = 2 * math.pi - az
    return math.degrees(alt), math.degrees(az)


def hours_to_hms(h: float) -> str:
    h = h % 24.0
    hh = int(h)
    mm = int((h - hh) * 60)
    ss = int(((h - hh) * 60 - mm) * 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def deg_to_dms(d: float) -> str:
    sign = "+" if d >= 0 else "-"
    d = abs(d)
    dd = int(d)
    mm = int((d - dd) * 60)
    ss = int(((d - dd) * 60 - mm) * 60)
    return f"{sign}{dd:02d}°{mm:02d}'{ss:02d}\""


def sun_radec(dt: datetime | None = None) -> tuple[float, float]:
    """低精度太阳赤经赤纬(小时, 度) —— 暮光判定够用。"""
    dt = dt or datetime.now(timezone.utc)
    n = julian_date(dt) - 2451545.0
    L = math.radians((280.460 + 0.9856474 * n) % 360)
    g = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = L + math.radians(1.915) * math.sin(g) + math.radians(0.020) * math.sin(2 * g)
    eps = math.radians(23.439 - 0.0000004 * n)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    return (math.degrees(ra) / 15.0) % 24.0, math.degrees(dec)


def sun_altitude(lat_deg: float, lon_deg: float, dt: datetime | None = None) -> float:
    """太阳地平高度(度)。<0 夜; -18~0 暮光; >0 白天。"""
    ra, dec = sun_radec(dt)
    alt, _ = radec_to_altaz(ra, dec, lat_deg, lon_deg, dt)
    return alt


def time_to_meridian_hours(ra_hours: float, lon_deg: float,
                           dt: datetime | None = None) -> float:
    """目标到达子午圈还有多久(小时,可为负=已过子午)。"""
    lst = lst_hours(lon_deg, dt)
    ha = (lst - ra_hours)
    # 归一到 [-12,12]
    while ha > 12:
        ha -= 24
    while ha < -12:
        ha += 24
    return -ha
