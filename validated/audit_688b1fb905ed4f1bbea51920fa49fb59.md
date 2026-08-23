Confirmed: `cleanup()` calls `os.RemoveAll(dir.path)` on the temp dir path only [1](#0-0) . It has no knowledge of, and does not attempt to clean up, any files the `tar` subprocess may have written outside that directory via path-traversal entries.

### Title
Failed CreateRepositoryFromSnapshot leaves attacker-controlled files outside the repo when external `tar` writes traversal entries before failing mid-stream - ([File: internal/gitaly/service/repository/create_repository_from_snapshot.go])

### Finding Description
`server.untar` pipes the attacker-supplied HTTP response body directly into an external `tar -C path -xvf -` process with no entry-name validation of any kind [2](#0-1) . GNU `tar` does not block relative `../` path components in member names by default — it happily writes wherever the joined path resolves to, unlike the hand-rolled `extractTarToDirectory` used elsewhere in the codebase, which explicitly checks `strings.HasPrefix(targetPath, targetDir+string(os.PathSeparator))` for every entry type [3](#0-2) . No equivalent guard exists in `untar`. The code comment itself acknowledges this: "the received archive is trusted *a lot*... it should undergo a lot of hardening" [4](#0-3) .

When `tar` processes entries sequentially and a later entry is truncated/corrupt, `tar` exits non-zero after already having written the earlier, valid `../leftover-file` entry to disk. `cmd.Wait()` returns the error, `untar` propagates it, and `repoutil.Create`'s `seedRepository` callback returns the error [5](#0-4) . The only rollback that occurs is the deferred `cleanup()`, which is `os.RemoveAll(newRepoDir.path)` — i.e., removal of the temporary repository directory itself [1](#0-0) , [6](#0-5) . This cleanup has no mechanism to discover or remove files that `tar` wrote outside `newRepoDir` via `../` traversal, since those paths are never tracked or recorded anywhere.

### Impact Explanation
This matches the "archive extraction escape" impact class explicitly called out in scope: a failed/rolled-back RPC leaves attacker-controlled residue (`leftover-file`) outside the intended repository directory, potentially anywhere on the storage filesystem the Gitaly process user can write to, persisting after the RPC call returns an error to the caller.

### Likelihood Explanation
Feasibility depends entirely on whether an unprivileged, no-special-role GitLab user can supply the `HttpUrl` (and cause the response to fail mid-stream) for `CreateRepositoryFromSnapshot`. In the codebase, this RPC's `HttpUrl` is designed to point at another Gitaly node's `GetSnapshot` endpoint (used for Geo/replication flows) rather than being a field that ordinary end users control through GitLab Rails APIs. I could not find, within the indexed code, any GitLab Rails-facing code path that lets a plain authenticated user supply an arbitrary `HttpUrl` value to this RPC — the callers found were internal (`internal/praefect/coordinator.go`, `internal/gitaly/storage/storagemgr/middleware.go`). Without confirming an accessible caller chain that lets an unprivileged user control `HttpUrl` and force a truncated response, the "attacker controls HttpUrl" precondition given in the question is not established as reachable by the stated unprivileged attacker model, so this cannot be validated as an in-scope, exploitable bug for that threat model as posed.

### Recommendation
N/A pending confirmation of reachability — if a control path exists (or is later exposed) where the RPC's `HttpUrl`/response content is attacker-influenced, `untar` should be hardened to either (a) validate/sanitize each tar entry name to reject `../`/absolute paths before invoking `tar`, mirroring `extractTarToDirectory`'s prefix checks, or (b) extract into an isolated temporary directory that is entirely disposed of (including anything written outside intended bounds by using `tar`'s `--one-top-level` combined with entry-name filtering, or replacing the external `tar` invocation with the already-existing sanitized in-process extractor).

### Proof of Concept
Not provided — the precondition (unprivileged attacker controlling `HttpUrl` for `CreateRepositoryFromSnapshot`) is not confirmed reachable from the available code/context, so a concrete unprivileged-attacker PoC cannot be constructed with confidence. A Devin session with full repo/build access would be needed to trace every caller of this RPC (including GitLab Rails-side call sites, which are outside this Gitaly repo) to confirm or refute reachability before treating this as a valid finding.

### Citations

**File:** internal/tempdir/tempdir.go (L42-46)
```go
	cleanup := func() {
		if err := os.RemoveAll(dir.path); err != nil {
			logger.WithError(err).WithField("temporary_directory", dir.path).ErrorContext(ctx, "failed to cleanup temp dir")
		}
	}
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L115-121)
```go
	cmd, err := command.New(ctx, s.logger, []string{"tar", "-C", path, "-xvf", "-"}, command.WithStdin(rsp.Body))
	if err != nil {
		return err
	}

	return cmd.Wait()
}
```

**File:** internal/gitaly/service/repository/create_repository_from_snapshot.go (L140-141)
```go
		// NOTE: The received archive is trusted *a lot*. Before pointing this RPC
		// at endpoints not under our control, it should undergo a lot of hardening.
```

**File:** internal/gitaly/service/repository/replicate.go (L334-339)
```go
		targetPath := filepath.Join(targetDir, header.Name)

		if !strings.HasPrefix(targetPath, targetDir+string(os.PathSeparator)) &&
			targetPath != targetDir {
			return fmt.Errorf("invalid file path in tar: %s", header.Name)
		}
```

**File:** internal/gitaly/repoutil/create.go (L106-110)
```go
	newRepoProto, newRepoDir, cleanup, err := tempdir.NewRepository(ctx, repository.GetStorageName(), logger, locator)
	if err != nil {
		return fmt.Errorf("creating temporary repository: %w", err)
	}
	defer cleanup()
```

**File:** internal/gitaly/repoutil/create.go (L144-148)
```go
	if err := seedRepository(newRepoProto); err != nil {
		// Return the error returned by the callback function as-is so we don't clobber any
		// potential returned gRPC error codes.
		return err
	}
```
