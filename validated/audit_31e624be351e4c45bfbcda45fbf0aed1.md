Confirmed the bug in `SetConfig`: the error-handling `switch` only returns for exit codes 1 and 2 from `git-config`; any other non-nil error (e.g. exit codes 3/4/128, or process failures other than a clean git-config exit) falls through the switch unhandled, and execution proceeds to `transaction.CommitLockedFile`, committing the locked config file as if the write succeeded.

### Title
Unchecked git-config exit code causes SetConfig to silently commit a failed/unwritten config change - (File: internal/git/localrepo/config.go)

### Summary
`Repo.SetConfig` writes a Git config value using `git config --replace-all --file <writer.Path()>`. If the underlying `git-config` invocation fails with any exit status other than `1` or `2`, the error is captured but never returned or otherwise acted upon — the `switch` statement has no `default` case, so the function falls through and proceeds to commit the (unmodified or partially written) locked config file as though the operation succeeded.

### Finding Description [1](#0-0) 

The exit-code handling only distinguishes two `git-config` failure modes documented upstream (bad section/name = 1, missing section/name = 2). Any other failure — e.g. exit code 3 (invalid config file), exit code 4 (config file cannot be written, such as a permissions or disk-full error), or exit code 128 (fatal errors, e.g. corrupted config file, symlink loop, or file-locking races) — is silently absorbed by the empty `switch`, and the function does not `return` an error in that branch. Execution then falls through unconditionally to: [2](#0-1) 

which calls `transaction.CommitLockedFile`, committing whatever content is currently in `writer.Path()` (potentially just the seeded/original config content, or a partially-written file) as the new, "successfully" updated config — while the caller of `SetConfig` receives a `nil` error and believes the configuration value was set.

This is a structurally identical bug class to the ERC20 "missing return value check" report: a called operation's failure/return status is not properly checked before proceeding, and the caller treats an unverified operation as successful.

`SetConfig` is a widely used low-level primitive throughout Gitaly's `localrepo.Repo` (e.g., for setting `receive.denyCurrentBranch`, hooks/config synchronization, and other repository configuration operations invoked from various RPC handlers). A failure path that silently no-ops instead of surfacing an error means downstream security- or correctness-relevant configuration values (e.g. related to hooks, quarantine, or push acceptance policy) can silently fail to be applied while all call sites believe they succeeded.

### Impact Explanation
Because the caller receives `nil` and no error, any code path that relies on `SetConfig` having actually applied a configuration value (for correctness or security policy) will proceed under a false assumption. In the worst case, a security-relevant config write (e.g. disabling something, or setting an option upon which a hook/gating decision depends) can silently fail to take effect, while the rest of the system operates as if it is in effect. This is a silent-failure / integrity issue rather than a directly exploitable memory-safety or auth-bypass bug, but it undermines confidence in config-based gating logic.

### Likelihood Explanation
Exit codes other than 1/2 from `git-config` are relatively uncommon in normal healthy operation (they typically require disk/permission issues, corrupted config files, or concurrent lock contention), so this would most likely manifest under resource exhaustion, concurrent writes, or corrupted repository state — conditions that can be triggered or influenced by a malicious/uncooperative tenant in a multi-tenant Gitaly deployment (e.g., inducing disk pressure or concurrent config writes to increase the chance of hitting one of these failure exit codes).

### Recommendation
Add a `default` case (or restructure to `if`/`else`) in the `switch` in `SetConfig` that returns a wrapped error for any exit code not explicitly handled, mirroring the more defensive pattern already used in `UnsetMatchingConfig` (which returns `fmt.Errorf("getting matching keys: %w", err)` for unmatched cases):

```go
if err := repo.ExecAndWait(ctx, gitcmd.Command{...}); err != nil {
    switch {
    case isExitWithCode(err, 1):
        return fmt.Errorf("%w: bad section or name", gitcmd.ErrInvalidArg)
    case isExitWithCode(err, 2):
        return fmt.Errorf("%w: missing section or name", gitcmd.ErrInvalidArg)
    default:
        return fmt.Errorf("setting config: %w", err)
    }
}
```

### Proof of Concept
1. Call `Repo.SetConfig(ctx, key, value, txManager)` on a repository whose `.git/config` file (or its directory) cannot be written by the Gitaly process — e.g. remove write permission on the parent directory after the `LockingFileWriter` is created but before `git config` runs, or trigger a disk-full / file-locking-race condition so `git-config` exits with a code other than 1 or 2 (e.g. 4 or 128).
2. Observe that `SetConfig` returns `nil` (no error) via `transaction.CommitLockedFile`, even though the intended config value was never actually written by `git-config`.
3. Read back the config value (e.g. via `git config --get key`) and confirm it does not reflect the value passed to `SetConfig`, despite the caller having received a successful (`nil`) return. [3](#0-2)

### Citations

**File:** internal/git/localrepo/config.go (L18-73)
```go
func isExitWithCode(err error, code int) bool {
	actual, ok := command.ExitStatus(err)
	if !ok {
		return false
	}

	return code == actual
}

// SetConfig will set a configuration value. Any preexisting values will be overwritten with the new
// value. The change will use transactional semantics.
func (repo *Repo) SetConfig(ctx context.Context, key, value string, txManager transaction.Manager) (returnedErr error) {
	repoPath, err := repo.Path(ctx)
	if err != nil {
		return err
	}
	configPath := filepath.Join(repoPath, "config")

	writer, err := safe.NewLockingFileWriter(configPath, safe.LockingFileWriterConfig{
		SeedContents: true,
	})
	if err != nil {
		return fmt.Errorf("creating config writer: %w", err)
	}
	defer func() {
		if err := writer.Close(); err != nil && returnedErr == nil {
			returnedErr = fmt.Errorf("closing config writer: %w", err)
		}
	}()

	if err := repo.ExecAndWait(ctx, gitcmd.Command{
		Name: "config",
		Flags: []gitcmd.Option{
			gitcmd.Flag{Name: "--replace-all"},
			gitcmd.ValueFlag{Name: "--file", Value: writer.Path()},
		},
		Args: []string{key, value},
	}); err != nil {
		// Please refer to https://git-scm.com/docs/git-config#_description
		// on return codes.
		switch {
		case isExitWithCode(err, 1):
			// section or key is invalid
			return fmt.Errorf("%w: bad section or name", gitcmd.ErrInvalidArg)
		case isExitWithCode(err, 2):
			// no section or name was provided
			return fmt.Errorf("%w: missing section or name", gitcmd.ErrInvalidArg)
		}
	}

	if err := transaction.CommitLockedFile(ctx, txManager, writer); err != nil {
		return fmt.Errorf("committing config: %w", err)
	}

	return nil
}
```
