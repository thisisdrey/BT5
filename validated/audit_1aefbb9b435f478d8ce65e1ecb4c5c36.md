Based on my investigation, only `create_repository_from_url.go` sets `transfer.bundleURI=false` before cloning from an attacker-controllable URL; `fetch_remote.go` (`buildCommandOpts`), `update_remote_mirror.go`, and `find_remote_root_ref.go` all construct in-memory git remotes from equally attacker-controllable URLs but do not apply the same mitigation.

### Title
FetchRemote/UpdateRemoteMirror/FindRemoteRootRef do not disable `transfer.bundleURI`, allowing SSRF via malicious remote server bundle-URI advertisement - ([File: internal/gitaly/service/repository/fetch_remote.go])

### Summary
`CreateRepositoryFromURL` was hardened by explicitly setting `transfer.bundleURI=false` when cloning from a caller-supplied URL, with the code comment stating this prevents "attack vectors" because "the URI can point to any server, including localhost." [1](#0-0)  This same class of RPCs — `FetchRemote`, `UpdateRemoteMirror`, and `FindRemoteRootRef` — all build an in-memory git remote from a request-supplied URL and execute `git fetch`/`git remote show` against it, but none of them apply the equivalent `transfer.bundleURI=false` mitigation.

### Finding Description
Git's bundle-URI feature (protocol v2 capability) lets a remote server, during clone/fetch negotiation, advertise a secondary URI that the local git client will separately fetch from to bootstrap objects. If unrestricted, a malicious or compromised remote server can advertise an arbitrary bundle URI (e.g. pointing at Gitaly's internal metadata/loopback services or other internal-only hosts), causing the local `git-fetch`/`git-clone` process spawned by Gitaly to make an outbound request to that attacker-chosen destination — a classic SSRF primitive originating from the Gitaly host itself.

Gitaly's own changelog and code comment confirm this exact risk was already recognized and patched for `CreateRepositoryFromURL`: [1](#0-0) 

