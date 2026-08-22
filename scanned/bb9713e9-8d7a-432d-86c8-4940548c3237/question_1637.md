# Q1637: IsPoolRepository: A relative_path containing '../' segments that normalize above the stora

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `IsPoolRepository` in `internal/gitaly/storage/repository_path.go` by supplying a relative_path containing '../' segments that normalize above the storage root, so that every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override is violated — specifically path stays under the storage root — leading to arbitrary file read/write outside the storage root?

## Target
- File/function: `internal/gitaly/storage/repository_path.go` -> `IsPoolRepository`
- Entrypoint: any repository RPC carrying Repository.relative_path / storage_name
- Attacker controls: Repository.relative_path, storage_name, and pool relative_path fields
- Exploit idea: Supply a relative_path containing '../' segments that normalize above the storage root; if `IsPoolRepository` uses it without enforcing that path stays under the storage root, the request escapes the intended boundary.
- Invariant to test: every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override.
- Expected Immunefi impact: (GitLab HackerOne class) Arbitrary file read/write outside the storage root (path traversal / storage escape) reaching another tenant's repository or host files.
- Fast validation: Go test feeding crafted relative_path into ValidateRelativePath and asserting rejection.
