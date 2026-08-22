# Q5628: updateReferenceWithHooks: A receive-pack push crafted so the pre-receive hook is skipped o

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `updateReferenceWithHooks` in `internal/gitaly/service/operations/update_with_hooks.go` (via the gRPC unary request) by supplying a receive-pack push crafted so the pre-receive hook is skipped or its failure ignored, so that reference updates on a push run through pre-receive/update/post-receive with an unforgeable hooks payload is violated — specifically every ref update is gated by a successful pre-receive — leading to hook/quarantine bypass on push: refs advance with objects that never passed pre-receive/update verification, or a protected-branch/access decision is skipped?

## Target
- File/function: `internal/gitaly/service/operations/update_with_hooks.go` -> `updateReferenceWithHooks`
- Entrypoint: a push over smarthttp/ssh receive-pack invoking the hook manager
- Attacker controls: pushed ref updates, pushed object graph, and the hooks payload environment (via the gRPC unary request)
- Exploit idea: Supply a receive-pack push crafted so the pre-receive hook is skipped or its failure ignored; if `updateReferenceWithHooks` uses it without enforcing that every ref update is gated by a successful pre-receive, the request escapes the intended boundary.
- Invariant to test: reference updates on a push run through pre-receive/update/post-receive with an unforgeable hooks payload; no path lets a ref advance while skipping them.
- Expected Immunefi impact: (GitLab HackerOne class) Hook/quarantine bypass on push: refs advance with objects that never passed pre-receive/update verification, or a protected-branch/access decision is skipped.
- Fast validation: Integration test asserting a rejecting pre-receive blocks the ref update.
