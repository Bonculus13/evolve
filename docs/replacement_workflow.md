# Replacement And Archival Workflow

1. Register an external app or script:
`python3 orchestrator.py --register-app "<name>" "<absolute_path>" "tag1,tag2,tag3"`

2. Generate overlap and replacement decisions:
`python3 orchestrator.py --replacement-report`

3. Decision policy:
- `archive_candidate` (coverage >= 0.80): plan migration and archive old app.
- `merge_candidate` (coverage >= 0.40): pull high-value pieces into evolve first.
- `keep`: no action; re-evaluate later.

4. Archive when ready:
`python3 orchestrator.py --archive-app "<name>" --reason "<why it is replaced>"`

Artifacts:
- Portfolio registry: `data/portfolio.json`
- Archive moves: `data/archive/`
- Archive log: `data/archive_log.jsonl`
