# TEI Reranker Notes

- `RERANK_PROVIDER=tei` uses the TEI `/rerank` contract (`texts` and a bare
  array of `index`/`score` results). An explicit rerank key is required; an
  empty `RERANK_API_KEY` sends no `Authorization` header.
- Duplicate result indexes are silently deduplicated after descending-score
  sorting, retaining the first (highest-score) entry. Invalid indexes and
  non-finite scores raise a rerank error and preserve the original retrieval
  order through the existing fallback.
- Citation chunks are not merged by document ID. Their order and citation
  numbers remain aligned with the source lookup.
