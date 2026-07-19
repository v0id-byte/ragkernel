"""原生 CAD 摄取（STEP/STL）。

结构化工程层（EngineeringEntity/CADDocument）+ 检索文本层（summarize→Page）双层表示。
重二进制依赖（trimesh / OpenCASCADE-OCP）在各 backend 内**惰性导入**——缺失时内核照常启动，
只有真正摄取 CAD 文件才报清晰的安装提示。见 base.py 的 provenance 三正交字段（诚实边界）。
"""
