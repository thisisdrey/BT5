# Q3490: isZeroOID: A custom-hook path or content resolved from attacker-controlled repo state

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `isZeroOID` in `internal/gitaly/hook/postreceive.go` by supplying a custom-hook path or content resolved from attacker-controlled repo state, so that reference updates on a push run through pre-receive/update/post-receive with an unforgeable hooks payload is violated — specifically custom hooks execute only from the trusted hooks dir — leading to hook/quarantine bypass on push: refs advance with objects that never passed pre-receive/update verification, or a protected-branch/access decision is skipped?

## Target
- File/function: `internal/gitaly/hook/postreceive.go` -> `isZeroOID`
- Entrypoint: a push over smarthttp/ssh receive-pack invoking the hook manager
- Attacker controls: pushed ref updates, pushed object graph, and the hooks payload environment
- Exploit idea: Supply a custom-hook path or content resolved from attacker-controlled repo state; if `isZeroOID` uses it without enforcing that custom hooks execute only from the trusted hooks dir, the request escapes the intended boundary.
- Invariant to test: reference updates on a push run through pre-receive/update/post-receive with an unforgeable hooks payload; no path lets a ref advance while skipping them.
- Expected Immunefi impact: (GitLab HackerOne class) Hook/quarantine bypass on push: refs advance with objects that never passed pre-receive/update verification, or a protected-branch/access decision is skipped.
- Fast validation: Test custom hook resolution against a hostile repo layout.
