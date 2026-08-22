# Q4519: Link: A temp-dir path derived from attacker fields that escapes the temp root

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `Link` in `internal/gitaly/storage/fs.go` by supplying a temp-dir path derived from attacker fields that escapes the temp root, so that every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override is violated — specifically temp paths are confined like repo paths — leading to arbitrary file read/write outside the storage root?

## Target
- File/function: `internal/gitaly/storage/fs.go` -> `Link`
- Entrypoint: any repository RPC carrying Repository.relative_path / storage_name
- Attacker controls: Repository.relative_path, storage_name, and pool relative_path fields
- Exploit idea: Supply a temp-dir path derived from attacker fields that escapes the temp root; if `Link` uses it without enforcing that temp paths are confined like repo paths, the request escapes the intended boundary.
- Invariant to test: every request-derived path resolves under the configured storage root after storage.ValidateRelativePath and locator joining, with no traversal, symlink escape, or absolute override.
- Expected Immunefi impact: (GitLab HackerOne class) Arbitrary file read/write outside the storage root (path traversal / storage escape) reaching another tenant's repository or host files.
- Fast validation: Test on tempdir creation with hostile inputs.
