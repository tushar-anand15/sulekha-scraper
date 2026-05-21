# Quality comparison — all models on 100 PDFs

Both v1 and v2 schemas tested. v1 was a 'list of MunicipalProject' schema (single Sulekha form gets treated as if it had many projects) — every v1 model emitted ~3-30 'projects' per single-project PDF, with hallucinated road names and form-label entries. v2 is a 'single SulekhaProject' schema with project_id/project_name_en/project_name_ml/total_cost_inr fields — extracts the actual one project per PDF.

## Per-model 'projects extracted' (per PDF)

| Model | mean | median | max | What's right |
|---|---:|---:|---:|---|
| g3-flash-preview think | 4.97 | 4.0 | 28 | 1 (over-extracts to 3-57) |
| g3-flash-preview nothink | 6.17 | 5.0 | 57 | 1 (over-extracts to 3-57) |
| g2.5-flash v2 | 1.00 | 1.0 | 1 | 1 (correct) |
| g3.1-flash-lite v2 | 1.00 | 1.0 | 1 | 1 (correct) |

## project_id capture (best objective signal)

How often the model extracted any valid Sulekha project_id (`Sxxxx/xx`):

| Model | PDFs with project_id | % |
|---|---:|---:|
| g3-flash-preview think | 0/100 | 0% |
| g3-flash-preview nothink | 0/100 | 0% |
| g2.5-flash v2 | 93/100 | 93% |
| g3.1-flash-lite v2 | 100/100 | 100% |

**v2 cross-model agreement on project_id**: 87/100 PDFs match between g2.5-flash and g3.1-flash-lite.

## Per-PDF cost stats (sample run)

| Model | mean | median | max | total spend |
|---|---:|---:|---:|---:|
| g3-flash-preview think | $0.0531 | $0.0489 | $0.2600 | $5.306 |
| g3-flash-preview nothink | $0.0167 | $0.0176 | $0.0582 | $1.673 |
| g2.5-flash v2 | $0.0331 | $0.0151 | $0.5109 | $3.310 |
| g3.1-flash-lite v2 | $0.0098 | $0.0072 | $0.1094 | $0.978 |

## Sample PDFs — first 10 rows, all models side-by-side

Shows the project_id and English name each model captured for each PDF.

| pdf_id | g3-flash-preview think (id) | g3-flash-preview nothink (id) | g2.5-flash v2 (id) | g3.1-flash-lite v2 (id) |
|---|---|---|---|---|
| 0244755e | — | — | S0001/23 | S0001/23 |
| 0391e142 | — | — | S0036/23 | S0036/23 |
| 04728dcf | — | — | S1372/21 | S1372/21 |
| 083807a2 | — | — | S0005/21 | S0005/21 |
| 0897dc47 | — | — | S0654/17 | S0654/17 |
| 0970625d | — | — | S0035/24 | S0035/24 |
| 0b14588f | — | — | S0460/20 | S0460/20 |
| 0bcf06ca | — | — | S0112/23 | S0112/23 |
| 0bf75c59 | — | — | 32 | S0032/20 |
| 1137439f | — | — | S0013/22 | S0013/22 |
| 1217429b | — | — | S0138/17 | S0138/17 |
| 1360b66a | — | — | — | S0618/21 |
| 1906f0cb | — | — | S1286/23 | S1286/23 |
| 1e34f20c | — | — | — | S0070/22 |
| 22b34271 | — | — | 9 | S0009/21 |
