# Q3319: validateFetchRemoteRequest: A git-config assembled for the fetch that injects extra remote

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `validateFetchRemoteRequest` in `internal/gitaly/service/repository/fetch_remote.go` by supplying a git-config assembled for the fetch that injects extra remote config, so that remote destinations are validated before use, configured credentials never leak to an attacker host, and extracted paths stay inside the target repository is violated — specifically fetch config cannot be extended by request input — leading to ssrf to an internal endpoint, credential/auth-header disclosure to an attacker host, or tar/symlink extraction escape planting files outside the repository?

## Target
- File/function: `internal/gitaly/service/repository/fetch_remote.go` -> `validateFetchRemoteRequest`
- Entrypoint: CreateRepositoryFrom{URL,Snapshot,Bundle}, FetchRemote, FetchBundle, RestoreRepository, SetCustomHooks
- Attacker controls: remote URL, HTTP headers, redirect targets, bundle-URI location, and tar/snapshot stream contents
- Exploit idea: Supply a git-config assembled for the fetch that injects extra remote config; if `validateFetchRemoteRequest` uses it without enforcing that fetch config cannot be extended by request input, the request escapes the intended boundary.
- Invariant to test: remote destinations are validated before use, configured credentials never leak to an attacker host, and extracted paths stay inside the target repository.
- Expected Immunefi impact: (GitLab HackerOne class) SSRF to an internal endpoint, credential/auth-header disclosure to an attacker host, or tar/symlink extraction escape planting files outside the repository.
- Fast validation: Test bundleuri/fetch git-config assembly.
