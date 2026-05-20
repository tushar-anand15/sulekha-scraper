# Sulekha PDF Extraction — Model Comparison Report

**Corpus size**: 1,951,542 UPLOADED PDFs
**Pricing snapshot**: 2026-05-20  (Vertex AI Standard tier; Batch = 50%)

## Per-run summary

| Model + schema | n | runaways | median /PDF | mean /PDF | p99 /PDF | Corpus Std | Corpus Batch |
|---|---:|---:|---:|---:|---:|---:|---:|
| **g3-flash-preview, thinking ON** | 100 | 15 | $0.0489 | $0.0531 | $0.2326 | $   103,554 | $    51,777 |
| **g3-flash-preview, no-think** | 100 | 0 | $0.0176 | $0.0167 | $0.0482 | $    32,654 | $    16,327 |
| **g2.5-flash, no-think** | 10 | 0 | $0.0102 | $0.0100 | $0.0132 | $    19,565 | $     9,782 |
| **g2.5-flash-lite, slim schema** | 5 | 3 | $0.0313 | $0.0315 | $0.0586 | $    61,402 | $    30,701 |
| **g2.5-flash, v2 single-project** | 100 | 5 | $0.0151 | $0.0331 | $0.5093 | $    64,603 | $    32,302 |
| **g3.1-flash-lite, v2 single-project** | 100 | 2 | $0.0072 | $0.0098 | $0.1091 | $    19,092 | $     9,546 |

## Key findings

- **gemini-3.1-flash-lite + v2 schema** is the cheapest stable option: ~$19k Std / $9.5k Batch raw, ~$14k Std / $7k Batch on median × N.
- **v1 multi-project schema was wrong**: every model under it hallucinated extra 'projects' (form labels, road names) because the prompt assumed PDFs were budget lists, when they are single-project forms.
- **gemini-2.5-flash-lite is not viable** at any schema — gets stuck in JSON draft-reject loops on too many PDFs.
- **gemini-2.5-flash v2** is correct on 95% of PDFs but has 5% runaways that pull mean cost up 3x.
- **DeepSeek V4** has no vision support per official docs — not usable for this pipeline.

## Cost mitigation

Adding `max_output_tokens=4096` per tile would cap any single runaway at ~$0.08, pulling corpus cost projections down to median × N for any model.

## Raw artifacts

All per-PDF parquets in `output/cost_estimate/`:

- `extraction_results_thinking.parquet` — g3-flash-preview, thinking ON (100 PDFs)
- `extraction_results_nothink.parquet` — g3-flash-preview, no-think (100 PDFs)
- `extraction_results_25flash.parquet` — g2.5-flash, no-think (10 PDFs (mini))
- `extraction_results_25flashlite.parquet` — g2.5-flash-lite, slim schema (4 PDFs (killed: runaways))
- `extraction_results_v2_25flash.parquet` — g2.5-flash, v2 single-project (100 PDFs)
- `extraction_results_v2_31flashlite.parquet` — g3.1-flash-lite, v2 single-project (100 PDFs)

Plus:
- `sample_manifest.full.v2.csv` — 100 sampled PDFs (50 median bytes + 50 top-bytes, stratified)
- `bytes_kde.png`, `pages_vs_bytes.png` — corpus distribution + sanity check
- `cost_compare.png`, `quality_compare.png` — earlier 2-way comparison charts