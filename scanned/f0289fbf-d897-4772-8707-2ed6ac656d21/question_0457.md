# Q0457: ComputePartition: A trailing-dot / mixed-case / Unicode-normalized path that collides with

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `ComputePartition` in `internal/gitaly/storage/path_helpers.go` by supplying a trailing-dot / mixed-case / Unicode-normalized path that collides with another repo, so that every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override is violated — specifically distinct requests never resolve to the same on-disk path — leading to arbitrary file read/write outside the storage root?

## Target
- File/function: `internal/gitaly/storage/path_helpers.go` -> `ComputePartition`
- Entrypoint: any repository RPC carrying Repository.relative_path / storage_name
- Attacker controls: Repository.relative_path, storage_name, and pool relative_path fields
- Exploit idea: Supply a trailing-dot / mixed-case / Unicode-normalized path that collides with another repo; if `ComputePartition` uses it without enforcing that distinct requests never resolve to the same on-disk path, the request escapes the intended boundary.
- Invariant to test: every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override.
- Expected Immunefi impact: (GitLab HackerOne class) Arbitrary file read/write outside the storage root (path traversal / storage escape) reaching another tenant's repository or host files.
- Fast validation: Differential test comparing normalized paths for collision.