However, `buildCommandOpts` in `fetch_remote.go`, used by the `FetchRemote` RPC (invoked for pull-mirror synchronization against a remote URL supplied in `FetchRemoteRequest.RemoteParams.Url`), constructs the fetch config without disabling bundle URIs: [2](#0-1) 

Similarly, `UpdateRemoteMirror` builds its remote config from `Remote.GetUrl()` with no bundle-URI restriction: [3](#0-2)  and `FindRemoteRootRef` does the same: [4](#0-3) 

All three of these entry points take a `resolved_address` field intended only to prevent DNS-rebinding on the *primary* remote host, via `gitcmd.GetURLAndResolveConfig` [5](#0-4)  — this protects against DNS rebinding of the main remote URL but does nothing to prevent the remote server from directing the git client to a *second*, entirely different, attacker-chosen bundle URI once the connection is established, since `transfer.bundleURI` remains enabled by default.

### Impact Explanation
A malicious or compromised git remote (reachable via a project's pull mirror configuration, a "clone/fetch from a remote" workflow, or any caller able to invoke `FetchRemote`/`UpdateRemoteMirror`/`FindRemoteRootRef` with an externally-controlled URL) can advertise a bundle URI pointing at internal-only Gitaly-host-reachable services (metadata endpoints, other internal RPC ports, `file://` in some git bundle URI configurations, etc.), causing Gitaly to issue outbound requests on its behalf. This is the same SSRF/vector class Gitaly's maintainers already fixed once for `CreateRepositoryFromURL`, but the fix was not applied consistently across the other RPCs that build equivalent in-memory git remotes from attacker/tenant-controlled URLs.

### Likelihood Explanation
`FetchRemote` is the standard mechanism backing GitLab pull mirrors, so any project owner configuring a pull mirror pointing at an attacker-controlled git server can trigger this path without needing any special Gitaly-internal privilege beyond ordinary project/mirror configuration — the "swapTarget" analog here is the un-denylisted "target" being the second-stage bundle URI advertised by the attacker's git server, which Gitaly's fetch/remote-show code paths do not block, unlike the sibling `CreateRepositoryFromURL` path that already treats this as a known, must-fix risk.

### Recommendation
Apply the same `gitcmd.WithGlobalOption(gitcmd.ConfigPair{Key: "transfer.bundleURI", Value: "false"})` mitigation used in `create_repository_from_url.go` to the `FetchRemote` (`buildCommandOpts`), `UpdateRemoteMirror`, and `FindRemoteRootRef` code paths so that no code path executing git operations against a request-supplied remote URL trusts a remote-advertised bundle URI.

### Proof of Concept
1. Configure (or invoke directly via gRPC) a pull mirror / `FetchRemoteRequest` whose `RemoteParams.Url` points to an attacker-controlled git HTTP/SSH server.
2. The attacker's git server, during the protocol v2 capability advertisement, includes a `bundle-uri` value pointing at an internal address only reachable from the Gitaly host (e.g. an internal admin endpoint or loopback service).
3. Because `transfer.bundleURI` is not disabled in `buildCommandOpts` (`internal/gitaly/service/repository/fetch_remote.go`), the local `git-fetch` process spawned by Gitaly follows the advertised bundle URI and issues a request to the internal target — demonstrating SSRF originating from the Gitaly server, the same vector already remediated in `CreateRepositoryFromURL` but left open here.

Note: I could not access the referenced GitHub commit (`c5ce7f5a396b2e8a942be5bc591dafa20b4395f4`) to confirm the exact scope of the original fix commit beyond the changelog entry and the surviving code comment/config in `create_repository_from_url.go`; this is based on static comparison of the current code paths in the indexed repository snapshot.

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L72-75)
```go
	// Drop support for bundle URI when fetching from a remote repository.
	// Since the URI can point to any server, including localhost, this is to
	// prevent attack vectors that could abuse this mechanism.
	opts = append(opts, gitcmd.WithGlobalOption(gitcmd.ConfigPair{Key: "transfer.bundleURI", Value: "false"}))
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L260-304)
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

	if featureflag.FetchRemoteProactiveAuth.IsEnabled(ctx) {
		if u, err := url.Parse(remoteURL); err == nil && u.User != nil {
			password, _ := u.User.Password()
			if u.User.Username() != "" && password != "" {
				config = append(config, gitcmd.ConfigPair{
					Key:   "http.proactiveAuth",
					Value: "basic",
				})
			}
		}
	}

	if authHeader := req.GetRemoteParams().GetHttpAuthorizationHeader(); authHeader != "" {
		config = append(config, gitcmd.ConfigPair{
			Key:   fmt.Sprintf("http.%s.extraHeader", req.GetRemoteParams().GetUrl()),
			Value: "Authorization: " + authHeader,
		})
	}

	opts.CommandOptions = append(opts.CommandOptions, gitcmd.WithConfigEnv(config...))

	return nil
}
```

**File:** internal/gitaly/service/remote/update_remote_mirror.go (L76-98)
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

	if authHeader := remote.GetHttpAuthorizationHeader(); authHeader != "" {
		remoteConfig = append(remoteConfig, gitcmd.ConfigPair{
			Key:   fmt.Sprintf("http.%s.extraHeader", remote.GetUrl()),
			Value: "Authorization: " + authHeader,
		})
	}
```

**File:** internal/gitaly/service/remote/find_remote_root_ref.go (L17-38)
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

	config = append(config, gitcmd.ConfigPair{Key: "remote.inmemory.url", Value: remoteURL})

	if authHeader := request.GetHttpAuthorizationHeader(); authHeader != "" {
		config = append(config, gitcmd.ConfigPair{
			Key:   fmt.Sprintf("http.%s.extraHeader", request.GetRemoteUrl()),
			Value: "Authorization: " + authHeader,
		})
	}
```

**File:** internal/git/gitcmd/command_resolve.go (L27-49)
```go
func GetURLAndResolveConfig(remoteURL string, resolvedAddress string) (string, []ConfigPair, error) {
	if remoteURL == "" {
		return "", nil, fmt.Errorf("URL is empty")
	}

	if resolvedAddress == "" {
		return "", nil, fmt.Errorf("resolved address is empty")
	}

	resolvedIP := net.ParseIP(resolvedAddress)
	if resolvedIP == nil {
		return "", nil, fmt.Errorf("resolved address has invalid IPv4/IPv6 address")
	}

	switch {
	case strings.HasPrefix(remoteURL, "http://"), strings.HasPrefix(remoteURL, "https://"), strings.HasPrefix(remoteURL, "git://"):
		return getURLAndResolveConfigForURL(remoteURL, resolvedAddress)
	case strings.HasPrefix(remoteURL, "ssh://"):
		return getURLAndResolveConfigForSSH(remoteURL, resolvedAddress)
	default:
		return getURLAndResolveConfigForSCP(remoteURL, resolvedAddress)
	}
}
```
