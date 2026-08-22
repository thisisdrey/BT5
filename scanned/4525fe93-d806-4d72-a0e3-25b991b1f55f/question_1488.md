# Q1488: alternates: A quarantine directory reused or shared so objects leak between requests

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `alternates` in `internal/gitaly/service/objectpool/alternates.go` by supplying a quarantine directory reused or shared so objects leak between requests, so that objects, alternates, quarantine directories and pools serve only the repository the request is authorized for is violated — specifically quarantine state is per-request and isolated — leading to cross-repository object access: an rpc on a repo the attacker controls resolves or serves objects belonging to another user's private repository?

## Target
- File/function: `internal/gitaly/service/objectpool/alternates.go` -> `alternates`
- Entrypoint: objectpool RPCs and any RPC relying on alternates/quarantine wiring
- Attacker controls: object-pool relative_path, alternates object directories, and quarantine dirs
- Exploit idea: Supply a quarantine directory reused or shared so objects leak between requests; if `alternates` uses it without enforcing that quarantine state is per-request and isolated, the request escapes the intended boundary.
- Invariant to test: objects, alternates, quarantine directories and pools serve only the repository the request is authorized for; unvetted objects stay quarantined until hooks pass.
- Expected Immunefi impact: (GitLab HackerOne class) Cross-repository object access: an RPC on a repo the attacker controls resolves or serves objects belonging to another user's private repository.
- Fast validation: Test quarantine dir uniqueness/cleanup.
