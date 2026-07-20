---
name: news-ingestion
description: Fetch and normalize news/filings text for the research pipeline — dedupe, timestamp, store. Use for building or prototyping the news/filings ingestion layer.
argument-hint: [ingestion task]
---

News ingestion task: $ARGUMENTS

- Dev-time prototyping uses the installed Firecrawl MCP; production ingestion uses a proper
  source (still an open decision — adding any PAID data source needs owner approval first).
- Every stored document carries: source URL, symbol(s), retrieved-at, and its TRUE
  published-at timestamp. Published-at is load-bearing for look-ahead hygiene — a document
  must never be available to research runs dated before it was published.
- Deduplicate by canonical URL + content hash; store cleaned text, not raw HTML.
- Normalize into the ingestion schema (see `research/tech-stack.md` §3) ready for embedding
  and for inclusion in research prompts.
- Respect robots.txt and source terms; no scraping workarounds — if a source blocks
  automated access, list it as unavailable rather than evading.
