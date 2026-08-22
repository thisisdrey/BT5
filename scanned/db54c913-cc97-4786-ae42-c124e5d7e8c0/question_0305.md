# Q0305: DisconnectGitAlternates: A DisconnectGitAlternates / Link ordering that leaves stale alter

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `DisconnectGitAlternates` in `internal/gitaly/service/objectpool/alternates.go` by supplying a DisconnectGitAlternates / Link ordering that leaves stale alternates, so that objects, alternates, quarantine directories and pools serve only the repository the request is authorized for is violated — specifically alternates are consistent after link/unlink — leading to cross-repository object access: an rpc on a repo the attacker controls resolves or serves objects belonging to another user's private repository?

## Target
- File/function: `internal/gitaly/service/objectpool/alternates.go` -> `DisconnectGitAlternates`
- Entrypoint: objectpool RPCs and any RPC relying on alternates/quarantine wiring
- Attacker controls: object-pool relative_path, alternates object directories, and quarantine dirs
- Exploit idea: Supply a DisconnectGitAlternates / Link ordering that leaves stale alternates; if `DisconnectGitAlternates` uses it without enforcing that alternates are consistent after link/unlink, the request escapes the intended boundary.
- Invariant to test: objects, alternates, quarantine directories and pools serve only the repository the request is authorized for; unvetted objects stay quarantined until hooks pass.
- Expected Immunefi impact: (GitLab HackerOne class) Cross-repository object access: an RPC on a repo the attacker controls resolves or serves objects belonging to another user's private repository.
- Fast validation: Test alternates file state across link operations.
