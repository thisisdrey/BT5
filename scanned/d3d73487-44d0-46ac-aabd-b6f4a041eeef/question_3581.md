# Q3581: VoteRepository: A tar entry that is a symlink later written through to escape the repo

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `VoteRepository` in `internal/gitaly/repoutil/create.go` by supplying a tar entry that is a symlink later written through to escape the repo, so that remote destinations are validated before use, configured credentials never leak to an attacker host, and extracted paths stay inside the target repository is violated — specifically symlinked extraction targets cannot escape — leading to ssrf to an internal endpoint, credential/auth-header disclosure to an attacker host, or tar/symlink extraction escape planting files outside the repository?

## Target
- File/function: `internal/gitaly/repoutil/create.go` -> `VoteRepository`
- Entrypoint: CreateRepositoryFrom{URL,Snapshot,Bundle}, FetchRemote, FetchBundle, RestoreRepository, SetCustomHooks
- Attacker controls: remote URL, HTTP headers, redirect targets, bundle-URI location, and tar/snapshot stream contents
- Exploit idea: Supply a tar entry that is a symlink later written through to escape the repo; if `VoteRepository` uses it without enforcing that symlinked extraction targets cannot escape, the request escapes the intended boundary.
- Invariant to test: remote destinations are validated before use, configured credentials never leak to an attacker host, and extracted paths stay inside the target repository.
- Expected Immunefi impact: (GitLab HackerOne class) SSRF to an internal endpoint, credential/auth-header disclosure to an attacker host, or tar/symlink extraction escape planting files outside the repository.
- Fast validation: Test extraction symlink handling.
