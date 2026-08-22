# Q3552: checkState: A ref name containing '..', control bytes, or '--' that skips validation

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `checkState` in `internal/git/updateref/updateref.go` by supplying a ref name containing '..', control bytes, or '--' that skips validation, so that a reference update targets only a validated ref name and a verified object, and old-OID guards prevent replacing another user's ref state is violated — specifically ref names are validated by git-check-ref-format rules — leading to unauthorized reference mutation: forcing a ref to an arbitrary/unvetted object, or an injection through a crafted ref name that skips validation?

## Target
- File/function: `internal/git/updateref/updateref.go` -> `checkState`
- Entrypoint: WriteRef, UpdateReferences, DeleteRefs, UserCreateBranch, UserCommitFiles, tag/patch RPCs
- Attacker controls: ref names, old/new object IDs, commit/patch content, and force flags
- Exploit idea: Supply a ref name containing '..', control bytes, or '--' that skips validation; if `checkState` uses it without enforcing that ref names are validated by git-check-ref-format rules, the request escapes the intended boundary.
- Invariant to test: a reference update targets only a validated ref name and a verified object, and old-OID guards prevent replacing another user's ref state.
- Expected Immunefi impact: (GitLab HackerOne class) Unauthorized reference mutation: forcing a ref to an arbitrary/unvetted object, or an injection through a crafted ref name that skips validation.
- Fast validation: Test write_ref/update_references ref-name validation.
