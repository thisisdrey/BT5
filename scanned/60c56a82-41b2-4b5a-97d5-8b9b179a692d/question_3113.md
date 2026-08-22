# Q3113: AcceptUpdate: A procreceive/postreceive race that commits refs before verification complet

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `AcceptUpdate` in `internal/gitaly/hook/procreceive_handler.go` by supplying a procreceive/postreceive race that commits refs before verification completes, so that reference updates on a push run through pre-receive/update/post-receive with an unforgeable hooks payload is violated — specifically hook completion strictly precedes ref visibility — leading to hook/quarantine bypass on push: refs advance with objects that never passed pre-receive/update verification, or a protected-branch/access decision is skipped?

## Target
- File/function: `internal/gitaly/hook/procreceive_handler.go` -> `AcceptUpdate`
- Entrypoint: a push over smarthttp/ssh receive-pack invoking the hook manager
- Attacker controls: pushed ref updates, pushed object graph, and the hooks payload environment
- Exploit idea: Supply a procreceive/postreceive race that commits refs before verification completes; if `AcceptUpdate` uses it without enforcing that hook completion strictly precedes ref visibility, the request escapes the intended boundary.
- Invariant to test: reference updates on a push run through pre-receive/update/post-receive with an unforgeable hooks payload; no path lets a ref advance while skipping them.
- Expected Immunefi impact: (GitLab HackerOne class) Hook/quarantine bypass on push: refs advance with objects that never passed pre-receive/update verification, or a protected-branch/access decision is skipped.
- Fast validation: Concurrency test on the hook->ref-write ordering.
