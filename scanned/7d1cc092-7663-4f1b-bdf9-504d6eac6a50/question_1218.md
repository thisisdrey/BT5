# Q1218: poolForRequest: Alternates entries with '../' escaping the objects directory

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `poolForRequest` in `internal/gitaly/service/objectpool/util.go` by supplying alternates entries with '../' escaping the objects directory, so that objects, alternates, quarantine directories and pools serve only the repository the request is authorized for is violated — specifically alternates entries are confined to valid object dirs — leading to cross-repository object access: an rpc on a repo the attacker controls resolves or serves objects belonging to another user's private repository?

## Target
- File/function: `internal/gitaly/service/objectpool/util.go` -> `poolForRequest`
- Entrypoint: objectpool RPCs and any RPC relying on alternates/quarantine wiring
- Attacker controls: object-pool relative_path, alternates object directories, and quarantine dirs
- Exploit idea: Supply alternates entries with '../' escaping the objects directory; if `poolForRequest` uses it without enforcing that alternates entries are confined to valid object dirs, the request escapes the intended boundary.
- Invariant to test: objects, alternates, quarantine directories and pools serve only the repository the request is authorized for; unvetted objects stay quarantined until hooks pass.
- Expected Immunefi impact: (GitLab HackerOne class) Cross-repository object access: an RPC on a repo the attacker controls resolves or serves objects belonging to another user's private repository.
- Fast validation: Test alternates path confinement.
