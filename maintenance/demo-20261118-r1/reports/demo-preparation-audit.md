# Demo Preparation Audit

- Status: **READY**
- Started: `2026-11-16T09:21:54.605290+00:00`
- Finished: `2026-11-16T09:21:55.648704+00:00`
- Demo DB: `C:\Users\ranna\NetResearchAI\maintenance\demo-20261118-r1\sqlite\netresearch.db`
- Backup: `C:\Users\ranna\NetResearchAI\maintenance\demo-20261118-r1\sqlite\netresearch.pre-path-migration.db`

## Database

- integrity_check: `['ok']`
- foreign_key_check: `[]`
- Backup logical match: `True`

| Table | Records |
|---|---:|
| `citation_edges` | 0 |
| `citation_references` | 252 |
| `paper_chunks` | 474 |
| `paper_element_backfill_state` | 8 |
| `paper_elements` | 1847 |
| `paper_import_items` | 4 |
| `paper_import_jobs` | 2 |
| `paper_pages` | 67 |
| `paper_versions` | 0 |
| `papers` | 8 |
| `schema_migrations` | 1 |

## Paper migration

| ID | Old path | New path | PDF SHA256 | Bytes |
|---:|---|---|---|---:|
| 1 | `data\uploads\bdfaa68d8984f0dc02beaca527b76f207d99b666d31d1da728ee0728182df697.pdf` | `C:\Users\ranna\NetResearchAI\maintenance\demo-20261118-r1\uploads\bdfaa68d8984f0dc02beaca527b76f207d99b666d31d1da728ee0728182df697.pdf` | `bdfaa68d8984f0dc02beaca527b76f207d99b666d31d1da728ee0728182df697` | 2215244 |
| 2 | `data\uploads\12b06565d00bb4a6ea38c0699430f78a3e59662e30d503f8e88f4c4c4bd12de8.pdf` | `C:\Users\ranna\NetResearchAI\maintenance\demo-20261118-r1\uploads\12b06565d00bb4a6ea38c0699430f78a3e59662e30d503f8e88f4c4c4bd12de8.pdf` | `12b06565d00bb4a6ea38c0699430f78a3e59662e30d503f8e88f4c4c4bd12de8` | 1046705 |
| 3 | `C:\Users\ranna\NetResearchAI\backend\data\uploads\f3057a4391f2f04fa39ccbac7cb01eec9977b8067ad1fc61c00ec6f3591068e5.pdf` | `C:\Users\ranna\NetResearchAI\maintenance\demo-20261118-r1\uploads\f3057a4391f2f04fa39ccbac7cb01eec9977b8067ad1fc61c00ec6f3591068e5.pdf` | `f3057a4391f2f04fa39ccbac7cb01eec9977b8067ad1fc61c00ec6f3591068e5` | 1186 |
| 4 | `C:\Users\ranna\NetResearchAI\backend\data\uploads\8c0d7e3cea2d00b5ffe72242bdd55734da9c8940ebec29d6c260ba53f050d292.pdf` | `C:\Users\ranna\NetResearchAI\maintenance\demo-20261118-r1\uploads\8c0d7e3cea2d00b5ffe72242bdd55734da9c8940ebec29d6c260ba53f050d292.pdf` | `8c0d7e3cea2d00b5ffe72242bdd55734da9c8940ebec29d6c260ba53f050d292` | 112229 |
| 5 | `C:\Users\ranna\NetResearchAI\backend\data\uploads\2b0579ef6732ea9374f94184c38c6817c599c03e37dc7375757b79f0934c6de6.pdf` | `C:\Users\ranna\NetResearchAI\maintenance\demo-20261118-r1\uploads\2b0579ef6732ea9374f94184c38c6817c599c03e37dc7375757b79f0934c6de6.pdf` | `2b0579ef6732ea9374f94184c38c6817c599c03e37dc7375757b79f0934c6de6` | 9317661 |
| 6 | `C:\Users\ranna\NetResearchAI\backend\data\uploads\b9ecd17c7063ca59834d9b2ed67d09ae87a5973ab02ec173799e51d452885bd8.pdf` | `C:\Users\ranna\NetResearchAI\maintenance\demo-20261118-r1\uploads\b9ecd17c7063ca59834d9b2ed67d09ae87a5973ab02ec173799e51d452885bd8.pdf` | `b9ecd17c7063ca59834d9b2ed67d09ae87a5973ab02ec173799e51d452885bd8` | 782150 |
| 7 | `C:\Users\ranna\NetResearchAI\backend\data\uploads\a5c79a4d67f8da8b7728d553f21d360f65dce49b2608b2c4d120bd7085d53a42.pdf` | `C:\Users\ranna\NetResearchAI\maintenance\demo-20261118-r1\uploads\a5c79a4d67f8da8b7728d553f21d360f65dce49b2608b2c4d120bd7085d53a42.pdf` | `a5c79a4d67f8da8b7728d553f21d360f65dce49b2608b2c4d120bd7085d53a42` | 618599 |
| 8 | `C:\Users\ranna\NetResearchAI\backend\data\uploads\5ed703cf3add6a537e83dd8712edc99196b963f28697f61e14cbcd80b938b25e.pdf` | `C:\Users\ranna\NetResearchAI\maintenance\demo-20261118-r1\uploads\5ed703cf3add6a537e83dd8712edc99196b963f28697f61e14cbcd80b938b25e.pdf` | `5ed703cf3add6a537e83dd8712edc99196b963f28697f61e14cbcd80b938b25e` | 1498953 |

### paper_id=2 disambiguation

- stored_filename: `12b06565d00bb4a6ea38c0699430f78a3e59662e30d503f8e88f4c4c4bd12de8.pdf`
- selected: `C:\Users\ranna\NetResearchAI\maintenance\demo-20261118-r1\uploads\12b06565d00bb4a6ea38c0699430f78a3e59662e30d503f8e88f4c4c4bd12de8.pdf`
- SHA256 candidates: `3`
- basis: `stored_filename+old_file_path_basename+size under authorized paper_id=2 special case`

## Chroma

- Vector count: `386`
- SQLite integrity: `['ok']`
- Missing index files: `[]`

## Environment protection

- Formal backend/data unchanged: `True`
- Staging source unchanged: `True`

## Warnings

- 4 completed paper_import_items retain historical staged_path metadata pointing to formal uploads; these were intentionally not modified.

## Failures

- None
