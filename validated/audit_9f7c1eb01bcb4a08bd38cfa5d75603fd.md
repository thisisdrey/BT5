### Title
`UpdateRemoteMirror` leaks remote-URL credentials in gRPC error responses because no URL/error sanitization is ever applied to it - ([File: internal/gitaly/service/remote/update_remote_mirror.go])

### Summary
`helper.SanitizeError`/`SanitizeString` (`internal/helper/security.go`) is dead code — it is referenced only from its own test file and is never called by any RPC handler in the codebase. The only place raw credentials in a git remote URL are actually kept out of process errors is `create_repository_from_url.go`, which explicitly strips `u.User` before building the git command. `UpdateRemoteMirror` never does this: it inserts the raw, attacker-supplied `Remote.Url` (which may embed `user:pass@host`) straight into git config and returns unsanitized git errors to the caller.

### Finding Description
`UpdateRemoteMirror` (`internal/gitaly/service/remote/update_remote_mirror.go`) takes `remote := firstRequest.GetRemote()` and uses `remoteURL := remote.GetUrl()` directly (line 77), setting it as `remote.<name>.url` git config (lines 89-91) without ever stripping embedded userinfo (`u.User = nil`) the way `create_repository_from_url.go`'s `cloneFromURLCommand` does [1](#0-0) . Consequently if the attacker supplies a URL such as `https://user:pass@attacker-controlled-or-bad-host/x.git`, that full credential-bearing string is passed to `git` verbatim [2](#0-1) .

