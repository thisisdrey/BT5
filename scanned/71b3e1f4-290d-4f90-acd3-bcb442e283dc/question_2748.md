# Q2748: DeriveReplicaPath: A path whose cleaned form re-enters the root after escaping (join-after

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `DeriveReplicaPath` in `internal/gitaly/storage/repository_path.go` by supplying a path whose cleaned form re-enters the root after escaping (join-after-clean bug), so that every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override is violated — specifically cleaning happens before, not after, the join — leading to arbitrary file read/write outside the storage root?

## Target
- File/function: `internal/gitaly/storage/repository_path.go` -> `DeriveReplicaPath`
- Entrypoint: any repository RPC carrying Repository.relative_path / storage_name
- Attacker controls: Repository.relative_path, storage_name, and pool relative_path fields
- Exploit idea: Supply a path whose cleaned form re-enters the root after escaping (join-after-clean bug); if `DeriveReplicaPath` uses it without enforcing that cleaning happens before, not after, the join, the request escapes the intended boundary.
- Invariant to test: every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override.
- Expected Immunefi impact: (GitLab HackerOne class) Arbitrary file read/write outside the storage root (path traversal / storage escape) reaching another tenant's repository or host files.
- Fast validation: Test asserting order of Clean vs Join in the locator.
