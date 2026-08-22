# Q5771: Error: A WriteRef forcing a ref to an object the pusher never introduced

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `Error` in `internal/git/updateref/updateref.go` (via the gRPC unary request) by supplying a WriteRef forcing a ref to an object the pusher never introduced, so that a reference update targets only a validated ref name and a verified object, and old-OID guards prevent replacing another user's ref state is violated — specifically ref targets are verified objects — leading to unauthorized reference mutation: forcing a ref to an arbitrary/unvetted object, or an injection through a crafted ref name that skips validation?

## Target
- File/function: `internal/git/updateref/updateref.go` -> `Error`
- Entrypoint: WriteRef, UpdateReferences, DeleteRefs, UserCreateBranch, UserCommitFiles, tag/patch RPCs
- Attacker controls: ref names, old/new object IDs, commit/patch content, and force flags (via the gRPC unary request)
- Exploit idea: Supply a WriteRef forcing a ref to an object the pusher never introduced; if `Error` uses it without enforcing that ref targets are verified objects, the request escapes the intended boundary.
- Invariant to test: a reference update targets only a validated ref name and a verified object, and old-OID guards prevent replacing another user's ref state.
- Expected Immunefi impact: (GitLab HackerOne class) Unauthorized reference mutation: forcing a ref to an arbitrary/unvetted object, or an injection through a crafted ref name that skips validation.
- Fast validation: Test WriteRef object verification.
