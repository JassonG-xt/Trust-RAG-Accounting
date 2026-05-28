"""Query analyzer node (accounting domain, Phase 2A).

Classifies the incoming question into an accounting-specific question
type with disambiguation between HOW-questions (bookkeeping_sop) and
COMPLIANCE-questions (invoice_compliance).

Priority order:

1. ``unsafe_request`` — highest. Tax evasion, invoice fabrication, etc.
2. ``bookkeeping_sop`` — when the question explicitly asks **how** to
   book / record / classify under a 科目 / how to apply an SOP, even if
   "发票" also appears.
3. ``invoice_compliance`` — when the question is about whether something
   **can** be booked, missing description, missing 备注, 专票 vs 普票
   compliance.
4. ``reimbursement_rule`` — taxi / hotel / travel reimbursement.
5. ``tax_policy`` — VAT, 小规模纳税人, 增值税, tax treatment.
6. ``document_checklist`` — 资料 / 清单 / monthly bookkeeping inputs.
7. ``risk_review`` — explicit risk / human-review phrasing.
8. ``temporal_policy_comparison`` — secondary flag that survives
   alongside the primary type; promoted to *primary* only for explicit
   side-by-side comparison phrasing ("和 2024 一样吗" / "对比").
9. ``general_accounting_qa`` — fallback.

``needs_temporal_check`` is computed independently from the primary
type so a tax_policy question still triggers temporal validation even
when the type stays "tax_policy".
"""

from __future__ import annotations

from ..state import TrustRAGState


# -----------------------------
# Hint tables
# -----------------------------

_UNSAFE_HINTS = (
    "伪造", "假发票", "虚开",
    "隐藏收入", "少交税", "少缴税", "逃税",
    "删除凭证", "销毁凭证",
    "绕过监管", "绕过税务", "规避检查",
    "hide income", "evade tax", "fabricate invoice", "delete voucher",
    "bypass regulator",
)

_HOW_VERBS = ("怎么", "如何", "应该", "should i", "how to", "how do i")

_COMPLIANCE_VERBS = (
    "能",  # 能否 / 能直接
    "可以",
    "能否",
    "可不可以",
    "can i",
    "may i",
)

_INVOICE_COMPLIANCE_HINTS = (
    "缺失", "无服务描述", "服务描述", "备注栏", "专票", "普票",
    "missing description", "without description",
)

_BOOKKEEPING_HINTS = (
    "入账", "做账", "记账", "科目", "bookkeeping", "ledger", "凭证科目",
)

_INVOICE_BASE_HINTS = (
    "发票", "invoice",
)

_REIMBURSEMENT_HINTS = (
    "报销", "reimbursement", "差旅", "住宿费", "住宿", "打车", "taxi", "hotel",
)

_TAX_HINTS = (
    "小规模纳税人", "纳税人", "vat", "增值税", "tax policy", "税务", "税收",
)
_TAX_HINTS_LOWER_ONLY = ("tax",)  # avoid Chinese "税" false positives like "税务" already covered

_CHECKLIST_HINTS = (
    "清单", "资料", "checklist", "材料清单", "月度记账", "month-end",
    "monthly bookkeeping", "bank statement",
)

_RISK_HINTS = (
    "风险", "risk", "复核", "human review", "manual review", "审核",
)

_TEMPORAL_HINTS = (
    "现在", "当前", "今年", "去年", "今天", "today",
    "2024", "2025", "2026",
    "旧规则", "新规则", "还有效", "still valid",
    "以前", "之前", "previously",
)

_COMPARISON_HINTS = (
    "一样", "区别", "比较", "对比", "vs", "compare",
)


# -----------------------------
# Helpers
# -----------------------------


def _has_any(text: str, lowered: str, hints: tuple[str, ...]) -> bool:
    return any(h in text or h in lowered for h in hints)


def _has_any_lower(lowered: str, hints: tuple[str, ...]) -> bool:
    return any(h in lowered for h in hints)


# -----------------------------
# Entry point
# -----------------------------


