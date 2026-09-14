"""包级 CLI 入口：python -m crawler_engine --url ... --engine http

（保留 python -m crawler_engine.runner 亦可，但包入口不会触发 runpy 重复导入告警。）
"""

from crawler_engine.runner import main

if __name__ == "__main__":
    raise SystemExit(main())
