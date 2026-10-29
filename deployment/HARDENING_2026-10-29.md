# Applied backend hardening — 29 October 2026

Applied to the backend VM only. The hypervisor was not accessed or modified.

## Deployment

- Existing `backend.service` and Compose project `backend` retained.
- Existing database volume `backend_pgvector_data` retained.
- API and PostgreSQL published on `127.0.0.1:8000` and `127.0.0.1:5432`.
- API/worker run as UID/GID 10001, with read-only root filesystems and writable
  uploads/tmp only. Reload disabled; memory, CPU, PID and log limits configured.
- Database password rotated; ignored VM `.env` updated with mode 0600. No
  credential values are included in this report or in source control.
- JWT lifetime remains 60 minutes. Existing `cihuy` credentials preserved.
- Three former built-in demo accounts deactivated, their passwords randomized,
  and sessions revoked. Nine real/retained accounts remain active. No active
  student is left assigned to an inactive demo supervisor.
- All 113 documents and 102 existing checks retained.

## Active processing limits

- Login: 5 failed attempts per account and 30 attempts per client IP / 900 s.
  Counters persist in PostgreSQL; excess requests return 429 and Retry-After.
  No additional Cloudflare setup is required. For a large campus NAT, adjust
  the IP budget deliberately while retaining the per-account limit.
- Trusted proxy: exact Docker gateway `172.18.0.1/32`; Uvicorn automatic proxy
  header rewriting disabled. Verify the gateway after recreating the network.
- File limit: 10 MiB; total multipart/request limit: 11 MiB; 4 concurrent body
  requests; 60 s body receive timeout.
- PDF: 500 pages, 2,000,000 extracted characters, 10,000 chunks; 2 concurrent
  validation subprocesses, each with a 120 s deadline.
- Queue job subprocess deadline: 1,200 s. Destructive reset disabled.

## Verification

- Local suite: 125 passed, 2 optional PostgreSQL integration tests skipped.
- Built image: 126 passed, 1 optional support integration test skipped;
  PostgreSQL concurrent login admission verified against an isolated schema.
- Public API health returned 200 from the deployed Vercel page.
- Live auth: retained admin token 200; expired/inactive-account tokens 401;
  sixth failed login 429 with Retry-After; oversized request 413.
- Existing PDF readable as UID 10001.
- Temporary isolated pipeline: real PDF validation, queued subprocess worker,
  TF-IDF/cosine comparison (100%) and six highlight annotations all passed.
  That smoke test used disposable storage/database and did not change campus
  documents or check history.

Protected pre-change backups are on the VM at
`/home/evopuki/backend-backups/hardening-20261029T071434Z/`.
The PostgreSQL dump was restored successfully into a separate temporary
database, whose table counts matched, and the temporary database was removed.
Prior images are tagged `pre-hardening-20261029T071434Z` for rollback.
