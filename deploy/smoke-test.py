#!/usr/bin/env python3
"""冒烟测试:对运行中的服务跑一遍关键路径,确认部署正常。
用法:  python smoke-test.py [http://127.0.0.1:8788]
仅用标准库,无需依赖。
"""
import json
import sys
import time
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8788").rstrip("/")
SID = "smoke"


def call(method, path, body=None):
    data = json.dumps({**(body or {}), "session_id": SID}).encode() if method == "POST" else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        ct = r.headers.get("content-type", "")
        return r.status, (json.load(r) if "json" in ct else r.read())


def check(name, cond, detail=""):
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}{(' · ' + detail) if detail else ''}")
    return cond


def main():
    print(f"冒烟测试 → {BASE}")
    ok = True
    try:
        st, d = call("GET", "/api/status")
        ok &= check("后端在线", st == 200 and d.get("ok"), f"provider={d.get('provider')}")
    except Exception as e:
        print(f"  [FAIL] 后端不可达: {e}"); sys.exit(1)

    for dev in ("camera", "mount", "focuser", "filterwheel"):
        st, d = call("POST", f"/api/equipment/{dev}/connect")
        ok &= check(f"连接 {dev}", d.get("ok"))

    st, d = call("POST", "/api/camera/action",
                 {"action": "capture", "params": {"exposure": 1, "mode": "single"}})
    ok &= check("发起拍摄", d.get("ok"))
    time.sleep(2)
    st, d = call("GET", "/api/camera/current-image")
    img = d.get("image") or {}
    ok &= check("生成影像", bool(img), f"#{img.get('image_id')} HFR={img.get('hfr')}")

    st, raw = call("GET", "/api/camera/image")
    ok &= check("影像 PNG", st == 200 and len(raw) > 1000, f"{len(raw)} bytes")

    st, d = call("POST", "/api/mount/action",
                 {"action": "slew", "params": {"ra": 16.69, "dec": 36.46, "target_name": "M13"}})
    ok &= check("赤道仪转向", d.get("ok"))

    st, d = call("GET", "/api/conditions")
    ok &= check("天文台条件", d.get("ok"), f"日高度={d.get('sun_altitude')}° {d.get('twilight')}")

    print("\n结果:", "全部通过 ✓" if ok else "存在失败 ✗")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
