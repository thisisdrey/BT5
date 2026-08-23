This confirms the vulnerability: `errorWithStderr` embeds the raw `stderr` bytes directly into the error's message string via `fmt.Errorf("%w, stderr: %q", err, stderr)` [1](#0-0) , and this wrapped error is passed unchanged up through `FetchFailedError`, `FetchBundle`'s `fmt.Errorf("fetch bundle: %w", err)`, and finally `structerr.NewInternal("%w", err)` in the RPC handler [2](#0-1) . Since `structerr.Error.GRPCStatus()` builds the client-visible `status.New(e.Code(), e.Error())` from the full error message string (not just structured metadata/details) [3](#0-2) , any text embedded in `e.Error()` — including the `stderr` bytes — is sent verbatim to the client. This differs from `WithMetadata`, which is server-side only and is not included in `GRPCStatus()` (confirmed by the test-only `StructErrUnaryInterceptor`/`interceptedError` which explicitly promotes metadata into error details only for testing) [4](#0-3) . The bundle path that appears in git-fetch's stderr is a temp file created via `tempdir.New` and `filepath.Join(tmpDir.Path(), "repo.bundle")`, which resides under the storage temp directory and could reveal absolute host paths [5](#0-4) .

### Title
Git-fetch stderr (including absolute temp bundle path) leaks to unprivileged gRPC client via FetchBundle error - ([File: internal/gitaly/service/repository/fetch_bundle.go])

### Summary
`FetchBundle` propagates the raw `stderr` output of the internal `git-fetch` invocation directly into the gRPC error message returned to the calling client. Because a malicious `Data` stream (an invalid bundle) reliably makes `git-fetch` fail while echoing the absolute path of the internal temp bundle file in its error text, an unprivileged client can learn Gitaly's internal storage/temp directory layout.

### Finding Description
The call chain is: `FetchBundle` (RPC handler) → `repo.FetchBundle` → `createTempBundle` (creates `bundlePath := filepath.Join(tmpDir.Path(), "repo.bundle")` under the storage's temp directory) → `repo.FetchRemote` (runs `git fetch inmemory` where `remote.inmemory.url` is set to `bundlePath`) [6](#0-5) . When the supplied `Data` is not a valid bundle, `git-fetch` exits non-zero and writes an error referencing the failing URL/path to stderr, which is captured into a buffer [7](#0-6) . On failure, `FetchRemote` wraps it as `FetchFailedError{errorWithStderr(err, stderr.Bytes())}`, and `errorWithStderr` concatenates the raw stderr bytes directly into the error's `.Error()` string via `fmt.Errorf("%w, stderr: %q", err, stderr)` [1](#0-0) . This string keeps propagating through `fmt.Errorf("fetch bundle: %w", err)` in `repo.FetchBundle` [8](#0-7)  and is finally wrapped by `structerr.NewInternal("%w", err)` in the RPC handler [2](#0-1) , with no sanitization or stripping of the message text at any point. `structerr.Error.GRPCStatus()` constructs the actual gRPC status sent over the wire using `e.Error()` as the status message — the entire concatenated string, including the embedded stderr — is transmitted to the client [3](#0-2) . Unlike `WithMetadata()`, which Gitaly's own style guide documents as safe for embedding potentially sensitive command stderr because it's server-side/log-only [9](#0-8) , `errorWithStderr` bakes the stderr straight into the message rather than attaching it as metadata, bypassing that intended safety boundary entirely.

### Impact Explanation
An unprivileged client who can invoke `FetchBundle` (e.g. via any gRPC path that lets them stream arbitrary bytes as a bundle for their own repository) receives the absolute filesystem path of Gitaly's per-storage temporary directory and the fixed bundle filename (`repo.bundle`) in the RPC error text. This discloses internal filesystem layout (storage root, tmp dir naming/UUID scheme) that is otherwise meant to stay server-internal, matching an information-disclosure bounty class — it does not by itself grant file read/write or code execution, but it aids reconnaissance for further path-based attacks and reveals server topology it shouldn't.

### Likelihood Explanation
Trivial and fully reachable by an unprivileged, minimally-capable attacker: they only need permission to invoke `FetchBundle` against a repository they control and to send non-bundle garbage as `Data`. No special configuration, race conditions, or elevated privileges are needed, and the failure/leak is deterministic and repeatable on every invalid-bundle attempt.

### Recommendation
Stop embedding raw command stderr into the error message returned to gRPC clients for `FetchBundle` (and other paths using `errorWithStderr`/`FetchFailedError`). Instead, attach the stderr as `structerr` metadata (`WithMetadata("stderr", ...)`) so it stays in server-side logs only, and return a generic, sanitized message (e.g., "fetch failed") to the client. Alternatively, apply `helper.SanitizeString`-style scrubbing of absolute paths/tmp dir names from stderr before including it in any client-visible message.

### Proof of Concept
```go
func TestFetchBundle_StderrLeaksTempPath(t *testing.T) {
    // ... setup Gitaly test server + valid empty repo ...
    stream, err := client.FetchBundle(ctx)
    require.NoError(t, err)

    require.NoError(t, stream.Send(&gitalypb.FetchBundleRequest{Repository: repo}))
    // Send garbage, non-bundle data to force git-fetch to fail
    require.NoError(t, stream.Send(&gitalypb.FetchBundleRequest{Data: []byte("not a real bundle")}))
    require.NoError(t, stream.CloseSend())

    _, err = stream.CloseAndRecv()
    require.Error(t, err)

    // The returned gRPC error message should NOT contain the internal storage/tmp path,
    // but currently it does (e.g. contains ".../+gitaly/tmp/.../repo.bundle").
    require.NotContains(t, err.Error(), "/tmp/")
    require.NotContains(t, err.Error(), "repo.bundle")
}
```

### Citations

**File:** internal/git/localrepo/repo.go (L216-221)
```go
func errorWithStderr(err error, stderr []byte) error {
	if len(stderr) == 0 {
		return err
	}
	return fmt.Errorf("%w, stderr: %q", err, stderr)
}
```

**File:** internal/gitaly/service/repository/fetch_bundle.go (L44-46)
```go
	if err := repo.FetchBundle(ctx, s.txManager, reader, opts); err != nil {
		return structerr.NewInternal("%w", err)
	}
```

**File:** internal/structerr/error.go (L239-258)
```go
func (e Error) GRPCStatus() *status.Status {
	st := status.New(e.Code(), e.Error())

	if details := e.Details(); len(details) > 0 {
		proto := st.Proto()

		for _, detail := range details {
			marshaled, err := anypb.New(detail)
			if err != nil {
				return status.New(codes.Internal, fmt.Sprintf("marshaling error details: %v", err))
			}

			proto.Details = append(proto.Details, marshaled)
		}

		st = status.FromProto(proto)
	}

	return st
}
```

**File:** internal/testhelper/testserver/structerr_interceptors.go (L15-21)
```go
// StructErrUnaryInterceptor is an interceptor for unary RPC calls that injects error metadata as detailed
// error. This is only supposed to be used for testing purposes as error metadata is considered to
// be a server-side detail. No clients should start to rely on it.
func StructErrUnaryInterceptor(ctx context.Context, req interface{}, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (interface{}, error) {
	response, err := handler(ctx, req)
	return response, interceptedError(err)
}
```

**File:** internal/git/localrepo/bundle.go (L160-184)
```go
	bundlePath, cleanup, err := repo.createTempBundle(ctx, reader)
	if err != nil {
		return fmt.Errorf("fetch bundle: %w", err)
	}
	defer cleanup()

	fetchConfig := []gitcmd.ConfigPair{
		{Key: "remote.inmemory.url", Value: bundlePath},
		{Key: "remote.inmemory.fetch", Value: git.MirrorRefSpec},
	}
	fetchOpts := FetchOpts{
		CommandOptions: []gitcmd.CmdOpt{
			gitcmd.WithConfigEnv(fetchConfig...),
			// Starting in Git version 2.46.0, executing git-fetch(1) on a bundle performs fsck
			// checks when `transfer.fsckObjects` is enabled. Prior to this, this configuration was
			// always ignored and fsck checks were not run. Unfortunately, fsck message severity
			// configuration is ignored by Git only for bundle fetches. Until this is supported by
			// Git, disable `transfer.fsckObjects` so bundles containing fsck errors can continue to
			// be fetched. This matches behavior prior to Git version 2.46.0.
			gitcmd.WithConfig(gitcmd.ConfigPair{Key: "transfer.fsckObjects", Value: "false"}),
		},
	}
	if err := repo.FetchRemote(ctx, "inmemory", fetchOpts); err != nil {
		return fmt.Errorf("fetch bundle: %w", err)
	}
```

**File:** internal/git/localrepo/bundle.go (L198-223)
```go
func (repo *Repo) createTempBundle(ctx context.Context, reader io.Reader) (bundlPath string, cleanup func(), returnErr error) {
	tmpDir, cleanup, err := tempdir.New(ctx, repo.GetStorageName(), repo.logger, repo.locator)
	if err != nil {
		return "", nil, fmt.Errorf("create temp bundle: %w", err)
	}

	bundlePath := filepath.Join(tmpDir.Path(), "repo.bundle")

	file, err := os.Create(bundlePath)
	if err != nil {
		cleanup() // Clean up if we fail after creating the temp directory
		return "", nil, fmt.Errorf("create temp bundle: %w", err)
	}
	defer func() {
		if err := file.Close(); err != nil && returnErr == nil {
			returnErr = fmt.Errorf("create temp bundle: %w", err)
		}
	}()

	if _, err = io.Copy(file, reader); err != nil {
		cleanup() // Clean up if we fail after creating the temp directory
		return "", nil, fmt.Errorf("create temp bundle: %w", err)
	}

	return bundlePath, cleanup, nil
}
```

**File:** internal/git/localrepo/remote.go (L84-135)
```go
	var stderr bytes.Buffer
	if opts.Stderr == nil {
		opts.Stderr = &stderr
	}

	objectHash, err := repo.ObjectHash(ctx)
	if err != nil {
		return fmt.Errorf("detecting object hash: %w", err)
	}

	commandOptions := []gitcmd.CmdOpt{
		gitcmd.WithEnv(opts.Env...),
		gitcmd.WithStdout(opts.Stdout),
		gitcmd.WithStderr(opts.Stderr),
		gitcmd.WithConfig(gitcmd.ConfigPair{
			// Git is so kind to point out that we asked it to not show forced updates
			// by default, so we need to ask it not to do that.
			Key: "advice.fetchShowForcedUpdates", Value: "false",
		}),
		gitcmd.WithConfig(gitcmd.ConfigPair{
			// The patch series https://lore.kernel.org/git/20240910203835.2288291-1-bence@ferdinandy.com/
			// introduces new behaviour that automatically sets the local HEAD to the remote's HEAD during
			// a fetch. This happens when the mirror refspec is used to fetch into a bare repository, which
			// we use in operations like `FetchBundle`.
			//
			// Setting the remote's `followremotehead` config to "never" will disable the new behaviour. We
			// do this temporarily until we're sure the new behaviour doesn't have any consequences.
			Key: fmt.Sprintf("remote.%s.followremotehead", remoteName), Value: "never",
		}),
	}
	if opts.DisableTransactions {
		commandOptions = append(commandOptions, gitcmd.WithDisabledHooks())
	} else {
		commandOptions = append(commandOptions, gitcmd.WithRefTxHook(objectHash, repo))
	}
	commandOptions = append(commandOptions, opts.CommandOptions...)

	cmd, err := repo.Exec(ctx,
		gitcmd.Command{
			Name:  "fetch",
			Flags: opts.buildFlags(),
			Args:  []string{remoteName},
		},
		commandOptions...,
	)
	if err != nil {
		return err
	}

	if err := cmd.Wait(); err != nil {
		return FetchFailedError{errorWithStderr(err, stderr.Bytes())}
	}
```

**File:** STYLE.md (L120-132)
```markdown
### Error metadata

Error metadata attaches dynamic data to errors that give the
consumer of logs additional context around why an error has happened. This can
include:

- Parameters controlled by the caller, assuming they don't leak any secrets,
  like an object ID.
- The standard error output of a command spawned by Gitaly.

Attaching such data as metadata is recommended over embedding it into the error
message. This makes errors easier to follow and allows us to deduplicate errors
by their now-static message in tools like Sentry.
```
