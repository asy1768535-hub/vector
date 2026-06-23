"""#6 §5.1 统一资格条件 eligibility() 的纯逻辑单测（覆盖设计 §14.4 矩阵）。"""
from __future__ import annotations

import uuid
from types import SimpleNamespace as NS

from app.workers.embedder import eligibility

OP = uuid.uuid4()
OTHER_OP = uuid.uuid4()


def _job(rev=1, op=None):
    return NS(document_revision=rev, rebuild_operation_id=op)


def _doc(rev=1, deleted=None):
    return NS(current_revision=rev, deleted_at=deleted)


def _lib(state="ready", active=None, deleted_at=None):
    return NS(index_state=state, active_rebuild_operation_id=active, deleted_at=deleted_at)


def _op(status="running"):
    return NS(status=status)


# 矩阵 1：ready + 普通 job → 允许
def test_ready_normal_allowed():
    assert eligibility(_job(op=None), _doc(), _lib("ready"), None) is True


# 矩阵 2：ready + rebuild job（含失败 operation 遗留被重试）→ 拒绝
def test_ready_rebuild_job_rejected():
    assert eligibility(_job(op=OP), _doc(), _lib("ready"), _op("failed")) is False
    assert eligibility(_job(op=OP), _doc(), _lib("ready"), _op("running")) is False


# 矩阵 3：rebuilding + 当前 operation 且 running → 允许
def test_rebuilding_current_running_allowed():
    assert eligibility(_job(op=OP), _doc(), _lib("rebuilding", active=OP), _op("running")) is True


# 矩阵 4：rebuilding + 普通 / 其他 operation / preparing → 拒绝
def test_rebuilding_rejects_normal_other_preparing():
    assert eligibility(_job(op=None), _doc(), _lib("rebuilding", active=OP), None) is False
    assert eligibility(_job(op=OTHER_OP), _doc(), _lib("rebuilding", active=OP), _op("running")) is False
    assert eligibility(_job(op=OP), _doc(), _lib("rebuilding", active=OP), _op("preparing")) is False


# 矩阵 5：failed → 全拒
def test_failed_state_rejects_all():
    assert eligibility(_job(op=None), _doc(), _lib("failed"), None) is False
    assert eligibility(_job(op=OP), _doc(), _lib("failed", active=OP), _op("running")) is False


# 通用前置：已删除 / revision 不匹配 → 拒绝
def test_deleted_rejected():
    assert eligibility(_job(op=None), _doc(deleted=object()), _lib("ready"), None) is False


def test_revision_mismatch_rejected():
    assert eligibility(_job(rev=1, op=None), _doc(rev=2), _lib("ready"), None) is False


def test_deleted_library_rejected():
    # #7：库已 tombstone → 任何 job 不执行（即便 doc 未删、revision 匹配、库仍 ready）
    assert eligibility(_job(op=None), _doc(), _lib("ready", deleted_at=object()), None) is False
