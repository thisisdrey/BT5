# Q1206: Unwrap: A hooks payload with mismatched repo/relative_path binding it to another repo

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `Unwrap` in `internal/gitaly/hook/prereceive.go` by supplying a hooks payload with mismatched repo/relative_path binding it to another repo, so that reference updates on a push run through pre-receive/update/post-receive with an unforgeable hooks payload is violated — specifically the payload binds to the exact target repository — leading to hook/quarantine bypass on push: refs advance with objects that never passed pre-receive/update verification, or a protected-branch/access decision is skipped?

## Target
- File/function: `internal/gitaly/hook/prereceive.go` -> `Unwrap`
- Entrypoint: a push over smarthttp/ssh receive-pack invoking the hook manager
- Attacker controls: pushed ref updates, pushed object graph, and the hooks payload environment
- Exploit idea: Supply a hooks payload with mismatched repo/relative_path binding it to another repo; if `Unwrap` uses it without enforcing that the payload binds to the exact target repository, the request escapes the intended boundary.
- Invariant to test: reference updates on a push run through pre-receive/update/post-receive with an unforgeable hooks payload; no path lets a ref advance while skipping them.
- Expected Immunefi impact: (GitLab HackerOne class) Hook/quarantine bypass on push: refs advance with objects that never passed pre-receive/update verification, or a protected-branch/access decision is skipped.
- Fast validation: Test payload repository binding.
