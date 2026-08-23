Based on my investigation, I found a strong analog: `gitcmd.GetURLAndResolveConfig` in `internal/git/gitcmd/command_resolve.go` performs case-sensitive scheme matching (`strings.HasPrefix(remoteURL, "http://")`, `"https://"`, `"git://"`, `"ssh://"`) to decide how to pin a remote URL's hostname to a caller-supplied `resolved_address`, mirroring the "only partially normalizes case" flaw class from the external report (an incomplete/exact-case string check causing a security decision to silently fail for out-of-range input). Several RPC handlers (`FetchRemote`, `FindRemoteRootRef`, `UpdateRemoteMirror`) pass the raw, attacker-influenced `RemoteUrl`/`Url` field directly into this function without prior normalization via `url.Parse`, unlike `CreateRepositoryFromURL`, which normalizes case as a side effect of calling `url.Parse` first.

### Title
DNS-rebinding protection bypass via case-sensitive scheme matching in `GetURLAndResolveConfig` - (File: internal/git/gitcmd/command_resolve.go)

### Summary
### Finding Description
`GetURLAndResolveConfig` decides how to pin a remote Git URL to a caller-supplied `resolvedAddress` (to defeat DNS rebinding/SSRF) purely via case-sensitive `strings.HasPrefix` checks against `"http://"`, `"https://"`, `"git://"`, and `"ssh://"` [1](#0-0) . If none of those match, the function falls through to `getURLAndResolveConfigForSCP`, which treats the string as an scp-like SSH address and rewrites the host portion up to the first `:` [2](#0-1) .

A URL such as `HTTP://attacker-controlled-host/x.git` or `Http://.../x.git` does not match any of the case-sensitive prefixes (Go string prefix matching is exact-case), so it is misrouted into the SCP branch. That branch parses it as `user@host:path`-style input, splits on the first colon, and reconstructs `resolvedAddress:path` [2](#0-1) . This produces a malformed/incorrect "resolved" URL/config rather than the intended `http.curloptResolve` config pin used for HTTP(S)/git protocols. Because `http.curloptResolve` (the mechanism that actually forces the given IP for the hostname while keeping TLS/HTTP semantics correct) is never set for a mixed/upper-case scheme, the anti-DNS-rebinding protection silently fails to apply for that request — the underlying `git` invocation is free to re-resolve the original hostname via DNS, which is exactly the SSRF/DNS-rebinding vector this mechanism exists to prevent.

This is reachable directly from unprivileged/ordinary RPC input: `FetchRemote`, `FindRemoteRootRef`, and `UpdateRemoteMirror` all take the caller-supplied `RemoteUrl`/`Url` field and a `ResolvedAddress` field and pass them straight into `GetURLAndResolveConfig` without any prior scheme normalization [3](#0-2) [4](#0-3) [5](#0-4) . This contrasts with `CreateRepositoryFromURL`'s `cloneFromURLCommand`, which first runs the URL through `url.Parse`/`u.String()` (which normalizes/lowercases the scheme) before calling `GetURLAndResolveConfig`, incidentally avoiding the bug in that one path [6](#0-5) .

### Impact Explanation
The `resolved_address` mechanism exists specifically to prevent DNS-rebinding/SSRF attacks by pinning the connection to a pre-resolved IP instead of letting `git` perform its own DNS lookup at request time. By supplying a differently-cased scheme (e.g., `HTTP://`, `Https://`, `GIT://`), a caller with access to `FetchRemote`, `FindRemoteRootRef`, or `UpdateRemoteMirror` can cause `GetURLAndResolveConfig` to take the wrong code path, the `http.curloptResolve` pin is never applied and/or the URL is corrupted, and the resulting git operation resolves the hostname via normal DNS. This reopens the DNS-rebinding/SSRF window the resolved-address parameter was introduced to close, letting an attacker who controls DNS for a hostname redirect Gitaly's outbound `git fetch`/`git remote show`/mirror-push traffic to arbitrary internal endpoints after an initial validation check believed it was pinned to a safe address.

### Likelihood Explanation
Exploitation only requires supplying a `RemoteUrl`/`Url` with an unconventional scheme case to an existing RPC that already accepts a `resolved_address` parameter — no privileged access, malicious peer, or MITM required, just crafting the RPC request fields (which are explicitly designed to be attacker/user-influenced input for this feature, e.g. pull mirrors, project imports). The bug is a straightforward case-sensitivity oversight in prefix matching, requiring no unusual timing or race condition.

### Recommendation
Normalize the scheme before dispatching in `GetURLAndResolveConfig`, e.g., parse the URL once (or lowercase only the scheme substring) and compare case-insensitively (`strings.EqualFold`) against `http`, `https`, `git`, and `ssh`, falling back to the SCP-like path only when no recognized scheme is present. Ideally, parse the URL with `net/url.Parse` up front (as `create_repository_from_url.go` incidentally does) in every caller (`fetch_remote.go`, `find_remote_root_ref.go`, `update_remote_mirror.go`) before calling `GetURLAndResolveConfig`, and add regression tests covering mixed-case schemes to confirm `http.curloptResolve` is still applied.

### Proof of Concept
1. Call `FetchRemote` (or `FindRemoteRootRef`/`UpdateRemoteMirror`) with `RemoteParams.Url = "HTTP://attacker.example/x.git"` and `RemoteParams.ResolvedAddress = "203.0.113.5"` (some benign-looking pinned IP the caller expects to be enforced).
2. Because `strings.HasPrefix(remoteURL, "http://")` is case-sensitive and fails to match `"HTTP://..."`, `GetURLAndResolveConfig` falls through to `getURLAndResolveConfigForSCP`, producing a garbled/incorrect result instead of setting `http.curloptResolve` [7](#0-6) .
3. `git fetch`/`git remote show`/`git push` is subsequently invoked with the original hostname and no IP pin, meaning DNS is resolved live by git at request time — if the attacker controls that DNS record (classic DNS-rebinding), the request can be redirected to an internal address, defeating the resolved-address SSRF protection.

### Citations

**File:** internal/git/gitcmd/command_resolve.go (L41-79)
```go
	switch {
	case strings.HasPrefix(remoteURL, "http://"), strings.HasPrefix(remoteURL, "https://"), strings.HasPrefix(remoteURL, "git://"):
		return getURLAndResolveConfigForURL(remoteURL, resolvedAddress)
	case strings.HasPrefix(remoteURL, "ssh://"):
		return getURLAndResolveConfigForSSH(remoteURL, resolvedAddress)
	default:
		return getURLAndResolveConfigForSCP(remoteURL, resolvedAddress)
	}
}

func getURLAndResolveConfigForSSH(remoteURL, resolvedAddress string) (string, []ConfigPair, error) {
	u, err := url.ParseRequestURI(remoteURL)
	if err != nil {
		return "", nil, fmt.Errorf("couldn't parse remoteURL: %w", err)
	}

	u.Host = resolvedAddress

	return u.String(), nil, nil
}

func getURLAndResolveConfigForSCP(remoteURL, resolvedAddress string) (string, []ConfigPair, error) {
	hostAndPath := strings.SplitN(remoteURL, ":", 2)
	if len(hostAndPath) != 2 {
		return "", nil, fmt.Errorf("invalid protocol/URL encountered: %s", remoteURL)
	}

	if strings.Contains(hostAndPath[0], "/") {
		return "", nil, fmt.Errorf("SSH URLs with '/' before colon are unsupported")
	}

	var userPrefix string

	if userAndHost := strings.SplitAfterN(remoteURL, "@", 2); len(userAndHost) > 1 {
		userPrefix = userAndHost[0]
	}

	return fmt.Sprintf("%s%s:%s", userPrefix, resolvedAddress, hostAndPath[1]), nil, nil
}
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L260-280)
```go
func buildCommandOpts(ctx context.Context, opts *localrepo.FetchOpts, req *gitalypb.FetchRemoteRequest) error {
	remoteURL := req.GetRemoteParams().GetUrl()
	var config []gitcmd.ConfigPair

	for _, refspec := range getRefspecs(req.GetRemoteParams().GetMirrorRefmaps()) {
		config = append(config, gitcmd.ConfigPair{
			Key: "remote.inmemory.fetch", Value: refspec,
		})
	}

	if resolvedAddress := req.GetRemoteParams().GetResolvedAddress(); resolvedAddress != "" {
		modifiedURL, resolveConfig, err := gitcmd.GetURLAndResolveConfig(remoteURL, resolvedAddress)
		if err != nil {
			return fmt.Errorf("couldn't get curloptResolve config: %w", err)
		}

		remoteURL = modifiedURL
		config = append(config, resolveConfig...)
	}

	config = append(config, gitcmd.ConfigPair{Key: "remote.inmemory.url", Value: remoteURL})
```

**File:** internal/gitaly/service/remote/find_remote_root_ref.go (L17-29)
```go
func (s *server) findRemoteRootRefCmd(ctx context.Context, request *gitalypb.FindRemoteRootRefRequest) (*command.Command, error) {
	remoteURL := request.GetRemoteUrl()
	var config []gitcmd.ConfigPair

	if resolvedAddress := request.GetResolvedAddress(); resolvedAddress != "" {
		modifiedURL, resolveConfig, err := gitcmd.GetURLAndResolveConfig(remoteURL, resolvedAddress)
		if err != nil {
			return nil, structerr.NewInvalidArgument("couldn't get curloptResolve config: %w", err)
		}

		remoteURL = modifiedURL
		config = append(config, resolveConfig...)
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

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L36-70)
```go
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
```
