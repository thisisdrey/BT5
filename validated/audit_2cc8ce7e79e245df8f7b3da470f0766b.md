### Title
Fragile string-based check of `git-fetch(1)` exit status allows quarantine migration to proceed on unverified/ambiguous fetch failures - (File: internal/gitaly/service/repository/fetch_remote.go)

### Summary
`fetchRemoteAtomic` decides whether to treat a failed `git-fetch(1)` dry-run as fatal or as an expected/benign failure (a partial reference-update rejection) purely by comparing the Go error's string representation against the literal string `"exit status 1"`, instead of properly parsing the process's real exit code via `command.ExitStatus()`. When this weak heuristic matches, Gitaly proceeds to migrate quarantined objects into the real repository and commit reference updates based on stdout that was produced by a command that did not run to completion successfully.

### Finding Description
In `fetchRemoteAtomic`, after invoking `quarantineRepo.FetchRemote(ctx, "inmemory", opts)` against an attacker-influenced remote (the `RemoteParams.Url` for a pull mirror or fetch), the error handling is: [1](#0-0) 

If `git-fetch(1)` fails, Gitaly does not check the actual process exit code (as it does elsewhere via `command.ExitStatus()`, e.g. in `internal/gitaly/service/ref/refexists.go`), but instead:
1. Checks if `stderr` is non-empty — if so, treats it as fatal.
2. Otherwise, compares `err.Error()` textually against the literal Go string `"exit status 1"` produced by `exec.ExitError.Error()`. If it matches, the error is silently swallowed and execution continues as if the fetch had "mostly" succeeded except for expected/failed reference updates.

This is functionally the same bug class as the reported Compound issue: a return/error code from an external process is not properly validated before continuing privileged follow-on logic. Here, "properly validated" would mean using the structured exit code (`command.ExitStatus(err)`) and confirming it is specifically `1` with no other indicators of failure, rather than string-matching the wrapped Go error text, which conflates all causes that render `"exit status 1"` with empty stderr as the intentionally-tolerated "reference update rejected" case.

If it continues, Gitaly parses `stdout` with `gitcmd.NewFetchPorcelainScanner` and proceeds to call `quarantineDir.Migrate(ctx)` and `refUpdater.Commit()`: [2](#0-1) 

### Impact Explanation
An attacker controlling the remote endpoint used in a `FetchRemote` RPC (pull-mirror or manual fetch, both driven by ordinary user-configured remote URLs) can attempt to craft a response/interaction that causes `git-fetch(1)` to exit with status 1 while producing no stderr output, for a reason unrelated to a "reference update rejected" outcome (e.g., truncated pack transfer, protocol violation ending mid-stream, or interrupted connection during a large fetch). Under the current logic this is indistinguishable from the benign "partial reference-update failure" case and Gitaly will proceed to migrate the quarantined objects into the real repository and commit reference updates derived from possibly incomplete/`stdout` porcelain data. This weakens the intended "atomicity" guarantee of the quarantine/migrate design (explicitly documented in the surrounding comments) that is supposed to prevent partial or unverified fetch results from being applied to the repository.

### Likelihood Explanation
Reaching this code path only requires an ordinary authenticated user to configure/trigger a `FetchRemote` call (e.g., pull mirroring) against a URL they control, which is a normal, low-privilege capability. Reliably engineering the exact "exit 1, empty stderr, but not actually a rejected-ref case" condition from a remote Git server implementation requires some effort, but is plausible since the check does not validate the actual git-fetch exit code or a specific expected message; any code path in git or the transport layer that produces this generic error text without emitting to stderr satisfies the bypass condition.

### Recommendation
Do not use exact string comparison of `err.Error()` to distinguish transient/benign vs. fatal `git-fetch` failures. Instead:
- Extract the real process exit code using the existing `command.ExitStatus(err)` helper (already used elsewhere, e.g. `internal/git/localrepo/config.go` and `internal/gitaly/service/ref/refexists.go`).
- Only treat exit code `1` as benign when there is additional positive confirmation that the failure is specifically a rejected reference update (e.g., verifying the porcelain output contains a `RefUpdateTypeUpdateFailed` line, or that scanning fully completed without truncation), rather than assuming absence of stderr is sufficient.
- Consider aborting (and not migrating quarantined objects) whenever the fetch process's own exit status/type cannot be positively confirmed as the expected benign case.

### Proof of Concept
1. Configure a `FetchRemote`/pull-mirror RPC pointing at an attacker-controlled Git remote.
2. Have the malicious remote server terminate the `git-fetch --dry-run --porcelain` connection or otherwise cause `git-fetch(1)` to return exit code 1 without writing to stderr, but before/without emitting a clean, fully-formed porcelain summary reflecting the true state of the transfer.
3. Observe that `fetchRemoteAtomic` treats `err.Error() == "exit status 1"` as the tolerated case in [3](#0-2) , continues, and proceeds to call `quarantineDir.Migrate` and commit reference updates based on the partial/ambiguous `stdout` content.

### Citations

**File:** internal/gitaly/service/repository/fetch_remote.go (L104-122)
```go
	if err := quarantineRepo.FetchRemote(ctx, "inmemory", opts); err != nil {
		// When `git-fetch(1)` fails to apply all reference updates successfully, the command
		// returns `exit status 1`. Despite this error, successful reference updates should still be
		// applied during the subsequent `git-update-ref(1)`. To differentiate between regular
		// errors and failed reference updates, stderr is checked for an error message. If an error
		// message is present, it is determined that an error occurred and the operation halts.
		errMsg := stderr.String()
		if errMsg != "" {
			return false, false, structerr.NewInternal("fetch remote: %q: %w", errMsg, err)
		}

		// Some errors during the `git-fetch(1)` operation do not print to stderr. If the error
		// message is not `exit status 1`, it is determined that the error is unrelated to failed
		// reference updates and the operation halts. Otherwise, it is assumed the error is from a
		// failed reference update and the operation proceeds to update references.
		if err.Error() != "exit status 1" {
			return false, false, structerr.NewInternal("fetch remote: %w", err)
		}
	}
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L227-236)
```go
	// Before committing the remaining reference updates, fetched objects must be migrated out of
	// the quarantine directory.
	if err := quarantineDir.Migrate(ctx); err != nil {
		return false, false, fmt.Errorf("migrating quarantined objects: %w", err)
	}

	// Commit the remaining queued reference updates so the changes get applied.
	if err := refUpdater.Commit(); err != nil {
		return false, false, fmt.Errorf("committing reference update: %w", err)
	}
```
