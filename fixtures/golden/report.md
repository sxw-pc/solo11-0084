# ReleaseHarbor release intake report

- Run: `run-20260115T120000Z-2db77088`
- Status: **partial**
- Started: 2026-01-15T12:00:00Z
- Finished: 2026-01-15T12:00:00Z
- Datasets / versions / artifacts: 3 / 5 / 9
- Verified / skipped / quarantined / failed: 8 / 0 / 1 / 0
- Blocked targets: 0

> All names, payloads, checksums, and URLs in this report are synthetic fixtures.

## Neighborhood Air Quality Samples (`air-quality`)

Northline Air Quality Collective | CC-BY-4.0 (synthetic fixture) | http://127.0.0.1:18765/datasets/air-quality

### January 2025 sample pack (`2025-01`, stable)

| Artifact | Status | Expected bytes | Actual bytes | Attempts |
|---|---|---:|---:|---:|
| samples-readme.md | verified | 159 | 159 | 1 |
| samples.csv | checksum_mismatch | 94 | 94 | 1 |
| samples.jsonl | verified | 196 | 196 | 1 |

## Civic Budget Tidy Tables (`civic-budget`)

Aurora Municipal Data Office | CC-BY-4.0 (synthetic fixture) | http://127.0.0.1:18765/datasets/civic-budget

### 2025 Q1 stable (`2025.1`, stable)

| Artifact | Status | Expected bytes | Actual bytes | Attempts |
|---|---|---:|---:|---:|
| budget_lines.csv | verified | 112 | 112 | 1 |
| budget_notes.txt | verified | 157 | 157 | 2 |

### 2025 Q2 beta preview (`2025.2-beta`, beta)

| Artifact | Status | Expected bytes | Actual bytes | Attempts |
|---|---|---:|---:|---:|
| budget_preview.csv | verified | 97 | 97 | 1 |

## Commuter Counts by Station (`transit-counts`)

Rivergate Transit Analytics Unit | CC0-1.0 (synthetic fixture) | http://127.0.0.1:18765/datasets/transit-counts

### Version 3 counts (`v3`, stable)

| Artifact | Status | Expected bytes | Actual bytes | Attempts |
|---|---|---:|---:|---:|
| stations-manifest.txt | verified | 50 | 50 | 1 |
| stations.csv | verified | 123 | 123 | 1 |

### Version 3 label pack (`v3-labels`, stable)

| Artifact | Status | Expected bytes | Actual bytes | Attempts |
|---|---|---:|---:|---:|
| station_labels.csv | verified | 85 | 85 | 1 |

End of deterministic fixture report.
