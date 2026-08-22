# Q5879: Info: A storage_name that is unknown or aliases another storage's root

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `Info` in `internal/gitaly/storage/walk_directory.go` (via the gRPC unary request) by supplying a storage_name that is unknown or aliases another storage's root, so that every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override is violated — specifically storage_name maps only to its configured root — leading to arbitrary file read/write outside the storage root?

## Target
- File/function: `internal/gitaly/storage/walk_directory.go` -> `Info`
- Entrypoint: any repository RPC carrying Repository.relative_path / storage_name
- Attacker controls: Repository.relative_path, storage_name, and pool relative_path fields (via the gRPC unary request)
- Exploit idea: Supply a storage_name that is unknown or aliases another storage's root; if `Info` uses it without enforcing that storage_name maps only to its configured root, the request escapes the intended boundary.
- Invariant to test: every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override.
- Expected Immunefi impact: (GitLab HackerOne class) Arbitrary file read/write outside the storage root (path traversal / storage escape) reaching another tenant's repository or host files.
- Fast validation: Test with an unconfigured/duplicate storage_name asserting no cross-storage access.
