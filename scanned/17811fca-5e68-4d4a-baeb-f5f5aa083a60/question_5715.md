# Q5715: UpdateHook: A forged or replayed hooks payload environment variable

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `UpdateHook` in `internal/gitaly/hook/update.go` (via the gRPC unary request) by supplying a forged or replayed hooks payload environment variable, so that reference updates on a push run through pre-receive/update/post-receive with an unforgeable hooks payload is violated — specifically the hooks payload is authenticated and non-forgeable — leading to hook/quarantine bypass on push: refs advance with objects that never passed pre-receive/update verification, or a protected-branch/access decision is skipped?

## Target
- File/function: `internal/gitaly/hook/update.go` -> `UpdateHook`
- Entrypoint: a push over smarthttp/ssh receive-pack invoking the hook manager
- Attacker controls: pushed ref updates, pushed object graph, and the hooks payload environment (via the gRPC unary request)
- Exploit idea: Supply a forged or replayed hooks payload environment variable; if `UpdateHook` uses it without enforcing that the hooks payload is authenticated and non-forgeable, the request escapes the intended boundary.
- Invariant to test: reference updates on a push run through pre-receive/update/post-receive with an unforgeable hooks payload; no path lets a ref advance while skipping them.
- Expected Immunefi impact: (GitLab HackerOne class) Hook/quarantine bypass on push: refs advance with objects that never passed pre-receive/update verification, or a protected-branch/access decision is skipped.
- Fast validation: Test HooksPayload encode/decode rejecting a tampered payload.
