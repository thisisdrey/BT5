### Title
CreateRepositoryFromURL leaks internal-network connection error details (git stderr) in the gRPC error message returned to the caller - (File: internal/gitaly/service/repository/create_repository_from_url.go)

### Summary
`CreateRepositoryFromURL` lets an unprivileged caller supply an arbitrary `Url` that Gitaly's `git clone` will connect to, and when the clone fails for any reason other than "repository not found," Gitaly returns `structerr.NewInternal("cloning repository: %w, stderr: %q", err, stderrStr)` directly as the gRPC error, embedding the raw git stderr text in the error message string that is sent back to the client [1](#0-0) . This stderr can differ depending on whether the target host/port is reachable, refused, or times out, giving the attacker an oracle to fingerprint internal network topology (classic error-based SSRF probing).

### Finding Description
`s.cloneFromURLCommand` runs `git clone` against `req.GetUrl()` with no allow/deny-list on scheme or destination host at this layer [2](#0-1) . When `cmd.Wait()` fails and the captured stderr does not match `remoteNotFoundRegex`, the code builds the returned error with `fmt.Errorf`-style formatting that interpolates the raw stderr text (`%q`) directly into the error's message via `structerr.NewInternal(...)` [3](#0-2) . Unlike the `MetadataItem`s attached via `WithMetadataItems` — which per `STYLE.md` and `structerr`'s own design are only meant to appear in server-side logs (`FieldsProducer`) and are only surfaced to gRPC clients through a **test-only** interceptor (`StructErrUnaryInterceptor`/`StructErrStreamInterceptor`, explicitly documented as "only supposed to be used for testing purposes... No clients should start to rely on it") [4](#0-3)  — the primary error **message** (constructed with `fmt.Errorf(format, a...)` in `structerr.newError`) becomes the actual gRPC status message returned to every caller in production, with no interceptor needed [5](#0-4) . Because `stderrStr` is embedded directly in that format string, git's connection-attempt error text (e.g., "Failed to connect to ... port 80: Connection refused" vs. "Could not resolve host" vs. a timeout message) is returned verbatim to the unprivileged caller in the RPC response.

### Impact Explanation
An attacker who can call `CreateRepositoryFromURL` (available to any user able to import/create a repository from a URL) can point `Url` at arbitrary internal/private addresses (e.g., cloud metadata endpoints, internal service ports) and use the differing stderr text returned in the gRPC error to distinguish "connection refused," "DNS/resolve failure," "timeout," or protocol-level failures. This constitutes SSRF-oracle behavior / information disclosure of internal network topology and reachability, matching a GitLab bounty "SSRF (blind, information disclosure)" impact class. Because outbound HTTP for `git clone` is not scheme/host-restricted at this Gitaly layer and redirect suppression (`http.followRedirects=false`) only limits redirect-following, not the initial destination, no existing check in this file stops the initial request from reaching internal hosts.

### Likelihood Explanation
Fully attacker-controlled and directly reachable: the attacker only needs standard permission to trigger repository creation from a URL (e.g. via project import), no special role, no secret, no MITM. The flow is deterministic and repeatable — every failed clone attempt returns the interpolated stderr in the response message, so the attacker can iterate hosts/ports to build a reachability map of Gitaly's network.

### Recommendation
Do not interpolate raw git stderr into the client-visible error message. Return a generic message (e.g., "cloning repository failed") to the RPC caller and place the stderr text and resolved address only in `WithMetadataItems`/log-only fields (which are already correctly not exposed to production clients). Additionally, consider adding network-destination validation/allow-listing for `CreateRepositoryFromURL`'s `Url` (e.g., rejecting loopback/link-local/private ranges unless explicitly permitted by config) consistent with GitLab Rails' existing URL-blocking for user-supplied import URLs.

### Proof of Concept
```go
func TestCreateRepositoryFromURL_LeaksInternalStderr(t *testing.T) {
    ctx := testhelper.Context(t)
    cfg, client := setupRepositoryServiceWithoutRepo(t) // existing test helper pattern

    repo := &gitalypb.Repository{StorageName: cfg.Storages[0].Name, RelativePath: gittest.NewRepositoryName(t)}

    // Point at an unreachable "internal" loopback port to simulate SSRF probing.
    _, err := client.CreateRepositoryFromURL(ctx, &gitalypb.CreateRepositoryFromURLRequest{
        Repository: repo,
        Url:        "http://127.0.0.1:1/internal-probe", // closed port -> "connection refused"
    })

    st, ok := status.FromError(err)
    require.True(t, ok)
    // FAILS today: st.Message() contains raw git stderr like
    // `cloning repository: exit status 128, stderr: "fatal: unable to access 'http://127.0.0.1:1/...': Failed to connect to 127.0.0.1 port 1: Connection refused\n"`
    require.NotContains(t, st.Message(), "Connection refused")
    require.NotContains(t, st.Message(), "127.0.0.1")
}
```
Running this against the current implementation shows the gRPC error message returned to the client contains the raw connection-establishment stderr and target address, confirming the leak.

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L21-85)
```go
func (s *server) cloneFromURLCommand(
	ctx context.Context,
	repoURL, resolvedAddress, repositoryFullPath, authorizationToken string, mirror bool,
	opts ...gitcmd.CmdOpt,
) (*command.Command, error) {
	cloneFlags := []gitcmd.Option{
		gitcmd.Flag{Name: "--quiet"},
	}

	if mirror {
		cloneFlags = append(cloneFlags, gitcmd.Flag{Name: "--mirror"})
	} else {
		cloneFlags = append(cloneFlags, gitcmd.Flag{Name: "--bare"})
	}

	u, err := url.Parse(repoURL)
	if err != nil {
		return nil, structerr.NewInternal("%w", err)
	}

	var config []gitcmd.ConfigPair
	if u.User != nil {
		password, hasPassword := u.User.Password()

		var creds string
		if hasPassword {
			creds = u.User.Username() + ":" + password
		} else {
			creds = u.User.Username()
		}

		u.User = nil
		authHeader := fmt.Sprintf("Authorization: Basic %s", base64.StdEncoding.EncodeToString([]byte(creds)))
		config = append(config, gitcmd.ConfigPair{Key: "http.extraHeader", Value: authHeader})
	} else if len(authorizationToken) > 0 {
		authHeader := fmt.Sprintf("Authorization: %s", authorizationToken)
		config = append(config, gitcmd.ConfigPair{Key: "http.extraHeader", Value: authHeader})
	}

	urlString := u.String()

	if resolvedAddress != "" {
		modifiedURL, resolveConfig, err := gitcmd.GetURLAndResolveConfig(u.String(), resolvedAddress)
		if err != nil {
			return nil, structerr.NewInvalidArgument("couldn't get curloptResolve config: %w", err)
		}

		urlString = modifiedURL
		config = append(config, resolveConfig...)
	}

	// Drop support for bundle URI when fetching from a remote repository.
	// Since the URI can point to any server, including localhost, this is to
	// prevent attack vectors that could abuse this mechanism.
	opts = append(opts, gitcmd.WithGlobalOption(gitcmd.ConfigPair{Key: "transfer.bundleURI", Value: "false"}))

	return s.gitCmdFactory.NewWithoutRepo(ctx,
		gitcmd.Command{
			Name:  "clone",
			Flags: cloneFlags,
			Args:  []string{urlString, repositoryFullPath},
		},
		append(opts, gitcmd.WithConfigEnv(config...))...,
	)
}
```

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L112-124)
```go
		if err := cmd.Wait(); err != nil {
			stderrStr := stderr.String()
			if remoteNotFoundRegex.MatchString(stderrStr) {
				return structerr.NewNotFound("cloning repository: repository at given URL not found").
					WithDetail(&gitalypb.CreateRepositoryFromURLError{
						Error: &gitalypb.CreateRepositoryFromURLError_RemoteNotFound{},
					})
			}

			return structerr.NewInternal("cloning repository: %w, stderr: %q", err, stderrStr).WithMetadataItems(
				structerr.MetadataItem{Key: "stderr", Value: stderrStr},
				structerr.MetadataItem{Key: "resolved_address", Value: req.GetResolvedAddress()},
			)
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

**File:** internal/structerr/error.go (L37-104)
```go
func newError(code codes.Code, format string, a ...any) Error {
	for i, arg := range a {
		err, ok := arg.(error)
		if !ok {
			continue
		}

		if errors.As(err, &(Error{})) {
			// We need to explicitly handle this, otherwise `status.FromError()` would
			// return these because we implement `GRPCStatus()`.
			continue
		}

		// If we see any wrapped gRPC error, then we retain its error code and details.
		// Note that we cannot use `status.FromError()` here, as that would only return an
		// error in case the immediate error is a gRPC status error.
		var wrappedGRPCStatus grpcStatuser
		if errors.As(err, &wrappedGRPCStatus) {
			grpcStatus := wrappedGRPCStatus.GRPCStatus()

			// The error message from gRPC errors is awkward because they include
			// RPC-specific constructs. This is awkward especially in the case where
			// these are embedded in the middle of an error message.
			//
			// So if we see that the top-level error is a gRPC error, then we only use
			// the status message as error message. But otherwise, we use the top-level
			// error message.
			message := err.Error()
			if st, ok := status.FromError(err); ok {
				message = st.Message()
			}

			var details []proto.Message
			for _, detail := range grpcStatus.Details() {
				if detailProto, ok := detail.(proto.Message); ok {
					details = append(details, detailProto)
				}
			}

			a[i] = Error{
				err:     errors.New(message),
				code:    grpcStatus.Code(),
				details: details,
			}
		}
	}

	formattedErr := fmt.Errorf(format, a...)

	// When we wrap an Error, we retain its error code. The intent of this is to retain the most
	// specific error code we have in the general case. As `Unknown` does not really count as a
	// specific error code, we will ignore these errors.
	//
	// Note that this impacts our middleware status handler, where we wrap non-context-errors
	// via `structerr.NewInternal()`. The result is that the caller should never see any
	// `Unknown` errors.
	var wrappedErr Error
	if errors.As(formattedErr, &wrappedErr) {
		if wrappedErr.code != codes.Unknown {
			code = wrappedErr.code
		}
	}

	return Error{
		err:  formattedErr,
		code: code,
	}
}
```
