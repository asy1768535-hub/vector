# v0.4 Graph Extraction Evaluation Data

All source text in this directory is synthetic. The files are safe to commit and are the only datasets permitted by
the M6 real-provider runner.

## Files

- `gold_v1.json`: exact six-Unit deterministic fixture from the v0.4 Master Plan.
- `release_v1.jsonl`: 30 fixed synthetic documents, one JSON object per line.
- `manifests/development_smoke_v1.json`: fixed first ten documents for Provider smoke.
- `manifests/release_v1.json`: complete Release Eval selection.

Validate without database, Settings or network access:

```powershell
python scripts/graph_extraction_eval.py validate `
  --manifest eval/graph_extraction/manifests/development_smoke_v1.json
python scripts/graph_extraction_eval.py validate `
  --manifest eval/graph_extraction/manifests/release_v1.json
```

Each Unit becomes one Chunk and one active Evidence row. Gold Evidence always means the exact full Unit text. Dataset
and manifest hashes use canonical parsed JSON, so Git newline conversion does not change them.
