# Q0481: NewCustomHookError: A custom-hook path or content resolved from attacker-controlled repo s

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `NewCustomHookError` in `internal/gitaly/hook/custom.go` by supplying a custom-hook path or content resolved from attacker-controlled repo state, so that reference updates on a push run through pre-receive/update/post-receive with an unforgeable hooks payload is violated — specifically custom hooks execute only from the trusted hooks dir — leading to hook/quarantine bypass on push: refs advance with objects that never passed pre-receive/update verification, or a protected-branch/access decision is skipped?

## Target
- File/function: `internal/gitaly/hook/custom.go` -> `NewCustomHookError`
- Entrypoint: a push over smarthttp/ssh receive-pack invoking the hook manager
- Attacker controls: pushed ref updates, pushed object graph, and the hooks payload environment
- Exploit idea: Supply a custom-hook path or content resolved from attacker-controlled repo state; if `NewCustomHookError` uses it without enforcing that custom hooks execute only from the trusted hooks dir, the request escapes the intended boundary.
- Invariant to test: reference updates on a push run through pre-receive/update/post-receive with an unforgeable hooks payload; no path lets a ref advance while skipping them.
- Expected Immunefi impact: (GitLab HackerOne class) Hook/quarantine bypass on push: refs advance with objects that never passed pre-receive/update verification, or a protected-branch/access decision is skipped.
- Fast validation: Test custom hook resolution against a hostile repo layout.
