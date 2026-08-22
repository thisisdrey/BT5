# Q0323: getReferenceBackend: A snapshot/tar stream with '../' or absolute entries escaping the rep

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `getReferenceBackend` in `internal/gitaly/service/repository/replicate.go` by supplying a snapshot/tar stream with '../' or absolute entries escaping the repo dir, so that remote destinations are validated before use, configured credentials never leak to an attacker host, and extracted paths stay inside the target repository is violated — specifically extracted entries stay inside the target repository — leading to ssrf to an internal endpoint, credential/auth-header disclosure to an attacker host, or tar/symlink extraction escape planting files outside the repository?

## Target
- File/function: `internal/gitaly/service/repository/replicate.go` -> `getReferenceBackend`
- Entrypoint: CreateRepositoryFrom{URL,Snapshot,Bundle}, FetchRemote, FetchBundle, RestoreRepository, SetCustomHooks
- Attacker controls: remote URL, HTTP headers, redirect targets, bundle-URI location, and tar/snapshot stream contents
- Exploit idea: Supply a snapshot/tar stream with '../' or absolute entries escaping the repo dir; if `getReferenceBackend` uses it without enforcing that extracted entries stay inside the target repository, the request escapes the intended boundary.
- Invariant to test: remote destinations are validated before use, configured credentials never leak to an attacker host, and extracted paths stay inside the target repository.
- Expected Immunefi impact: (GitLab HackerOne class) SSRF to an internal endpoint, credential/auth-header disclosure to an attacker host, or tar/symlink extraction escape planting files outside the repository.
- Fast validation: Test CreateRepositoryFromSnapshot/RestoreRepository against a hostile tar.
