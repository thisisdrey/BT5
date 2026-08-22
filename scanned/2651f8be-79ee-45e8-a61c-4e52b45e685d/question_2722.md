# Q2722: buildCommandOpts: Custom-hooks tar (SetCustomHooks) with path traversal in entry names

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `buildCommandOpts` in `internal/gitaly/service/repository/fetch_remote.go` by supplying custom-hooks tar (SetCustomHooks) with path traversal in entry names, so that remote destinations are validated before use, configured credentials never leak to an attacker host, and extracted paths stay inside the target repository is violated — specifically hook extraction confines entry paths — leading to ssrf to an internal endpoint, credential/auth-header disclosure to an attacker host, or tar/symlink extraction escape planting files outside the repository?

## Target
- File/function: `internal/gitaly/service/repository/fetch_remote.go` -> `buildCommandOpts`
- Entrypoint: CreateRepositoryFrom{URL,Snapshot,Bundle}, FetchRemote, FetchBundle, RestoreRepository, SetCustomHooks
- Attacker controls: remote URL, HTTP headers, redirect targets, bundle-URI location, and tar/snapshot stream contents
- Exploit idea: Supply custom-hooks tar (SetCustomHooks) with path traversal in entry names; if `buildCommandOpts` uses it without enforcing that hook extraction confines entry paths, the request escapes the intended boundary.
- Invariant to test: remote destinations are validated before use, configured credentials never leak to an attacker host, and extracted paths stay inside the target repository.
- Expected Immunefi impact: (GitLab HackerOne class) SSRF to an internal endpoint, credential/auth-header disclosure to an attacker host, or tar/symlink extraction escape planting files outside the repository.
- Fast validation: Test SetCustomHooks/ExtractHooks path confinement.