def query_analyzer(state: TrustRAGState) -> dict:
    question = (state.get("question") or "").strip()
    if not question:
        return {
            "question_type": "empty",
            "domain": "accounting",
            "needs_temporal_check": False,
            "needs_safety_check": True,
            "routing_decision": "standard_rag",
            "routing_reason": "default_standard_rag",
            "visited_nodes": ["query_analyzer"],
            "errors": ["empty question"],
        }

    lower = question.lower()

    # 1. Highest priority: unsafe intent → Phase 5A fast-path routing.
    if _has_any(question, lower, _UNSAFE_HINTS):
        return {
            "question_type": "unsafe_request",
            "domain": "accounting",
            "needs_temporal_check": False,
            "needs_safety_check": True,
            "routing_decision": "unsafe_fast_path",
            "routing_reason": "question_type=unsafe_request",
            "visited_nodes": ["query_analyzer"],
        }

    has_how_verb = _has_any_lower(lower, _HOW_VERBS) or any(h in question for h in _HOW_VERBS)
    has_compliance_verb = _has_any_lower(lower, _COMPLIANCE_VERBS) or any(
        h in question for h in _COMPLIANCE_VERBS
    )
    has_bookkeeping_kw = _has_any(question, lower, _BOOKKEEPING_HINTS)
    has_invoice_kw = _has_any(question, lower, _INVOICE_BASE_HINTS)
    has_invoice_compliance_kw = _has_any(question, lower, _INVOICE_COMPLIANCE_HINTS)
    has_reimbursement_kw = _has_any(question, lower, _REIMBURSEMENT_HINTS)
    has_checklist_kw = _has_any(question, lower, _CHECKLIST_HINTS)
    has_risk_kw = _has_any(question, lower, _RISK_HINTS)
    has_tax_kw = _has_any(question, lower, _TAX_HINTS) or _has_any_lower(lower, _TAX_HINTS_LOWER_ONLY)
    is_temporal = _has_any(question, lower, _TEMPORAL_HINTS)
    is_comparison = _has_any(question, lower, _COMPARISON_HINTS)

    # 2. bookkeeping_sop wins when HOW + (bookkeeping or invoice).
    # "Alpha Trading Co. 的餐饮发票应该怎么入账？" → here.
    if has_how_verb and (has_bookkeeping_kw or has_invoice_kw):
        question_type = "bookkeeping_sop"
    # 3. invoice_compliance — explicit compliance signal or compliance verb
    #    on an invoice / bookkeeping question.
    elif has_invoice_compliance_kw or (
        has_compliance_verb and (has_invoice_kw or has_bookkeeping_kw)
    ):
        question_type = "invoice_compliance"
    # 4. reimbursement.
    elif has_reimbursement_kw:
        question_type = "reimbursement_rule"
    # 5. tax.
    elif has_tax_kw:
        question_type = "tax_policy"
    # 6. checklist.
    elif has_checklist_kw:
        question_type = "document_checklist"
    # 7. bare bookkeeping / invoice without disambiguators.
    elif has_bookkeeping_kw:
        question_type = "bookkeeping_sop"
    elif has_invoice_kw:
        question_type = "invoice_compliance"
    # 8. risk review.
    elif has_risk_kw:
        question_type = "risk_review"
    else:
        question_type = "general_accounting_qa"

    # ``needs_temporal_check`` is a secondary flag — temporal validation
    # still runs regardless of primary type.
    needs_temporal_check = is_temporal or question_type in {
        "reimbursement_rule",
        "tax_policy",
        "invoice_compliance",
        "bookkeeping_sop",
    }

    # Promote to temporal_policy_comparison ONLY for explicit comparison
    # phrasing; otherwise leave the primary type intact.
    if is_temporal and is_comparison:
        question_type = "temporal_policy_comparison"

    return {
        "question_type": question_type,
        "domain": "accounting",
        "needs_temporal_check": needs_temporal_check,
        "needs_safety_check": True,
        "routing_decision": "standard_rag",
        "routing_reason": "default_standard_rag",
        "visited_nodes": ["query_analyzer"],
    }
