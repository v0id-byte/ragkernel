"""CAD 摄取性能测量：冷启动、单文件摄取耗时、大文件峰值内存、实体/片计数、无索引爆炸验证。

需 cad extra：`uv run --extra cad python scripts/cad_benchmark.py`
"""

from __future__ import annotations

import os
import resource
import tempfile
import time


def _rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS 返回字节，Linux 返回 KB
    return r / (1024 * 1024) if r > 10**7 else r / 1024


def main():
    os.environ.setdefault("RAGKERNEL_DATA_DIR", tempfile.mkdtemp())
    d = tempfile.mkdtemp()

    # 冷启动：重后端惰性导入耗时
    t = time.perf_counter()
    import trimesh  # noqa: F401
    t_trimesh = time.perf_counter() - t
    t = time.perf_counter()
    from ragkernel.cad import step_backend  # noqa: F401  (触发 OCP 顶层导入)
    t_ocp = time.perf_counter() - t

    # 小文件
    import trimesh
    trimesh.creation.box(extents=[10, 20, 30]).export(os.path.join(d, "small.stl"), file_type="stl")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer
    w = STEPControl_Writer()
    w.Transfer(BRepPrimAPI_MakeBox(10., 20., 30.).Shape(), STEPControl_StepModelType.STEPControl_AsIs)
    p_step = os.path.join(d, "small.step")
    w.Write(p_step)

    # 大 STL（~100MB 量级）：高分辨率球
    big = trimesh.creation.icosphere(subdivisions=8)
    p_big = os.path.join(d, "big.stl")
    big.export(p_big, file_type="stl")
    big_tris = len(big.faces)
    big_mb = os.path.getsize(p_big) / (1024 * 1024)
    del big

    from ragkernel import pipeline, store
    db = store.connect()

    def _ingest(path):
        t0 = time.perf_counter()
        res = pipeline.ingest_file(path, db=db, do_embed=False)
        return res, time.perf_counter() - t0

    r_stl, t_stl = _ingest(os.path.join(d, "small.stl"))
    r_step, t_step = _ingest(p_step)
    rss_before = _rss_mb()
    r_big, t_big = _ingest(p_big)
    rss_after = _rss_mb()

    n_ent_big = len(store.get_engineering_entities(db, r_big["document_id"]))
    n_chunk_big = db.execute("SELECT COUNT(*) FROM chunks WHERE document_id=?",
                             (r_big["document_id"],)).fetchone()[0]

    print("=== CAD 摄取性能 ===")
    print(f"冷启动 import trimesh           : {t_trimesh*1000:8.1f} ms")
    print(f"冷启动 import OCP (STEP 后端)    : {t_ocp*1000:8.1f} ms")
    print(f"小 STL 摄取 (12 三角)           : {t_stl*1000:8.1f} ms  chunks={r_stl['chunks']}")
    print(f"小 STEP 摄取 (单盒)             : {t_step*1000:8.1f} ms  chunks={r_step['chunks']}")
    print(f"大 STL 文件                    : {big_mb:8.1f} MB  三角面={big_tris}")
    print(f"大 STL 摄取耗时                 : {t_big*1000:8.1f} ms")
    print(f"进程峰值 RSS (摄取大文件后)      : {rss_after:8.1f} MB  (之前 {rss_before:.1f} MB)")
    print(f"大 STL 实体数                   : {n_ent_big}")
    print(f"大 STL chunk 数                 : {n_chunk_big}")
    print(f"chunks / 三角面 (须≈0，验无爆炸): {n_chunk_big/big_tris:.2e}")
    assert n_chunk_big < 100, "索引爆炸：chunk 数异常偏高"
    print("✓ 无每三角面/每面一 chunk 的索引爆炸")


if __name__ == "__main__":
    main()
