# Q1185: HashRaftPartitionPath: NUL bytes or path separators encoded to survive validation

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `HashRaftPartitionPath` in `internal/gitaly/storage/path_helpers.go` by supplying NUL bytes or path separators encoded to survive validation, so that every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override is violated — specifically encoded separators do not bypass the validator — leading to arbitrary file read/write outside the storage root?

## Target
- File/function: `internal/gitaly/storage/path_helpers.go` -> `HashRaftPartitionPath`
- Entrypoint: any repository RPC carrying Repository.relative_path / storage_name
- Attacker controls: Repository.relative_path, storage_name, and pool relative_path fields
- Exploit idea: Supply NUL bytes or path separators encoded to survive validation; if `HashRaftPartitionPath` uses it without enforcing that encoded separators do not bypass the validator, the request escapes the intended boundary.
- Invariant to test: every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override.
- Expected Immunefi impact: (GitLab HackerOne class) Arbitrary file read/write outside the storage root (path traversal / storage escape) reaching another tenant's repository or host files.
- Fast validation: Fuzz over encoded relative_path bytes asserting the confinement invariant holds.
