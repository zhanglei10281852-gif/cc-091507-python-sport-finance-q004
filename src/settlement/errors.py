"""错误类型。"""

from __future__ import annotations


class EngineError(Exception):
    """业务规则校验失败（400）。"""

    code = "engine_error"


class NotFound(EngineError):
    code = "not_found"


class Conflict(EngineError):
    """状态冲突，例如重复实体、违反不变量。"""

    code = "conflict"


class DuplicateReceipt(Conflict):
    """支付回执编号重复，防止重复付款。"""

    code = "duplicate_receipt"


class ApprovalRequired(Conflict):
    """超支尚未取得有效审批。"""

    code = "approval_required"


class ApprovalExpired(Conflict):
    """审批已超过截止时间，须重新发起。"""

    code = "approval_expired"
