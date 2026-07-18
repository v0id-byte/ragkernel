"""落盘文件夹监听：文件出现即自动索引（傻瓜式入口之一）。sha256 幂等防抖。"""

import time
from pathlib import Path

from . import connectors, pipeline


def run(folder: str):
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    folder = str(Path(folder).expanduser())
    exts = connectors.supported_exts()

    def _try_ingest(path: str):
        p = Path(path)
        if p.suffix.lower() not in exts or not p.is_file():
            return
        try:
            pipeline.ingest_file(p, on_stage=lambda s, d: print(f"  [{s}] {d}"))
        except Exception as e:
            print(f"  ✗ {p.name}: {type(e).__name__}: {e}")

    class Handler(FileSystemEventHandler):
        def on_created(self, event):
            if not event.is_directory:
                time.sleep(0.5)  # 等文件写完
                _try_ingest(event.src_path)

        def on_modified(self, event):
            if not event.is_directory:
                _try_ingest(event.src_path)

    print(f"监听 {folder}（{sorted(exts)}）—— 把文件丢进来即自动索引。Ctrl-C 结束。")
    obs = Observer()
    obs.schedule(Handler(), folder, recursive=True)
    obs.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        obs.stop()
    obs.join()
