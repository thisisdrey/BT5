### Title
Unvalidated `timeout` field in `FetchRemoteRequest` disables the only execution-time bound on outbound `git-fetch`, enabling unbounded RPC-handler resource consumption - (File: internal/gitaly/service/repository/fetch_remote.go)

### Summary
`FetchRemote` derives its only execution-time safety bound directly from a caller-supplied `timeout` field with no minimum/maximum validation, mirroring the GoGoPool `duration` bug where an unvalidated user-supplied numeric field that governs a protective calculation/limit can be driven to a value that defeats the protection (there: minimize slashing; here: eliminate the fetch timeout).

### Finding Description
`FetchRemote` only applies a context timeout when the caller-supplied value is strictly greater than zero: [1](#0-0) 

`validateFetchRemoteRequest` never checks `req.GetTimeout()` at all — it only validates the repository and the remote URL: [2](#0-1) 

Because the field is a plain signed integer with no lower/upper bound enforced (analogous to the unchecked `duration` field in the GoGoPool `createMinipool` report), a caller can send `timeout: 0` (the default/unset value) or a negative value to make the `req.GetTimeout() > 0` check false, which skips the `context.WithTimeout` call entirely. The subsequent `fetchRemoteAtomic` then invokes `quarantineRepo.FetchRemote(ctx, "inmemory", opts)` against a remote URL supplied by the caller with no time bound of any kind: [3](#0-2) 

`git-fetch(1)` against an attacker/operator-controlled or slow/malicious remote can hang indefinitely (e.g., slow-loris style transport stalls, or a deliberately unresponsive Git server), holding the RPC handler, its quarantine directory, ref-transaction resources, and any acquired concurrency-limiter slot open for an unbounded duration.

### Impact Explanation
This is a DoS of an RPC handler in the same family as the referenced concurrency/queue-limit configuration in Gitaly's own backpressure design, which exists specifically to bound how long resources can be held per RPC. Because `FetchRemote`'s only per-call time bound is client-controlled and optional (zero/negative disables it), a caller with access to invoke `FetchRemote` (e.g., a mirroring/import workflow) can bypass the intended time-bound protection, tie up quarantine directories, and hold a `pack-objects`/per-repo concurrency slot indefinitely (as documented for the backpressure mechanism), degrading availability for the same repository/RPC pair.

### Likelihood Explanation
Likelihood is credible but bounded: `FetchRemote` requires an authenticated Gitaly RPC caller (not an anonymous internet client), so this is not exploitable by an arbitrary MITM/malicious external peer, but it is exploitable by any caller of the `FetchRemote` RPC (e.g., a project/repository owner triggering repository mirroring against attacker-controlled or slow-responding remote URLs) with zero effort — simply omitting or zeroing the `timeout` field, which is the default protobuf value anyway, so this is trivially reachable in normal usage patterns and not merely a contrived edge case.

### Recommendation
Enforce a mandatory bound on the `timeout` field in `validateFetchRemoteRequest`: reject non-positive values or fall back to a fixed, server-side-configured default/maximum timeout regardless of what the client sends, rather than allowing the client to fully disable the timeout by supplying `0` or a negative number.

### Proof of Concept
1. Set up a remote Git server (or TCP listener acting as one) that accepts a connection and never completes the `git-fetch(1)` handshake/response.
2. Call `FetchRemote` with `RemoteParams.Url` pointing at that server and `Timeout` unset (i.e., `0`).
3. Observe that `req.GetTimeout() > 0` evaluates false in `internal/gitaly/service/repository/fetch_remote.go` lines 35-39, so no `context.WithTimeout` is applied.
4. Observe the RPC handler, its quarantine directory, and any acquired per-repo concurrency slot remain held indefinitely because `quarantineRepo.FetchRemote` in `fetchRemoteAtomic` never times out.

### Citations

**File:** internal/gitaly/service/repository/fetch_remote.go (L30-39)
```go
func (s *server) FetchRemote(ctx context.Context, req *gitalypb.FetchRemoteRequest) (*gitalypb.FetchRemoteResponse, error) {
	if err := s.validateFetchRemoteRequest(ctx, req); err != nil {
		return nil, err
	}

	if req.GetTimeout() > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, time.Duration(req.GetTimeout())*time.Second)
		defer cancel()
	}
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L49-122)
```go
// fetchRemoteAtomic fetches changes from the specified remote repository. To be atomic, fetched
// objects are first quarantined and only migrated before committing the reference transaction.
func (s *server) fetchRemoteAtomic(ctx context.Context, req *gitalypb.FetchRemoteRequest) (_ bool, _ bool, returnedErr error) {
	var stdout, stderr bytes.Buffer
	opts := localrepo.FetchOpts{
		Stdout:  &stdout,
		Stderr:  &stderr,
		Force:   req.GetForce(),
		Prune:   !req.GetNoPrune(),
		Tags:    localrepo.FetchOptsTagsAll,
		Verbose: true,
		// Transactions are disabled during fetch operation because no references are updated when
		// the dry-run option is enabled. Instead, the reference-transaction hook is performed
		// during the subsequent execution of `git-update-ref(1)`.
		DisableTransactions: true,
		// When the `dry-run` option is used with `git-fetch(1)`, Git objects are received without
		// performing reference updates. This is used to quarantine objects on the initial fetch and
		// migration to occur only during reference update.
		DryRun: true,
		// The `porcelain` option outputs reference update information from `git-fetch(1) to stdout.
		// Since references are not updated during a `git-fetch(1)` dry-run, the reference
		// information is used during `git-update-ref(1)` execution to update the appropriate
		// corresponding references.
		Porcelain: true,
	}

	if req.GetNoTags() {
		opts.Tags = localrepo.FetchOptsTagsNone
	}

	if err := buildCommandOpts(ctx, &opts, req); err != nil {
		return false, false, err
	}

	sshCommand, sshCleanup, err := gitcmd.BuildSSHInvocation(ctx, s.logger, req.GetSshKey(), req.GetKnownHosts())
	if err != nil {
		return false, false, err
	}
	defer sshCleanup()

	opts.Env = append(opts.Env, "GIT_SSH_COMMAND="+sshCommand)

	// When performing fetch, objects are received before references are updated. If references fail
	// to be updated, unreachable objects could be left in the repository that would need to be
	// garbage collected. To be more atomic, a quarantine directory is set up where objects will be
	// fetched prior to being migrated to the main repository when reference updates are committed.
	quarantineDir, quarantineCleanup, err := quarantine.New(ctx, req.GetRepository(), s.logger, s.locator)
	if err != nil {
		return false, false, fmt.Errorf("creating quarantine directory: %w", err)
	}
	defer func() {
		quarantineCleanup() // Errors are logged by the tempdir package
	}()

	quarantineRepo := s.localRepoFactory.Build(quarantineDir.QuarantinedRepo())
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

**File:** internal/gitaly/service/repository/fetch_remote.go (L306-320)
```go
func (s *server) validateFetchRemoteRequest(ctx context.Context, req *gitalypb.FetchRemoteRequest) error {
	if err := s.locator.ValidateRepository(ctx, req.GetRepository()); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	if req.GetRemoteParams() == nil {
		return structerr.NewInvalidArgument("missing remote params")
	}

	if req.GetRemoteParams().GetUrl() == "" {
		return structerr.NewInvalidArgument("blank or empty remote URL")
	}

	return nil
}
```
