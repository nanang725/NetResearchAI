# Chroma Full Index Repair

- Status: **SUCCESS**
- Finished: `2026-11-16T09:36:10.563859+00:00`
- Demo database: `C:\Users\ranna\NetResearchAI\maintenance\demo-20261118-r1\sqlite\netresearch.db`
- Demo uploads: `C:\Users\ranna\NetResearchAI\maintenance\demo-20261118-r1\uploads`
- Demo Chroma: `C:\Users\ranna\NetResearchAI\maintenance\demo-20261118-r1\chroma_db`
- ImportJob: `25f666ec-3259-4bb8-beae-6dfcafc76acf`

## Repair result

| Metric | Before | After |
|---|---:|---:|
| SQLite Chunks | 474 | 474 |
| Chroma Vectors | 386 | 474 |
| Missing | 88 | 0 |
| Stale | 386 | 0 |
| Legacy | 384 | 0 |
| Orphan | 0 | 0 |
| Unverifiable | 0 | 0 |
| Read errors | 0 | 0 |

- Final audit consistent: `True`
- ImportJob final status: `completed`
- Items: `8/8 succeeded`, `0 failed`
- Rebuild POST count: `1`

## Papers

| Paper | Index | Chunks | Vectors | PDF |
|---:|---|---:|---:|---|
| 1 | indexed | 90 | 90 | valid |
| 2 | indexed | 49 | 49 | valid |
| 3 | indexed | 1 | 1 | valid |
| 4 | indexed | 8 | 8 | valid |
| 5 | indexed | 257 | 257 | valid |
| 6 | indexed | 16 | 16 | valid |
| 7 | indexed | 38 | 38 | valid |
| 8 | indexed | 15 | 15 | valid |

## Chroma validation

- SQLite integrity: `['ok']`
- Vectors: `474`
- Unique IDs: `474`
- Exact chunk ID set match: `True`
- Duplicate IDs: `0`
- index_fingerprint metadata: `474`
- Missing required index files: `[]`

| File | Bytes | SHA256 |
|---|---:|---|
| `maintenance/demo-20261118-r1/chroma_db/chroma.sqlite3` | 7577600 | `f44f102a75073097302dadbda7257f5cfa469eaadd38d203fe355150bcf76b8b` |
| `maintenance/demo-20261118-r1/chroma_db/ec4a224f-2e61-441c-9c48-ac95019e9b24/data_level0.bin` | 2304384 | `1930ddbcae6ca008eceb5d449bd8c5523f9dfbf3027f71ce7b30bf39683c56f2` |
| `maintenance/demo-20261118-r1/chroma_db/ec4a224f-2e61-441c-9c48-ac95019e9b24/header.bin` | 100 | `e33cab57086cd20111dd7084e6f8686e1ac486f2abfb47d9197afcdff8242af6` |
| `maintenance/demo-20261118-r1/chroma_db/ec4a224f-2e61-441c-9c48-ac95019e9b24/index_metadata.pickle` | 15910 | `e58835294aad2401148a4d4771a4e44a091f02163efa2bddf4628fc99a6605e6` |
| `maintenance/demo-20261118-r1/chroma_db/ec4a224f-2e61-441c-9c48-ac95019e9b24/length.bin` | 2176 | `e4c21ab6cfb3ac84d34a6b960ce7495694247de765734ac6e40bc381616a7b2e` |
| `maintenance/demo-20261118-r1/chroma_db/ec4a224f-2e61-441c-9c48-ac95019e9b24/link_lists.bin` | 4556 | `aebb63af45663e0af98f655a7cf067d046b70dacc1f260b99fd5bfbd25bc8178` |

## Backup

- Path: `C:\Users\ranna\NetResearchAI\maintenance\demo-20261118-r1\backups\pre-chroma-rebuild-20261116`
- DB integrity: `['ok']`
- Chroma source/backup match: `True`
- Uploads source/backup match: `True`
- Manifest file count: `21`

## Environment protection

- Formal backend/data unchanged: `True`
- Staging source unchanged: `True`
- Combined protection fingerprint unchanged: `True`

## Startup and safety

- Initial sandbox launch failed before app/DB/Chroma access: yes
- Successful server command: `backend\.venv\Scripts\python.exe -B -m uvicorn app.main:app --host 127.0.0.1 --port 8018 --log-level info`
- Launcher PID / server PID: `19280` / `19220`
- Backend stopped after validation: yes
- Historical staged_path warning records: `4`

## Warnings

- First startup attempt exited before DB/Chroma access because sandbox denied torch DLL loading; a single authorized non-sandbox retry succeeded.
- Pre-rebuild audit legacy count was 384 rather than 386 because chunk_1 and chunk_2 were stale with current schema markers; this is explained by their reported document/title/page changes.
- Four completed historical paper_import_items retain formal uploads staged_path metadata; startup safety review proved they are not queued/running and are not used by index rebuild.

## Failures

- None

## Unverified

- None
