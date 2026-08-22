# Q0490: ExtractPool: A member repo reading pooled objects it was never granted

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `ExtractPool` in `internal/gitaly/service/objectpool/util.go` by supplying a member repo reading pooled objects it was never granted, so that objects, alternates, quarantine directories and pools serve only the repository the request is authorized for is violated — specifically pooled objects are only reachable by authorized members — leading to cross-repository object access: an rpc on a repo the attacker controls resolves or serves objects belonging to another user's private repository?

## Target
- File/function: `internal/gitaly/service/objectpool/util.go` -> `ExtractPool`
- Entrypoint: objectpool RPCs and any RPC relying on alternates/quarantine wiring
- Attacker controls: object-pool relative_path, alternates object directories, and quarantine dirs
- Exploit idea: Supply a member repo reading pooled objects it was never granted; if `ExtractPool` uses it without enforcing that pooled objects are only reachable by authorized members, the request escapes the intended boundary.
- Invariant to test: objects, alternates, quarantine directories and pools serve only the repository the request is authorized for; unvetted objects stay quarantined until hooks pass.
- Expected Immunefi impact: (GitLab HackerOne class) Cross-repository object access: an RPC on a repo the attacker controls resolves or serves objects belonging to another user's private repository.
- Fast validation: Cross-repo read test through the pool.
