# Q3283: submodules: An old-OID guard bypass allowing overwrite of another user's ref state

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `submodules` in `internal/gitaly/service/operations/submodules.go` by supplying an old-OID guard bypass allowing overwrite of another user's ref state, so that a reference update targets only a validated ref name and a verified object, and old-OID guards prevent replacing another user's ref state is violated — specifically old-OID must match current state before update — leading to unauthorized reference mutation: forcing a ref to an arbitrary/unvetted object, or an injection through a crafted ref name that skips validation?

## Target
- File/function: `internal/gitaly/service/operations/submodules.go` -> `submodules`
- Entrypoint: WriteRef, UpdateReferences, DeleteRefs, UserCreateBranch, UserCommitFiles, tag/patch RPCs
- Attacker controls: ref names, old/new object IDs, commit/patch content, and force flags
- Exploit idea: Supply an old-OID guard bypass allowing overwrite of another user's ref state; if `submodules` uses it without enforcing that old-OID must match current state before update, the request escapes the intended boundary.
- Invariant to test: a reference update targets only a validated ref name and a verified object, and old-OID guards prevent replacing another user's ref state.
- Expected Immunefi impact: (GitLab HackerOne class) Unauthorized reference mutation: forcing a ref to an arbitrary/unvetted object, or an injection through a crafted ref name that skips validation.
- Fast validation: Concurrency/test on updateref old-OID enforcement.
