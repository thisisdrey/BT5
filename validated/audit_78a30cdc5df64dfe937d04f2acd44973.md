### Title
FetchRemote RPC omits the `transfer.bundleURI=false` guard present in CreateRepositoryFromURL, allowing SSRF via attacker-controlled bundle-URI capability - ([File: internal/gitaly/service/repository/fetch_remote.go])

### Summary
`cloneFromURLCommand` explicitly disables Git's bundle-URI feature via `gitcmd.WithGlobalOption(gitcmd.ConfigPair{Key: "transfer.bundleURI", Value: "false"})` to prevent a malicious remote from redirecting the client to fetch from an internal/localhost bundle URI. `FetchRemote`'s `buildCommandOpts` (in `internal/gitaly/service/repository/fetch_remote.go`) and the underlying `localrepo.Repo.FetchRemote`/`FetchOpts.buildFlags` construct the fetch's config/flags without ever setting `transfer.bundleURI=false`, so no equivalent mitigation exists for this sibling RPC.

### Finding Description
`CreateRepositoryFromURL` → `cloneFromURLCommand` (lines 72-75) appends a global git config disabling `transfer.bundleURI` before invoking `git clone` against an attacker-supplied URL, specifically to stop a malicious server from advertising a `bundle-uri` capability that points the client at an internal address. [1](#0-0) 

`FetchRemote` reaches the same class of untrusted input: `req.GetRemoteParams().GetUrl()` is attacker-controlled (a repo owner can register any remote URL for their own repository and trigger a pull mirror update or explicit `FetchRemote` call), and this flows into `buildCommandOpts`, which only sets `remote.inmemory.fetch`, `remote.inmemory.url`, optional resolve config, and auth headers — no `transfer.bundleURI` config. [2](#0-1) 

The resulting `opts.CommandOptions` is passed into `quarantineRepo.FetchRemote(ctx, "inmemory", opts)`, which builds the actual `git fetch` invocation in `internal/git/localrepo/remote.go`. That function only sets `advice.fetchShowForcedUpdates` and `remote.<name>.followremotehead`, again without disabling `transfer.bundleURI`. [3](#0-2) 

Because modern Git supports server-advertised bundle URIs during protocol v2 negotiation (`transfer.bundleURI`), a malicious remote controlled by the attacker (their own upstream/fork used as the fetch source) can advertise a bundle URI pointing to an internal address (e.g., `http://169.254.169.254/...` or `http://localhost:PORT/...`). Unlike `CreateRepositoryFromURL`, `FetchRemote` does not disable this feature, so the Gitaly host's `git fetch` process would attempt to retrieve the bundle from the attacker-specified internal URL — a server-side request forgery originating from the Gitaly node.

### Impact Explanation
This is a Server-Side Request Forgery (SSRF) primitive: an attacker who controls only their own repository's remote configuration (a standard, unprivileged capability — setting up a pull mirror or invoking `FetchRemote`/mirror-update flows) can cause the Gitaly server process to issue an outbound HTTP(S) request to an arbitrary internal address dictated by the attacker's malicious Git server. This matches GitLab's SSRF impact class and could be leveraged to reach internal services, cloud metadata endpoints, or other network-restricted resources reachable from the Gitaly host.

### Likelihood Explanation
Feasible and repeatable: the attacker needs only to control the content served by the remote endpoint used in a `FetchRemoteRequest` (e.g., their own fork/mirror source configured via GitLab's standard pull-mirror feature, which ultimately invokes this RPC), no elevated privileges, no secrets, and no race conditions. The exploit is fully attacker-driven — they choose when to advertise the bundle-URI capability and to what internal address it should point.

### Recommendation
Add the same mitigation used in `cloneFromURLCommand` to `FetchRemote`'s config building — append `gitcmd.WithGlobalOption(gitcmd.ConfigPair{Key: "transfer.bundleURI", Value: "false"})` (or equivalently `gitcmd.WithConfig(...)`) to `opts.CommandOptions` in `buildCommandOpts` in `internal/gitaly/service/repository/fetch_remote.go`, and consider adding it centrally in `localrepo.Repo.FetchRemote` (`internal/git/localrepo/remote.go`) so all fetch paths (`FetchRemote`, `FetchInternal`, mirror-update flows) are protected consistently rather than requiring each call site to remember the guard.

### Proof of Concept
```go
// internal/gitaly/service/repository/fetch_remote_bundleuri_test.go
func TestFetchRemote_BundleURISSRF(t *testing.T) {
    ctx := testhelper.Context(t)
    cfg, client := setupRepositoryService(t)

    // Start a malicious "remote" HTTP git server that, during the
    // smart-HTTP v2 handshake, advertises a bundle-uri capability
    // pointing at an internal listener (simulating cloud metadata /
    // internal service), e.g. http://127.0.0.1:<internalPort>/secret.bundle.
    maliciousRemote, internalHits := startMaliciousBundleURIServer(t)

    repoProto, repoPath := gittest.CreateRepository(t, ctx, cfg)

    _, err := client.FetchRemote(ctx, &gitalypb.FetchRemoteRequest{
        Repository: repoProto,
        RemoteParams: &gitalypb.RemoteParams{
            Url: maliciousRemote.URL,
        },
    })
    require.NoError(t, err)

    // Expected (mitigated) behavior: internalHits should be 0, because
    // transfer.bundleURI=false should have prevented git-fetch from
    // following the advertised bundle URI.
    require.Equal(t, 0, internalHits.Load(),
        "FetchRemote followed attacker-advertised bundle URI to an internal address")
}
```
Run against the current code: since `buildCommandOpts` in `fetch_remote.go` never sets `transfer.bundleURI=false`, `git-fetch` processes the bundle-uri advertisement and issues a request to `internalHits`'s listener, causing the assertion to fail — demonstrating the SSRF gap relative to `CreateRepositoryFromURL`'s explicit mitigation.

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

**File:** internal/git/localrepo/remote.go (L94-119)
```go
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
```
