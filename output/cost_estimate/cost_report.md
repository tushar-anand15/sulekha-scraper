# Gemini 3 Flash Preview — extraction cost: thinking on/off

**Pricing snapshot:** 2026-05-20 (Vertex AI Standard tier; thoughts billed at output rate)
- Input  : $0.50 / 1M tokens
- Output : $3.00 / 1M tokens
- Batch  : 50% of Standard

Corpus (UPLOADED, non-zero): **1,951,542** PDFs. p90 byte threshold = 178,186 B.

## Per-PDF averages

```
               scenario  n_samples  mean_in_per_pdf  mean_out_per_pdf  mean_thoughts_per_pdf  mean_cost_per_pdf  stratified_total_std  stratified_total_batch
            thinking ON        100          15991.0           15022.4                    NaN            0.05306               73012.0                 36506.0
thinking OFF (budget=0)        100          15958.6            2582.5                    0.0            0.01573               24106.0                 12053.0
```

## Headline

| Scenario | Standard tier | Batch tier | Mean cost/PDF |
|---|---|---|---|
| Thinking ON  | **$73,012**  (80% CI $70,195–$75,882) | $36,506 | $0.0531 |
| Thinking OFF | **$24,106**  (80% CI $22,481–$25,914) | $12,053 | $0.0157 |
| **Savings**  | **$48,906  (67%)** | $24,453 | — |

**Thinking contributes ~67% of the total cost.** Disabling it via `thinking_budget=0` cuts spend from $73,012 to $24,106 at Standard tier (or $36,506 → $12,053 at Batch).

## Per-bucket breakdown

### Thinking ON
```
       page_count        tiles        input_tokens          output_tokens          total_cost_usd       
             mean median  mean median         mean   median          mean   median           mean median
bucket                                                                                                  
median      12.68   13.0  3.60    4.0     11797.44  13094.0       9200.58   9096.5           0.03   0.03
top         22.74   22.0  6.16    6.0     20184.64  19599.0      20844.32  18049.0           0.07   0.06
```

### Thinking OFF
```
       page_count        tiles        input_tokens          output_tokens         total_cost_usd        thoughts_tokens       
             mean median  mean median         mean   median          mean  median           mean median            mean median
bucket                                                                                                                        
median      12.68   13.0  3.60    4.0     11797.44  13094.0       1870.02  1600.5           0.01   0.01             0.0    0.0
top         22.74   22.0  6.16    6.0     20119.68  19599.0       3294.94  3070.5           0.02   0.02             0.0    0.0
```

## Sanity check: pages vs bytes

- Pearson r = 0.955
- Mean pages/PDF (sample): 17.71
- Mean tiles/PDF (sample): 4.88

## Method

- 100 PDFs sampled: 50 in the median byte band (170,712–173,387 B), 50 in the top byte band (>= p95 with preference for >p99).
- Each PDF rendered at 150 DPI, split into 2×2 grid tiles (4 pages/tile), each tile sent to Gemini 3 Flash Preview (`gemini-3-flash-preview` on Vertex AI, location=`global`) with the existing `SulekhaTileExtraction` Pydantic schema as `response_schema` and the existing system prompt from `refs/prompts.py`.
- Tiles within a PDF run in parallel; 5 PDFs in flight concurrently.
- `thinking_budget=0` disables the thoughts pass for the OFF run.
- Stratified extrapolation: corpus split at p90 bytes; rows below p90 (~90%) priced like the median-bucket sample mean, rows above priced like the top-bucket sample mean. Bootstrap (2000 iter) gives the 80% CI.