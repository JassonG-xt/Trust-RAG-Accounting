"""Bilingual lightweight tokenizer for the accounting corpus.

The accounting corpus mixes English (Alpha Trading Co., Beta Catering
Ltd., bookkeeping_sop) with Chinese (业务招待费, 增值税, 配送费). A
real production pipeline would use jieba / pkuseg / cl100k_base; we
explicitly stay dependency-free because:

1. The vocabulary is tiny (a few hundred unique terms).
2. The domain mapping is curated (餐饮 → meal / entertainment), which
   is more reliable than a generic Chinese segmenter that does not
   know about 业务招待费.
3. Phase 3B will swap this out behind the same interface for an
   embedding tokenizer; until then, deterministic local tokenization
   keeps the test suite fast and reproducible.

Two functions are exported:

* :func:`tokenize` — emit *surface tokens* (lowercase English alnum
  runs + matched Chinese terms + character bigrams over unmatched
  Chinese runs).
* :func:`expand_query_terms` — emit a superset of tokens that also
  includes the English equivalent of every Chinese term that hit the
  domain dictionary. This is what the BM25 / keyword retrievers index
  against, so a Chinese query naturally hits English-language chunks.

Both are pure: same input → same output.
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Domain dictionary
# ---------------------------------------------------------------------------
#
# Curated mapping from Chinese accounting terms to their English-language
# counterparts found in the corpus. Adding an entry here is the canonical
# way to fix a "Chinese query missed an English chunk" failure.

_ACCOUNTING_EXPANSIONS: dict[str, list[str]] = {
    "入账": ["bookkeeping", "record", "recorded", "accounting", "book"],
    "做账": ["bookkeeping", "accounting"],
    "记账": ["bookkeeping", "accounting"],
    "科目": ["account", "expense", "expenses"],
    "餐饮": ["meal", "meals", "entertainment"],
    "业务招待": ["business", "entertainment", "expenses"],
    "招待费": ["entertainment", "expenses"],
    "招待": ["entertainment"],
    "发票": ["invoice", "invoices"],
    "专票": ["special", "vat", "invoice"],
    "普票": ["ordinary", "invoice"],
    "备注": ["remark", "remarks", "description"],
    "服务描述": ["service", "description"],
    "服务费": ["service", "fee"],
    "配送": ["delivery", "service"],
    "配送费": ["delivery", "fee"],
    "报销": ["reimbursement", "reimbursed"],
    "差旅": ["travel"],
    "住宿": ["hotel", "lodging"],
    "打车": ["taxi"],
    "审批": ["approval", "manager"],
    "增值税": ["vat", "tax"],
    "小规模纳税人": ["small-scale", "taxpayer", "small", "scale"],
    "纳税人": ["taxpayer"],
    "资料": ["document", "documents", "checklist"],
    "清单": ["checklist"],
    "银行流水": ["bank", "statements", "statement"],
    "流水": ["statements"],
    "审核": ["review", "manual"],
    "复核": ["review", "manual"],
    "公司": ["company", "ltd", "co"],
    "客户": ["client"],
    "现在": ["now", "current"],
    "超过": ["over", "above", "exceed"],
    "需要": ["require", "requires", "required"],
    "怎么": ["how"],
    "如何": ["how"],
    "应该": ["should"],
    "处理": ["process", "handle", "treat"],
    "缺失": ["missing"],
    "明确": ["clear", "explicit"],
    "直接": ["direct", "directly"],
    "原始": ["original"],
    "凭证": ["voucher", "receipt"],
    "餐饮发票": ["meal", "invoice", "entertainment"],
    "餐饮费": ["meal", "expenses"],
    "餐费": ["meal", "expenses"],
    "酒店": ["hotel"],
    "出租车": ["taxi"],
    "审计": ["audit"],
    "合规": ["compliance", "compliant"],
}


# Cache: sort keys longest-first so longest-match wins ("业务招待费" before
# "招待"). This is essential — otherwise the segmenter would emit
# "招待" for a query containing "业务招待费" and the more specific
# expansion would never fire.
_EXPANSION_KEYS_BY_LENGTH = sorted(
    _ACCOUNTING_EXPANSIONS.keys(), key=len, reverse=True
)


# ---------------------------------------------------------------------------
# Regex primitives
# ---------------------------------------------------------------------------

# An "English alnum run" is one or more ASCII letters/digits/underscore, with
# optional internal hyphens. We don't care about Unicode letters here —
# any non-ASCII letter gets handled by the Chinese pass below.
_EN_ALNUM = re.compile(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*")

# A "Chinese run" is one or more CJK Unified Ideographs.
_CJK_RUN = re.compile(r"[一-鿿]+")


_STOP_WORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "to",
        "in",
        "on",
        "is",
        "are",
        "be",
        "and",
        "or",
        "for",
        "with",
        "by",
        "as",
        "at",
        "from",
        "this",
        "that",
        "it",
        "co",
        "ltd",
    }
)


def _english_tokens(text: str) -> list[str]:
    """Lowercase + alnum-split + stop-word filter."""

    raw = _EN_ALNUM.findall(text.lower())
    out: list[str] = []
    for tok in raw:
        if tok in _STOP_WORDS:
            continue
        if tok.isdigit() and len(tok) <= 1:
            continue
        out.append(tok)
    return out


def _chinese_dictionary_matches(text: str) -> list[str]:
    """Return matched dictionary keys (longest-first non-overlapping)."""

    matches: list[str] = []
    for run in _CJK_RUN.findall(text):
        i = 0
        n = len(run)
        while i < n:
            matched = False
            for key in _EXPANSION_KEYS_BY_LENGTH:
                if run.startswith(key, i):
                    matches.append(key)
                    i += len(key)
                    matched = True
                    break
            if not matched:
                # Advance one char so we don't infinite-loop on unmatched
                # Chinese. Bigrams are added separately by tokenize().
                i += 1
    return matches


def _chinese_bigrams(text: str) -> list[str]:
    """Character bigrams over each CJK run.

    Provides recall for queries whose terms aren't in the dictionary.
    The bigrams are cheap to index and bounded in count.
    """

    bigrams: list[str] = []
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            bigrams.append(run)
            continue
        for i in range(len(run) - 1):
            bigrams.append(run[i : i + 2])
    return bigrams


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def tokenize(text: str) -> list[str]:
    """Emit surface tokens used for *indexing*.

    Surface tokens are what BM25 indexes against. They don't include
    query-side English expansions of Chinese keys — that's the job of
    :func:`expand_query_terms` (which calls this first and then layers
    the expansions on top).
    """

    if not text:
        return []

    tokens: list[str] = []
    tokens.extend(_english_tokens(text))
    tokens.extend(_chinese_dictionary_matches(text))
    tokens.extend(_chinese_bigrams(text))
    return tokens


def expand_query_terms(query: str) -> list[str]:
    """Emit indexing tokens + cross-lingual expansions.

    For each Chinese term in the query that lives in the domain
    dictionary, its English expansion words are appended. So a query
    containing "餐饮" gets ``meal``, ``entertainment`` appended,
    letting BM25 score against English-only chunks.

    Order of emission is stable across runs: original tokens first,
    expansions appended at the end in dictionary-iteration order.
    Stop-words are not re-filtered (expansions are curated; they
    contain useful single-word terms like "vat").
    """

    base = tokenize(query)
    expansions: list[str] = []
    seen_keys: set[str] = set()
    for key in _chinese_dictionary_matches(query):
        if key in seen_keys:
            continue
        seen_keys.add(key)
        expansions.extend(_ACCOUNTING_EXPANSIONS[key])
    return base + expansions