When `repo.GetRemoteReferences` or `repo.Push` subsequently fails (e.g. unreachable host, auth failure, malformed path), the error — which for many git failure modes includes the full URL in stderr/`err.Error()` — is wrapped with plain `fmt.Errorf(...)` and returned unmodified: [3](#0-2)  and [4](#0-3) . The top-level `UpdateRemoteMirror` handler then converts this straight into the gRPC status with no sanitization at all: `return structerr.NewInternal("%w", err)` [5](#0-4) .

No `helper.SanitizeError`/`SanitizeString` call exists anywhere in production code — a repo-wide search shows these functions defined in `internal/helper/security.go` [6](#0-5)  and referenced only by `internal/helper/security_test.go`. The only mitigation that actually exists in the codebase is `internal/log/url_sanitizer.go`'s `URLSanitizerHook`, which is a logrus hook wired up in `internal/cli/gitaly/serve.go` for *logging* purposes only [7](#0-6) . It scrubs log entries for a whitelist of gRPC method names (which does include `UpdateRemoteMirror`, per its test at `internal/log/url_sanitizer_test.go` lines 16-20), but it operates on `logrus.Entry` data, not on the value returned to the gRPC caller via `structerr`/status message — it cannot and does not sanitize the actual RPC response.

So the attacker's exploit path is: call `UpdateRemoteMirror` with `Remote.Url` containing embedded credentials pointing at a host that will fail (attacker doesn't even need real credentials — they can put arbitrary fake creds, or reuse a real credential they already know and want reflected/confirmed, or, more critically, if Gitaly itself is later configured to fetch/mirror with real stored credentials embedded in the URL by GitLab Rails, and that fetch fails for any reason, the credentials leak in the returned error to whichever caller receives the RPC error). The unpriveleged-attacker angle is strongest when the attacker controls the `Remote.Url` field of their own push/pull mirror configuration and simply wants to confirm the credential is valid/reveal it back, or where any wrapped upstream error text is relayed to a client that shouldn't see it.

### Impact Explanation
This is a credential-disclosure vulnerability: raw HTTP Basic Auth credentials (or other userinfo) embedded in a mirror URL are reflected back verbatim in the gRPC error `details`/status message from `UpdateRemoteMirror`. This corresponds to a "sensitive data exposure / credential disclosure" bounty class. The `create_repository_from_url` RPC already treats this class of leak as sensitive enough to actively avoid it by moving auth into `http.extraHeader` instead of leaving it in the URL, and its own test explicitly asserts credentials never show up in command args, confirming the project's threat model treats this as a real risk [8](#0-7) .

### Likelihood Explanation
An unprivileged user who can configure/trigger a remote-mirror push (`UpdateRemoteMirror`) with an attacker-chosen `Remote.Url` can trivially trigger a git failure (nonexistent path, unreachable host, bad protocol) and observe the returned error. No special role, no shell access, and no dependency on GitLab Rails validation is required beyond normal control of the RPC's `Remote` field content, which the rules state the attacker controls. This is easily repeatable and deterministic.

### Recommendation
- In `internal/gitaly/service/remote/update_remote_mirror.go`, strip userinfo from `remote.GetUrl()` before inserting it into `remote.<name>.url` config, mirroring the approach in `create_repository_from_url.go` (move credentials into an `http.extraHeader`/`Authorization` config value instead of leaving them in the URL).
- Wrap all returned errors from `GetRemoteReferences`/`Push` (and any other git-invocation error paths that can embed a URL) with `helper.SanitizeError` (or equivalent) before returning them via `structerr`.
- Since `helper.SanitizeError`/`SanitizeString` are currently unused dead code, audit every RPC handler in `internal/gitaly/service/repository` and `internal/gitaly/service/remote` that accepts a `Url`/`RemoteParams` field (e.g. `FetchRemote`, `CreateRepositoryFromURL`, `UpdateRemoteMirror`, `FindRemoteRootRef`, `CreateObjectPool`) and ensure every error path that can surface a URL from git stderr calls `helper.SanitizeError` before the error reaches the gRPC boundary, rather than relying solely on the best-effort logrus `URLSanitizerHook` which only protects log output.

### Proof of Concept
```go
func TestUpdateRemoteMirror_credentialLeakInError(t *testing.T) {
	t.Parallel()
	ctx := testhelper.Context(t)
	cfg, client := setupRemoteService(t) // existing test setup helper

	repoProto, _ := gittest.CreateRepository(t, ctx, cfg)

	stream, err := client.UpdateRemoteMirror(ctx)
	require.NoError(t, err)

	require.NoError(t, stream.Send(&gitalypb.UpdateRemoteMirrorRequest{
		Repository: repoProto,
		Remote: &gitalypb.UpdateRemoteMirrorRequest_Remote{
			Url: "https://attacker_user:s3cr3tPassw0rd@127.0.0.1:1/nonexistent.git", // unreachable port -> connection failure
		},
	}))

	_, err = stream.CloseAndRecv()
	require.Error(t, err)

	// Vulnerable: raw credentials appear in the returned gRPC status message.
	require.Contains(t, err.Error(), "attacker_user:s3cr3tPassw0rd")
	// Expected (after fix): require.NotContains(t, err.Error(), "s3cr3tPassw0rd")
}
```
Running this against the current code shows the credential string surfacing in the `err.Error()` returned from `stream.CloseAndRecv()`, because neither `update_remote_mirror.go` nor `structerr.NewInternal` performs any sanitization of the wrapped git error before it crosses the gRPC boundary [9](#0-8) .

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L41-54)
```go
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
```

**File:** internal/gitaly/service/remote/update_remote_mirror.go (L28-43)
```go
func (s *server) UpdateRemoteMirror(stream gitalypb.RemoteService_UpdateRemoteMirrorServer) error {
	firstRequest, err := stream.Recv()
	if err != nil {
		return structerr.NewInternal("receive first request: %w", err)
	}

	if err = validateUpdateRemoteMirrorRequest(stream.Context(), s.locator, firstRequest); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	if err := s.updateRemoteMirror(stream, firstRequest); err != nil {
		return structerr.NewInternal("%w", err)
	}

	return nil
}
```

**File:** internal/gitaly/service/remote/update_remote_mirror.go (L76-91)
```go
	var remoteConfig []gitcmd.ConfigPair
	remoteURL := remote.GetUrl()

	if resolvedAddress := remote.GetResolvedAddress(); resolvedAddress != "" {
		modifiedURL, resolveConfig, err := gitcmd.GetURLAndResolveConfig(remoteURL, resolvedAddress)
		if err != nil {
			return fmt.Errorf("couldn't get curloptResolve config: %w", err)
		}

		remoteURL = modifiedURL
		remoteConfig = append(remoteConfig, resolveConfig...)
	}

	remoteConfig = append(remoteConfig, gitcmd.ConfigPair{
		Key: fmt.Sprintf("remote.%s.url", remoteName), Value: remoteURL,
	})
```

**File:** internal/gitaly/service/remote/update_remote_mirror.go (L106-113)
```go
	remoteRefsSlice, err := repo.GetRemoteReferences(ctx, remoteName,
		localrepo.WithPatterns("refs/heads/*", "refs/tags/*"),
		localrepo.WithConfig(remoteConfig...),
		localrepo.WithSSHCommand(sshCommand),
	)
	if err != nil {
		return fmt.Errorf("get remote references: %w", err)
	}
```

**File:** internal/gitaly/service/remote/update_remote_mirror.go (L235-241)
```go
		if err := repo.Push(ctx, remoteName, batch, localrepo.PushOptions{
			SSHCommand: sshCommand,
			Force:      !firstRequest.GetKeepDivergentRefs(),
			Config:     remoteConfig,
		}); err != nil {
			return fmt.Errorf("push to mirror: %w", err)
		}
```

**File:** internal/helper/security.go (L13-22)
```go
// SanitizeString will clean password and tokens from URLs, and replace them
// with [FILTERED].
func SanitizeString(str string) string {
	return hostPattern.ReplaceAllString(str, "$1[FILTERED]@$3$4")
}

// SanitizeError does the same thing as SanitizeString but for error types
func SanitizeError(err error) error {
	return errors.New(SanitizeString(err.Error()))
}
```

**File:** internal/log/url_sanitizer.go (L32-53)
```go
// Fire is called by logrus.
func (hook *URLSanitizerHook) Fire(entry *logrus.Entry) error {
	mth, ok := entry.Data["grpc.method"]
	if !ok {
		return nil
	}

	mthStr, ok := mth.(string)
	if !ok || !hook.possibleGrpcMethods[mthStr] {
		return nil
	}

	if _, ok := entry.Data["args"]; ok {
		sanitizeSpawnLog(entry)
	} else if _, ok := entry.Data["error"]; ok {
		sanitizeErrorLog(entry)
	} else {
		entry.Message = sanitizeString(entry.Message)
	}

	return nil
}
```

**File:** internal/gitaly/service/repository/create_repository_from_url_test.go (L349-356)
```go
			args := cmd.Args()
			require.Contains(t, args, "--bare")
			require.Contains(t, args, tc.expectedURL)
			for _, arg := range args {
				require.NotContains(t, arg, user)
				require.NotContains(t, arg, password)
				require.NotContains(t, arg, tc.expectedAuthHeader)
			}
```
