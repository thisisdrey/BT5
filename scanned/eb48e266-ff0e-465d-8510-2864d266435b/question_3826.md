# Q3826: TempDir: An absolute path smuggled as relative_path

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `TempDir` in `internal/gitaly/config/locator.go` by supplying an absolute path smuggled as relative_path, so that every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override is violated — specifically absolute inputs are rejected, not honored — leading to arbitrary file read/write outside the storage root?

## Target
- File/function: `internal/gitaly/config/locator.go` -> `TempDir`
- Entrypoint: any repository RPC carrying Repository.relative_path / storage_name
- Attacker controls: Repository.relative_path, storage_name, and pool relative_path fields
- Exploit idea: Supply an absolute path smuggled as relative_path; if `TempDir` uses it without enforcing that absolute inputs are rejected, not honored, the request escapes the intended boundary.
- Invariant to test: every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override.
- Expected Immunefi impact: (GitLab HackerOne class) Arbitrary file read/write outside the storage root (path traversal / storage escape) reaching another tenant's repository or host files.
- Fast validation: Unit test passing '/etc/...' as relative_path and asserting error.
