# Q0293: NewHooksPayload: A ref update that reaches updateReferenceWithHooks without running update

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `NewHooksPayload` in `internal/git/gitcmd/hooks_payload.go` by supplying a ref update that reaches updateReferenceWithHooks without running update hooks, so that reference updates on a push run through pre-receive/update/post-receive with an unforgeable hooks payload is violated — specifically no update path bypasses hook invocation — leading to hook/quarantine bypass on push: refs advance with objects that never passed pre-receive/update verification, or a protected-branch/access decision is skipped?

## Target
- File/function: `internal/git/gitcmd/hooks_payload.go` -> `NewHooksPayload`
- Entrypoint: a push over smarthttp/ssh receive-pack invoking the hook manager
- Attacker controls: pushed ref updates, pushed object graph, and the hooks payload environment
- Exploit idea: Supply a ref update that reaches updateReferenceWithHooks without running update hooks; if `NewHooksPayload` uses it without enforcing that no update path bypasses hook invocation, the request escapes the intended boundary.
- Invariant to test: reference updates on a push run through pre-receive/update/post-receive with an unforgeable hooks payload; no path lets a ref advance while skipping them.
- Expected Immunefi impact: (GitLab HackerOne class) Hook/quarantine bypass on push: refs advance with objects that never passed pre-receive/update verification, or a protected-branch/access decision is skipped.
- Fast validation: Trace/test update_with_hooks call graph for a bypass.
