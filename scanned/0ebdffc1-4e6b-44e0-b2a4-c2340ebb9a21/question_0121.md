# Q0121: Env: A fetch-into-object-pool that pulls a private repo's objects into a shared pool

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `Env` in `internal/git/alternates/alternates.go` by supplying a fetch-into-object-pool that pulls a private repo's objects into a shared pool, so that objects, alternates, quarantine directories and pools serve only the repository the request is authorized for is violated — specifically pool membership does not leak private objects across members — leading to cross-repository object access: an rpc on a repo the attacker controls resolves or serves objects belonging to another user's private repository?

## Target
- File/function: `internal/git/alternates/alternates.go` -> `Env`
- Entrypoint: objectpool RPCs and any RPC relying on alternates/quarantine wiring
- Attacker controls: object-pool relative_path, alternates object directories, and quarantine dirs
- Exploit idea: Supply a fetch-into-object-pool that pulls a private repo's objects into a shared pool; if `Env` uses it without enforcing that pool membership does not leak private objects across members, the request escapes the intended boundary.
- Invariant to test: objects, alternates, quarantine directories and pools serve only the repository the request is authorized for; unvetted objects stay quarantined until hooks pass.
- Expected Immunefi impact: (GitLab HackerOne class) Cross-repository object access: an RPC on a repo the attacker controls resolves or serves objects belonging to another user's private repository.
- Fast validation: Test FetchIntoObjectPool object provenance.
