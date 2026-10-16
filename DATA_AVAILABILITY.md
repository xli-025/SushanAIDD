# Data availability

## Included data

This repository includes only small test fixtures, source manifests, configuration templates, and model assets required for software verification. The docking fixtures are intended for automated smoke testing and are not manuscript evidence.

## Excluded data

The following were intentionally excluded from the public package:

- Runtime SQLite databases containing user, task, event, and settings records.
- Access logs, task logs, generated artifacts, and user-submitted inputs.
- TLS private keys, session material, invitation codes, and administrator credentials.
- Generated result directories and server-specific deployment files.
- Local TCMSP and herb-source SQLite snapshots pending confirmation of redistribution permission.

## Third-party scientific sources

The workflow can query or ingest data from sources including TCMSP, SymMap, HerbiV, PubChem, RCSB PDB, STRING, and g:Profiler. Each source remains subject to its own access conditions, license, availability, and citation requirements. Users are responsible for obtaining data lawfully and documenting the version or retrieval date used for a reproduction run.

Source manifests are included where they were available. They describe the expected inputs but do not grant redistribution rights for third-party databases.

## Reproducing manuscript results

Exact reproduction of a historical live-source run requires the source snapshots used in that run. If redistribution permission is confirmed, the authors should deposit those snapshots in a durable research-data repository and add a DOI here. If redistribution is not permitted, the authors should deposit derived, non-restricted intermediate tables sufficient to rerun docking and reverse design, together with checksums and provenance.
