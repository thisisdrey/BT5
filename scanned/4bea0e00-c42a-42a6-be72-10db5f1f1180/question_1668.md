# Q1668: validateFetchIntoObjectPoolRequest: An object-pool relative_path escaping into a non-pool 

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `validateFetchIntoObjectPoolRequest` in `internal/gitaly/service/objectpool/fetch_into_object_pool.go` by supplying an object-pool relative_path escaping into a non-pool repository, so that objects, alternates, quarantine directories and pools serve only the repository the request is authorized for is violated — specifically pool paths are validated and confined — leading to cross-repository object access: an rpc on a repo the attacker controls resolves or serves objects belonging to another user's private repository?

## Target
- File/function: `internal/gitaly/service/objectpool/fetch_into_object_pool.go` -> `validateFetchIntoObjectPoolRequest`
- Entrypoint: objectpool RPCs and any RPC relying on alternates/quarantine wiring
- Attacker controls: object-pool relative_path, alternates object directories, and quarantine dirs
- Exploit idea: Supply an object-pool relative_path escaping into a non-pool repository; if `validateFetchIntoObjectPoolRequest` uses it without enforcing that pool paths are validated and confined, the request escapes the intended boundary.
- Invariant to test: objects, alternates, quarantine directories and pools serve only the repository the request is authorized for; unvetted objects stay quarantined until hooks pass.
- Expected Immunefi impact: (GitLab HackerOne class) Cross-repository object access: an RPC on a repo the attacker controls resolves or serves objects belonging to another user's private repository.
- Fast validation: Test pool path validation.
