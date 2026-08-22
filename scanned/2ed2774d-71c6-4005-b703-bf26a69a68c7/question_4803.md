# Q4803: procreceive_handler: Objects that remain reachable after the update hook rejects them (qua

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `procreceive_handler` in `internal/gitaly/hook/procreceive_handler.go` by supplying objects that remain reachable after the update hook rejects them (quarantine not discarded), so that reference updates on a push run through pre-receive/update/post-receive with an unforgeable hooks payload is violated — specifically rejected pushes leave no reachable objects — leading to hook/quarantine bypass on push: refs advance with objects that never passed pre-receive/update verification, or a protected-branch/access decision is skipped?

## Target
- File/function: `internal/gitaly/hook/procreceive_handler.go` -> `procreceive_handler`
- Entrypoint: a push over smarthttp/ssh receive-pack invoking the hook manager
- Attacker controls: pushed ref updates, pushed object graph, and the hooks payload environment
- Exploit idea: Supply objects that remain reachable after the update hook rejects them (quarantine not discarded); if `procreceive_handler` uses it without enforcing that rejected pushes leave no reachable objects, the request escapes the intended boundary.
- Invariant to test: reference updates on a push run through pre-receive/update/post-receive with an unforgeable hooks payload; no path lets a ref advance while skipping them.
- Expected Immunefi impact: (GitLab HackerOne class) Hook/quarantine bypass on push: refs advance with objects that never passed pre-receive/update verification, or a protected-branch/access decision is skipped.
- Fast validation: Test that a failed hook migrates no objects out of quarantine.
