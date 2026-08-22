# Q1352: validateUserUpdateSubmoduleRequest: A batch reference transaction where one entry escapes 

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `validateUserUpdateSubmoduleRequest` in `internal/gitaly/service/operations/submodules.go` by supplying a batch reference transaction where one entry escapes validation, so that a reference update targets only a validated ref name and a verified object, and old-OID guards prevent replacing another user's ref state is violated — specifically every entry in a transaction is validated independently — leading to unauthorized reference mutation: forcing a ref to an arbitrary/unvetted object, or an injection through a crafted ref name that skips validation?

## Target
- File/function: `internal/gitaly/service/operations/submodules.go` -> `validateUserUpdateSubmoduleRequest`
- Entrypoint: WriteRef, UpdateReferences, DeleteRefs, UserCreateBranch, UserCommitFiles, tag/patch RPCs
- Attacker controls: ref names, old/new object IDs, commit/patch content, and force flags
- Exploit idea: Supply a batch reference transaction where one entry escapes validation; if `validateUserUpdateSubmoduleRequest` uses it without enforcing that every entry in a transaction is validated independently, the request escapes the intended boundary.
- Invariant to test: a reference update targets only a validated ref name and a verified object, and old-OID guards prevent replacing another user's ref state.
- Expected Immunefi impact: (GitLab HackerOne class) Unauthorized reference mutation: forcing a ref to an arbitrary/unvetted object, or an injection through a crafted ref name that skips validation.
- Fast validation: Test update_references batch validation.
