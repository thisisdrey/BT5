# Q0351: UserUpdateSubmodule: A DeleteRefs call removing refs outside the caller's intended set

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `UserUpdateSubmodule` in `internal/gitaly/service/operations/submodules.go` by supplying a DeleteRefs call removing refs outside the caller's intended set, so that a reference update targets only a validated ref name and a verified object, and old-OID guards prevent replacing another user's ref state is violated — specifically deletion is scoped to validated ref names — leading to unauthorized reference mutation: forcing a ref to an arbitrary/unvetted object, or an injection through a crafted ref name that skips validation?

## Target
- File/function: `internal/gitaly/service/operations/submodules.go` -> `UserUpdateSubmodule`
- Entrypoint: WriteRef, UpdateReferences, DeleteRefs, UserCreateBranch, UserCommitFiles, tag/patch RPCs
- Attacker controls: ref names, old/new object IDs, commit/patch content, and force flags
- Exploit idea: Supply a DeleteRefs call removing refs outside the caller's intended set; if `UserUpdateSubmodule` uses it without enforcing that deletion is scoped to validated ref names, the request escapes the intended boundary.
- Invariant to test: a reference update targets only a validated ref name and a verified object, and old-OID guards prevent replacing another user's ref state.
- Expected Immunefi impact: (GitLab HackerOne class) Unauthorized reference mutation: forcing a ref to an arbitrary/unvetted object, or an injection through a crafted ref name that skips validation.
- Fast validation: Test delete_refs scope.
