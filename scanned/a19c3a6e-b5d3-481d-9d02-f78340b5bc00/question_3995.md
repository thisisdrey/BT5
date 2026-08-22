# Q3995: validateUserApplyPatchHeader: A UserCommitFiles/apply-patch path writing outside the repo 

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `validateUserApplyPatchHeader` in `internal/gitaly/service/operations/apply_patch.go` by supplying a UserCommitFiles/apply-patch path writing outside the repo tree, so that a reference update targets only a validated ref name and a verified object, and old-OID guards prevent replacing another user's ref state is violated — specifically commit file paths stay within the repo tree — leading to unauthorized reference mutation: forcing a ref to an arbitrary/unvetted object, or an injection through a crafted ref name that skips validation?

## Target
- File/function: `internal/gitaly/service/operations/apply_patch.go` -> `validateUserApplyPatchHeader`
- Entrypoint: WriteRef, UpdateReferences, DeleteRefs, UserCreateBranch, UserCommitFiles, tag/patch RPCs
- Attacker controls: ref names, old/new object IDs, commit/patch content, and force flags
- Exploit idea: Supply a UserCommitFiles/apply-patch path writing outside the repo tree; if `validateUserApplyPatchHeader` uses it without enforcing that commit file paths stay within the repo tree, the request escapes the intended boundary.
- Invariant to test: a reference update targets only a validated ref name and a verified object, and old-OID guards prevent replacing another user's ref state.
- Expected Immunefi impact: (GitLab HackerOne class) Unauthorized reference mutation: forcing a ref to an arbitrary/unvetted object, or an injection through a crafted ref name that skips validation.
- Fast validation: Test commit_files/apply_patch path handling.
