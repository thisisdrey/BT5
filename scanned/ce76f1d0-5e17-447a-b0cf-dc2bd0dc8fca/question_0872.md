# Q0872: BackupCustomHooks: A remote URL pointing at an internal/link-local address (SSRF)

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `BackupCustomHooks` in `internal/gitaly/service/repository/get_custom_hooks.go` by supplying a remote URL pointing at an internal/link-local address (SSRF), so that remote destinations are validated before use, configured credentials never leak to an attacker host, and extracted paths stay inside the target repository is violated — specifically outbound fetch destinations are validated/restricted — leading to ssrf to an internal endpoint, credential/auth-header disclosure to an attacker host, or tar/symlink extraction escape planting files outside the repository?

## Target
- File/function: `internal/gitaly/service/repository/get_custom_hooks.go` -> `BackupCustomHooks`
- Entrypoint: CreateRepositoryFrom{URL,Snapshot,Bundle}, FetchRemote, FetchBundle, RestoreRepository, SetCustomHooks
- Attacker controls: remote URL, HTTP headers, redirect targets, bundle-URI location, and tar/snapshot stream contents
- Exploit idea: Supply a remote URL pointing at an internal/link-local address (SSRF); if `BackupCustomHooks` uses it without enforcing that outbound fetch destinations are validated/restricted, the request escapes the intended boundary.
- Invariant to test: remote destinations are validated before use, configured credentials never leak to an attacker host, and extracted paths stay inside the target repository.
- Expected Immunefi impact: (GitLab HackerOne class) SSRF to an internal endpoint, credential/auth-header disclosure to an attacker host, or tar/symlink extraction escape planting files outside the repository.
- Fast validation: Test FetchRemote/CreateRepositoryFromURL rejecting internal targets.
