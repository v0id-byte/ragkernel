"""归档文档绝不经 MCP 泄漏给 agent。

刻意**走 specs_and_handlers() 拿到的 handler**（MCP server 的 _call 就是从这张表取函数），
而不是直接调 Toolbox 方法——这样将来有人在 server.py 或 specs_and_handlers 里另接一条
查询通路，这些测试才会亮红。
"""

import json
import time

import pytest

from ragkernel import store
from ragkernel.tools import Toolbox

TEXT = "液压泵异响排查步骤：先查油位，再查联轴器同心度。"
Q = "液压泵异响"


@pytest.fixture
def mcp(db, doc):
    """(handlers, document_id)——文档已入库、可检索。"""
    doc_id = doc(TEXT, filename="pump.md")
    _, handlers = Toolbox(db=db).specs_and_handlers()
    return handlers, doc_id


def _archive(db, doc_id):
    store.set_archived(db, doc_id, int(time.time()))


def test_search_documents_hides_archived(db, mcp):
    handlers, doc_id = mcp
    assert "油位" in handlers["search_documents"](query=Q)

    _archive(db, doc_id)
    assert "油位" not in handlers["search_documents"](query=Q)


def test_list_documents_hides_archived(db, mcp):
    handlers, doc_id = mcp
    assert "pump.md" in handlers["list_documents"]()

    _archive(db, doc_id)
    assert "pump.md" not in handlers["list_documents"]()


def test_read_document_hides_archived(db, mcp):
    handlers, doc_id = mcp
    assert "油位" in handlers["read_document"](document_id=doc_id)

    _archive(db, doc_id)
    out = handlers["read_document"](document_id=doc_id)
    assert "油位" not in out
    # 与"文档不存在"同一句话——不做存在性预言机
    assert out == handlers["read_document"](document_id=99999)


@pytest.mark.parametrize("tool,kwargs", [
    ("search_by_category", {"query": Q, "category": "故障案例"}),
    ("search_by_field", {"query": Q, "field": "model", "value": "*"}),
])
def test_other_search_entrypoints_hide_archived(db, mcp, tool, kwargs):
    """search_documents 之外的检索入口共用 hybrid_search，同样受不变式保护。
    这些是「将来有人新增入口忘了加过滤」最可能发生的地方。"""
    handlers, doc_id = mcp
    _archive(db, doc_id)
    assert "油位" not in handlers[tool](**kwargs)


# ── CAD 结构化层：不经 hybrid_search 的独立通路 ────────────────

@pytest.fixture
def cad_doc(cad_db, ingest, fx):
    pytest.importorskip("OCP")
    doc_id = ingest("box.step")["document_id"]
    _, handlers = Toolbox(db=cad_db).specs_and_handlers()
    return handlers, doc_id


def test_cad_entities_hidden_when_archived(cad_db, cad_doc):
    """CAD 工程实体走 store.get_engineering_entities，不经 search._ACTIVE——
    它的过滤是单独加的，必须单独测，否则这条通路没有回归保护。"""
    handlers, doc_id = cad_doc
    assert json.loads(handlers["list_cad_entities"](document_id=doc_id))["total"] > 0

    _archive(cad_db, doc_id)
    assert json.loads(handlers["list_cad_entities"](document_id=doc_id))["total"] == 0


def test_cad_entity_lookup_hidden_when_archived(cad_db, cad_doc):
    handlers, doc_id = cad_doc
    uid = json.loads(handlers["list_cad_entities"](document_id=doc_id))["entities"][0]["entity_uid"]
    assert "error" not in json.loads(handlers["get_cad_entity"](document_id=doc_id, entity_uid=uid))

    _archive(cad_db, doc_id)
    out = json.loads(handlers["get_cad_entity"](document_id=doc_id, entity_uid=uid))
    assert "error" in out and "properties" not in out
