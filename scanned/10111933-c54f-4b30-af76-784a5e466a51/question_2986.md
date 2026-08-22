# Q2986: RecordLink: A relative_path with a symlink component pointing outside the storage

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `RecordLink` in `internal/gitaly/storage/fs.go` by supplying a relative_path with a symlink component pointing outside the storage, so that every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override is violated — specifically symlink components cannot redirect resolution outside the storage — leading to arbitrary file read/write outside the storage root?

## Target
- File/function: `internal/gitaly/storage/fs.go` -> `RecordLink`
- Entrypoint: any repository RPC carrying Repository.relative_path / storage_name
- Attacker controls: Repository.relative_path, storage_name, and pool relative_path fields
- Exploit idea: Supply a relative_path with a symlink component pointing outside the storage; if `RecordLink` uses it without enforcing that symlink components cannot redirect resolution outside the storage, the request escapes the intended boundary.
- Invariant to test: every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override.
- Expected Immunefi impact: (GitLab HackerOne class) Arbitrary file read/write outside the storage root (path traversal / storage escape) reaching another tenant's repository or host files.
- Fast validation: Go test creating a symlinked repo dir and asserting the resolved path is confined.
