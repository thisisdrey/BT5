### Title
DoS in `SSHUploadPackWithSidechannel` due to missing deepen-exit-status exception - ([File: internal/gitaly/service/ssh/upload_pack.go])

### Summary
`git-upload-pack(1)` is documented and known to exit with a non-zero status after successfully serving a shallow ("deepen") negotiation. Gitaly's smart-HTTP handler explicitly accounts for this and treats it as success, but the SSH sidechannel handler for the same RPC does not, so every legitimate shallow fetch/clone performed over SSH is turned into an internal error.

### Finding Description
In `internal/gitaly/service/smarthttp/upload_pack.go`, `runUploadPack` explicitly special-cases this condition: [1](#0-0) 

```go
if err := cmd.Wait(); err != nil {
    stats = collector.finish()
    if _, ok := command.ExitStatus(err); ok && stats.Deepen != "" {
        // We have seen a 'deepen' message in the request. It is expected that
        // git-upload-pack has a non-zero exit status: don't treat this as an
        // error.
        s.recordServedBytes(stats, respBytes)
        return stats, nil
    }
    return nil, structerr.NewFailedPrecondition("waiting for upload-pack: %w", err)
}
```

This is confirmed by a dedicated regression test, `TestServer_PostUploadPackWithSidechannel_suppressDeepenExitError`, which asserts `require.NoError(t, err)` for a `deepen 1` request: [2](#0-1) 

The equivalent SSH path, `sshUploadPack` in `internal/gitaly/service/ssh/upload_pack.go`, invokes the exact same `git-upload-pack` binary via `runUploadCommand`, but has no such exception — any non-nil error from `cmd.Wait()` (propagated up through `runUploadCommand`) is treated as a hard failure: [3](#0-2) 

```go
if err := s.runUploadCommand(ctx, repo, stdin, stdout, stderr, timeoutTicker, pktline.PktDone(), gitcmd.Command{
    Name: "upload-pack",
    Args: []string{repoPath},
}, commandOpts...); err != nil {
    status, _ := command.ExitStatus(err)
    return nil, status, fmt.Errorf("running upload-pack: %w", err)
}
```

`runUploadCommand` itself has no deepen-aware exception either — it simply wraps and returns the wait error: [4](#0-3) 

The caller, `SSHUploadPackWithSidechannel`, then converts *any* such error into a gRPC `Internal` error even though the packfile has already been fully and correctly transmitted to the client: [5](#0-4) 

This is structurally the same bug class as the referenced report: a return signal that legitimately indicates "operation succeeded" (git-upload-pack's post-deepen non-zero exit code, analogous to `mint`'s amount-as-error-code return) is being compared/treated as a failure condition (`err != nil` ⇒ hard failure), causing the RPC to report failure for an operation that in fact completed correctly.

### Impact Explanation
Any ordinary user performing a shallow fetch or clone (`git fetch --depth=N`, `git clone --depth=N`) over the Git SSH transport against Gitaly will have their `SSHUploadPackWithSidechannel` RPC fail with an `Internal` gRPC error, even though `git-upload-pack` already streamed the complete, correct packfile to the client before exiting non-zero. This is a reachable, unprivileged-triggerable availability bug (DoS of a specific, commonly-used Git operation — shallow clone/fetch — over SSH) with no data loss or corruption, mirroring the "confirmed Medium, no funds at risk, availability only" judgment in the referenced report.

### Likelihood Explanation
High likelihood of being triggered incidentally: shallow clones/fetches (`--depth`) are a standard, frequently used Git feature, and any client using the SSH transport (as opposed to smart HTTP, which is already fixed) will hit this path on every such request. No special privileges, malicious peers, or crafted payloads are needed — a normal `git fetch --depth=1` over SSH is sufficient.

### Recommendation
Mirror the fix already applied to the smart-HTTP `upload-pack` path: in `sshUploadPack` (or in `runUploadCommand`), track whether the negotiation contained a `deepen` request (the negotiation stats are already parsed via `stats.ParsePackfileNegotiation` and available as `negotiation.Deepen`), and if `command.ExitStatus(err)` succeeds and `negotiation.Deepen != ""`, treat the command's non-zero exit as success rather than propagating it as an RPC error, consistent with `internal/gitaly/service/smarthttp/upload_pack.go` lines 185-196.

### Proof of Concept
1. Create a repository with at least one commit.
2. Over SSH, invoke `SSHUploadPackWithSidechannel` sending a `want <oid> <caps>` followed by a `deepen 1` pktline and a flush packet (equivalent to what `git fetch --depth=1` sends), exactly as the existing `TestServer_PostUploadPackWithSidechannel_suppressDeepenExitError` test does for the HTTP path (`internal/gitaly/service/smarthttp/upload_pack_test.go:356-386`), but instead call the SSH RPC (`internal/gitaly/service/ssh/upload_pack.go` `SSHUploadPackWithSidechannel`).
3. Observe that `git-upload-pack` writes a complete `shallow`+packfile response to the client (operation succeeds at the Git protocol level), but the gRPC call returns `structerr.NewInternal` because `cmd.Wait()` returned a non-nil error and there is no deepen exception in the SSH path, unlike the HTTP path.

### Citations

**File:** internal/gitaly/service/smarthttp/upload_pack.go (L185-196)
```go
	if err := cmd.Wait(); err != nil {
		stats = collector.finish()
		if _, ok := command.ExitStatus(err); ok && stats.Deepen != "" {
			// We have seen a 'deepen' message in the request. It is expected that
			// git-upload-pack has a non-zero exit status: don't treat this as an
			// error.
			s.recordServedBytes(stats, respBytes)
			return stats, nil
		}

		return nil, structerr.NewFailedPrecondition("waiting for upload-pack: %w", err)
	}
```

**File:** internal/gitaly/service/smarthttp/upload_pack_test.go (L356-386)
```go
func TestServer_PostUploadPackWithSidechannel_suppressDeepenExitError(t *testing.T) {
	t.Parallel()

	testhelper.NewFeatureSets(
		featureflag.BundleURI,
	).Run(t, testServerPostUploadPackWithSidechannelSuppressDeepenExitError)
}

func testServerPostUploadPackWithSidechannelSuppressDeepenExitError(t *testing.T, ctx context.Context) {
	t.Parallel()

	runTestWithAndWithoutConfigOptions(t, ctx, testServerPostUploadPackSuppressDeepenExitError, makePostUploadPackWithSidechannelRequest, testcfg.WithPackObjectsCacheEnabled())
}

func testServerPostUploadPackSuppressDeepenExitError(t *testing.T, ctx context.Context, makeRequest requestMaker, opts ...testcfg.Option) {
	cfg := testcfg.Build(t, opts...)
	cfg.SocketPath = runSmartHTTPServer(t, cfg)

	repo, repoPath := gittest.CreateRepository(t, ctx, cfg)
	commitID := gittest.WriteCommit(t, cfg, repoPath)

	var requestBody bytes.Buffer
	gittest.WritePktlineString(t, &requestBody, fmt.Sprintf("want %s %s\n", commitID, clientCapabilities))
	gittest.WritePktlineString(t, &requestBody, "deepen 1")
	gittest.WritePktlineFlush(t, &requestBody)

	rpcRequest := &gitalypb.PostUploadPackWithSidechannelRequest{Repository: repo}
	response, err := makeRequest(t, ctx, cfg.SocketPath, cfg.Auth.Token, rpcRequest, &requestBody)
	require.NoError(t, err)
	require.Equal(t, gittest.Pktlinef(t, "shallow %s", commitID)+"0000", response.String())
}
```

**File:** internal/gitaly/service/ssh/upload_pack.go (L108-114)
```go
	if err := s.runUploadCommand(ctx, repo, stdin, stdout, stderr, timeoutTicker, pktline.PktDone(), gitcmd.Command{
		Name: "upload-pack",
		Args: []string{repoPath},
	}, commandOpts...); err != nil {
		status, _ := command.ExitStatus(err)
		return nil, status, fmt.Errorf("running upload-pack: %w", err)
	}
```

**File:** internal/gitaly/service/ssh/upload_pack.go (L144-147)
```go
	stats, _, err := s.sshUploadPack(ctx, req, conn, stdout, stderr)
	if err != nil {
		return nil, structerr.NewInternal("%w", err)
	}
```

**File:** internal/gitaly/service/ssh/upload_command.go (L65-98)
```go
	if err := cmd.Wait(); err != nil {
		// The read monitor will cancel the local `ctx` when we do not observe a specific packet before the
		// timeout ticker ticks. This is done to address a time-of-check-to-time-of-use-style race, where the
		// client opens a connection but doesn't yet perform the negotiation of what data the server should
		// send. Because access checks only happen at the beginning of the call, it may be the case that the
		// client's permissions have changed since the RPC call started.
		//
		// To address this issue, we thus timebox the maximum amount of time between the start of the RPC call
		// and the end of the negotiation phase. While this doesn't completely address the issue, it's the best
		// we can reasonably do here.
		//
		// To distinguish cancellation of the overall RPC call and a timeout of the negotiation phase we use two
		// different contexts. In the case where the local context has been cancelled, we know that the reason
		// for cancellation is that the negotiation phase did not finish in time and thus return a more specific
		// error.
		if ctx.Err() != nil && rpcContext.Err() == nil {
			return structerr.NewDeadlineExceeded("waiting for negotiation: %w", ctx.Err())
		}

		// A common error case is that the client is terminating the request prematurely,
		// e.g. by killing their git-fetch(1) process because it's taking too long. This is
		// an expected failure, but we're not in a position to easily tell this error apart
		// from other errors returned by git-upload-pack(1). So we have to resort to parsing
		// the error message returned by Git, and if we see that it matches we return an
		// error with a `Canceled` error code.
		//
		// Note that we're being quite strict with how we match the error for now. We may
		// have to make it more lenient in case we see that this doesn't catch all cases.
		if stderrBuilder.String() == "fatal: the remote end hung up unexpectedly\n" {
			return structerr.NewCanceled("user canceled the request")
		}

		return fmt.Errorf("cmd wait: %w, stderr: %q", err, stderrBuilder.String())
	}
```
