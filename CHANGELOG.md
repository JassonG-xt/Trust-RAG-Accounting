# Changelog

## [Unreleased]

## [Phase 9B] Deployment and Operations Guide

- Added deployment guide, operations runbook, and configuration reference.
- Added deploy readiness check and production-like local run helper.
- Updated maintenance, release, roadmap, README, and contributing docs for operations workflows.

## [Phase 9A] Repository Hardening and Release Hygiene

- Added contributor, security, changelog, release checklist, and maintenance documentation.
- Added GitHub pull request and issue templates.
- Added a repository hygiene script and CI check for forbidden tracked files.
- Corrected roadmap test-count drift while preserving runtime behavior.

## [Phase 8E] Provider Benchmark Trends

- Added local provider benchmark history snapshots.
- Added `/v1/provider-benchmarks/history`.
- Added dashboard trend section for provider benchmark summaries.

## [Phase 8D] Provider Benchmark Dashboard

- Added a read-only dashboard panel for local provider benchmark artifacts.
- Added provider benchmark artifact APIs.
- Kept benchmark execution manual and outside CI.

## [Phase 8C] Real Provider Benchmark Report

- Added manual provider benchmark reporting for template, mock, and optional real providers.
- Compared fallback rate, citation validation, safety preservation, and latency.
- Kept deterministic eval CI separate from provider benchmarking.

## [Phase 8B] Optional Citation-Aware LLM Generator

- Added optional real-LLM answer generation behind `LLM_ANSWER_MODE=llm`.
- Preserved deterministic template mode as the default.
- Added citation-contract validation and deterministic fallback.

## [Phase 7] Dashboard and Review Workflow

- Added the FastAPI-served reviewer dashboard.
- Added reviewer actions, state transitions, filtering, pagination, and export.
- Added historical eval trend snapshots for local dashboard inspection.

## [Phase 6] Eval Harness and CI Gate

- Added deterministic accounting eval cases and threshold policy.
- Added GitHub Actions eval gate.
- Added PR eval comment generation and regression delta.

## [Phase 5] Conditional Routing and Human Review

- Added unsafe request fast-path routing.
- Added human review handoff and local review queue.

## [Phase 3-4] Retrieval and LangChain Adapter

- Added hybrid retrieval with keyword, BM25, mock vectors, and mock reranking.
- Added LangChain `BaseRetriever` adapter and runnable retrieval nodes.
- Added local tracing hooks and runnable metadata.

## [Phase 2] Ingestion and Chunking

- Added Markdown ingestion and `DocumentRepository`.
- Added PDF and DOCX ingestion.
- Added chunk-level metadata and local JSON stores.
