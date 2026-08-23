### Title
Reference name is not validated before being embedded in the hooks `changes` line, allowing injection of extra synthetic hook-input lines - ([File: internal/gitaly/hook/updateref/update_with_hooks.go])

### Summary
`UpdaterWithHooks.UpdateReference` builds the `changes` string fed to `PreReceiveHook` and `ReferenceTransactionHook` as `fmt.Sprintf("%s %s %s\n", oldrev, newrev, reference)`, but only `oldrev`/`newrev` are validated with `objectHash.ValidateHex`; the `reference` value is never checked for embedded control characters such as `\n`. `git.ReferenceName` values built from `UserUpdateBranch`'s `BranchName` field (via `git.NewReferenceNameFromBranchName`) are copied verbatim from the unprivileged caller without calling `git.ValidateReference`, so a branch name containing a literal newline can inject a second, attacker-controlled "oldrev newrev refname" line into the hook's stdin stream before Git itself ever rejects the malformed ref.

### Finding Description
- `UserUpdateBranch` (`internal/gitaly/service/operations/user_update_branch.go:64`) builds `referenceName := git.NewReferenceNameFromBranchName(string(req.GetBranchName()))`. `NewReferenceNameFromBranchName` (`internal/git/reference.go:96-98`) simply prepends `"refs/heads/"` to the caller-controlled bytes — it does **not** call `git.ValidateReference`, which is the function that rejects control characters, spaces, and newlines.
- `oldrev`/`newrev` are parsed with `objectHash.FromHex`, which internally calls the strict `ValidateHex` (`internal/git/object_id.go:120-135`) — these values cannot carry injected bytes.
- `UpdateReference` (`internal/gitaly/hook/updateref/update_with_hooks.go:190-200`) only checks `reference == ""`, then builds:
  `changes := fmt.Sprintf("%s %s %s\n", oldrev, newrev, reference)` [1](#0-0) 
  This `changes` buffer is sent as stdin to `PreReceiveHook` (line 224) and to `ReferenceTransactionHook` (lines 277, 292, 305) — both of which are consumed by GitLab Rails' custom hook / access-control logic, which parses stdin line-by-line as `oldrev newrev refname`.
- Because `reference` can contain an embedded `\n`, an attacker-supplied branch name like `evil\n<oldrev2> <newrev2> refs/heads/master` turns the single logical `changes` line into two lines, the second of which is a synthetic, fully attacker-controlled "ref update" record that never actually happens in Git but is presented to the hook input stream as if it did.
- Actual format enforcement (`git.ValidateReference`'s control-character check, or `updateref.Updater`'s `RefInvalidFormatRegex` parsing of `git update-ref` stderr) only happens later, inside `updater.Prepare()`/`Commit()` at lines 283/296 — i.e. **after** `PreReceiveHook` (line 224) and `UpdateHook` (line 246) have already run with the malformed `changes`/argument data. `UpdateHook` itself passes `ref`/`oldValue`/`newValue` as separate `exec.Command` arguments (`internal/gitaly/hook/update.go:71-78`), so there is no shell/argv injection there, but the `changes` stdin stream passed to `PreReceiveHook`/`ReferenceTransactionHook` is affected.

### Impact Explanation
This lets an unprivileged user smuggle a spoofed, extra "ref change" line into the stdin payload consumed by GitLab's custom/Rails pre-receive and reference-transaction hooks before Gitaly's own ref-format validation has a chance to reject the malformed reference. Since GitLab Rails' pre-receive access-control gate parses this stream to authorize/deny the push per line, this could desynchronize what Rails believes is being changed versus what Git will actually apply (the real update ultimately fails with `InvalidReferenceFormatError` once `git update-ref` runs), corresponding to a hook/gating bypass or spoofing class of issue rather than a full RCE.

### Likelihood Explanation
This is trivially reachable by any unprivileged user permitted to call `UserUpdateBranch` (push access to their own repo), requiring no special configuration, secrets, or privileged roles — only a crafted `BranchName` field containing a literal `\n` byte alongside a syntactically valid extra "oldrev newrev refname" triplet.

### Recommendation
Call `git.ValidateReference` (or otherwise reject any reference name containing control characters, in particular `\n`) on the `BranchName`/`reference` value immediately after receiving it in `UserUpdateBranch` (and all other RPCs deriving a `git.ReferenceName` from raw request bytes), and additionally validate `reference` inside `UpdaterWithHooks.UpdateReference` before it is interpolated into the `changes` string, so malformed references are rejected before any hook is invoked rather than only when `git update-ref` finally runs.

### Proof of Concept
```go
func TestUpdateReference_NewlineInjectionInChangesLine(t *testing.T) {
    // ... setup UpdaterWithHooks u, repoProto, quarantineDir, valid oldrev/newrev hex values ...
    maliciousRef := git.ReferenceName(
        "refs/heads/evil\n" + oldrev.String() + " " + newrev2.String() + " refs/heads/master",
    )
    err := u.UpdateReference(ctx, repoProto, user, nil, maliciousRef, newrev, oldrev)
    // Expect: the hook manager's PreReceiveHook/ReferenceTransactionHook stdin ("changes")
    // contains two lines instead of one, the second being a synthetic,
    // fully attacker-controlled "oldrev newrev refname" record,
    // even though the actual git update-ref call ultimately fails with
    // an InvalidReferenceFormatError due to the embedded newline.
}
```
Capture the `stdin` passed to a mock `hook.Manager.PreReceiveHook`/`ReferenceTransactionHook` and assert it contains more than one newline-delimited record for a single logical `UpdateReference` call.

### Citations

**File:** internal/gitaly/hook/updateref/update_with_hooks.go (L190-200)
```go
	if reference == "" {
		return fmt.Errorf("reference cannot be empty")
	}
	if err := objectHash.ValidateHex(oldrev.String()); err != nil {
		return fmt.Errorf("validating old value: %w", err)
	}
	if err := objectHash.ValidateHex(newrev.String()); err != nil {
		return fmt.Errorf("validating new value: %w", err)
	}

	changes := fmt.Sprintf("%s %s %s\n", oldrev, newrev, reference)
```
