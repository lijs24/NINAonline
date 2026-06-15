#!/usr/bin/env python3
"""启动入口:python run.py  (默认模拟引擎,局域网 0.0.0.0:8788)

环境变量:
  NINAWEB_PROVIDER=sim|live      后端模式(默认 sim 模拟引擎)
  NINAWEB_PORT=8788              端口
  NINAWEB_NINA_URL=http://...    provider=live 时的 NINA Advanced API 地址
  NINAWEB_LAT / NINAWEB_LNG      观测站点
"""
import uvicorn

from config import settings

if __name__ == "__main__":
    print(f"星枢 · NINA Web · provider={settings.provider} · "
          f"http://{settings.host}:{settings.port}/")
    uvicorn.run("app:app", host=settings.host, port=settings.port, log_level="info")
