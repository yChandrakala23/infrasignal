# Evaluation results

All documents, records and complaints used here are synthetic and were written by the developers. Scores show that the pipeline works on these examples. They are not an estimate of accuracy on real BBMP, KPPP or citizen data.

## Field extraction

- Table-style sample pack (extractor tuned on this): **100%** (116/116 values in 19 documents)
- Narrative set 1 (gaps fixed afterwards; not independent): **97%** (28/29 values in 6 documents)
- Narrative set 2 (never used for tuning): **66%** (19/29 values in 6 documents)

Document type accuracy: 97% over 31 documents.

## Matching (automatic links only)

| Task | Precision | Recall | F1 | TP/FP/FN |
|---|---|---|---|---|
| Document to work | 100% | 88% | 94% | 15/0/2 |
| Tender to work | 100% | 76% | 87% | 13/0/4 |
| Work to road | 100% | 75% | 86% | 15/0/5 |

Suggested hits among missed documents: 2/2. Fuzzy-only automatic links: 0.

## Clustering (85 complaints, 15 groups)

ARI 1.00, pairwise F1 100%.

| Variant | ARI | Pair F1 |
|---|---|---|
| Distance only (plain DBSCAN, 40 m) | 0.92 | 92% |
| + category rule | 1.00 | 100% |
| + category + text similarity | 1.00 | 100% |
| + category + text + time gap (full) | 1.00 | 100% |
| GPS noise 0 m | 1.00 | 100% |
| GPS noise 10 m | 1.00 | 100% |
| GPS noise 20 m | 0.88 | 89% |
| GPS noise 35 m | 0.42 | 43% |

## Alerts

- long_running: 1 alerts, 0 false positives
- multiple_reports: 14 alerts, 0 false positives
- repeat_days: 9 alerts, 0 false positives
- surge: 1 alerts, 0 false positives
- unmatched_recurring: 14 alerts, 0 false positives
- Real groups with no alert: none

## Limitations

- Extraction: the first sample pack uses 'Label: value' tables and the extractor was tuned on it, so its 100% is not independent. Narrative set 1 was used to find and fix gaps (its post-fix score is not independent either). Only narrative set 2 was never used for tuning.
- Real scanned documents (skewed, stamped, handwritten, Kannada text) were not available. OCR was tested on one clean synthetic scan. Kannada OCR is not enabled.
- The optional LLM extraction pass and the optional multilingual sentence-embedding similarity were implemented but not run against live services here. All numbers above use the rule-based and TF-IDF paths.
- Matching: a link is only automatic on an exact identifier. Name, road and description similarity only suggests. The recall figures therefore deliberately exclude fuzzy-only cases, which are reported as 'suggested hits'.
- Clustering labels were assigned by the author when generating the complaints, so the clusters are cleaner than real life. The GPS-noise stress test shows how quickly scores fall once the data gets less tidy.
- Road geometry is a small hand-placed approximation (22 roads), not a surveyed centreline. Work-to-road linking is by road name and ward because roadwork records carry no coordinates.
- Alert analysis runs without road data, so segment-based rules (road hotspot) are not exercised.
