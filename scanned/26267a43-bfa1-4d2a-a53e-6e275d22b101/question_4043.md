# Q4043: RecordRemoval: A walk that follows entries out of the intended subtree

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `RecordRemoval` in `internal/gitaly/storage/fs.go` by supplying a walk that follows entries out of the intended subtree, so that every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override is violated — specifically directory walks stay within the target repo — leading to arbitrary file read/write outside the storage root?

## Target
- File/function: `internal/gitaly/storage/fs.go` -> `RecordRemoval`
- Entrypoint: any repository RPC carrying Repository.relative_path / storage_name
- Attacker controls: Repository.relative_path, storage_name, and pool relative_path fields
- Exploit idea: Supply a walk that follows entries out of the intended subtree; if `RecordRemoval` uses it without enforcing that directory walks stay within the target repo, the request escapes the intended boundary.
- Invariant to test: every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override.
- Expected Immunefi impact: (GitLab HackerOne class) Arbitrary file read/write outside the storage root (path traversal / storage escape) reaching another tenant's repository or host files.
- Fast validation: Test walk_directory over a tree with escaping entries.
