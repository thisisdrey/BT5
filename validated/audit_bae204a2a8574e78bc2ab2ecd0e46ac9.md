### Title
SSRF via `transfer.bundleURI` in `FetchRemote` RPC (mirror pull from attacker URL) - ([File: internal/gitaly/service/repository/fetch_remote.go])

### Summary
`cloneFromURLCommand` in `create_repository_from_url.go` explicitly force-disables `transfer.bundleURI` when cloning from an attacker-supplied URL, with a comment stating this is done specifically "to prevent attack vectors that could abuse this mechanism" (SSRF via a malicious server advertising a bundle-uri pointing at an internal address). [1](#0-0)  The `FetchRemote` RPC path, which also performs a `git fetch` against a fully attacker-controlled URL (`req.GetRemoteParams().GetUrl()`), does not add this same override anywhere in its command construction.

### Finding Description
`buildCommandOpts` in `fetch_remote.go` builds the fetch configuration from `req.GetRemoteParams().GetUrl()` (attacker-controlled) and sets `remote.inmemory.url`/`remote.inmemory.fetch`, optional `http.<url>.extraHeader`, and calls `gitcmd.GetURLAndResolveConfig` for DNS-rebinding protection, but never appends a `transfer.bundleURI=false` global/config option. [2](#0-1)  This resulting `opts.CommandOptions` is passed straight into `localrepo.Repo.FetchRemote`, which executes `git fetch` against the `inmemory` remote. [3](#0-2)  The `clone` command description in `gitcmd` explicitly turns `transfer.bundleURI` **on** by default for every git-clone invocation Gitaly performs (`ConfigPair{Key: "transfer.bundleURI", Value: "true"}`), which is precisely why `cloneFromURLCommand` must explicitly force it back off when the target URL is attacker-controlled. [4](#0-3)  Because the "fetch" command description shares the same network-security configuration pattern as "clone" (the clone entry's comment "See 'fetch' for why we disable following redirects" cross-references it), the fetch code path likely negotiates the same Git-protocol bundle-uri capability during the object negotiation. If a malicious remote responds with a bundle-uri capability line pointing at an internal/loopback address, Gitaly's underlying `git fetch` process (not the Go HTTP client that already validates `resolved_address`) would issue its own HTTP request to that internal address — the exact SSRF vector that `cloneFromURLCommand` was patched to prevent, but here reachable through the fetch/mirror-pull path with no equivalent guard.

### Impact Explanation
This is a Server-Side Request Forgery from the Gitaly host itself: an unprivileged user who can configure a pull mirror or trigger `FetchRemote` (via GitLab Rails project mirroring/import features) toward a URL they control can make Gitaly's `git-fetch` process issue arbitrary outbound HTTP requests to internal-only addresses (e.g. cloud metadata endpoints, internal services), bypassing the `resolved_address`/`GetURLAndResolveConfig` DNS-rebinding protections that only cover the primary remote URL, not any bundle-uri redirect target advertised by the malicious remote.

### Likelihood Explanation
Preconditions match the described attacker capability: any user able to configure or trigger a pull from a remote they control (pull mirror, project import, or a direct `FetchRemote` RPC call with `remote_params.url` pointing at attacker infrastructure) can exploit this without any elevated privilege, secret, or peer compromise. The attack is fully repeatable since it only requires standing up an HTTP git server that advertises a malicious bundle-uri capability.

### Recommendation
Apply the same mitigation used in `cloneFromURLCommand`: in `buildCommandOpts` (or in `FetchRemote`/`localrepo.FetchRemote` generally, whenever the remote URL is externally/attacker controlled), append `gitcmd.WithGlobalOption(gitcmd.ConfigPair{Key: "transfer.bundleURI", Value: "false"})` to `opts.CommandOptions` before invoking git-fetch. Consider centralizing this so any code path performing `git fetch`/`git clone` against a caller-supplied URL is force-disabled for bundle-uri by default, rather than relying on each call site to remember the override.

### Proof of Concept
```go
func TestFetchRemote_BundleURI_SSRF(t *testing.T) {
    // Start a probe HTTP server representing an "internal" service.
    probeHit := make(chan struct{}, 1)
    probe := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        probeHit <- struct{}{}
    }))
    defer probe.Close()

    // Start a malicious git-http server that, during upload-pack advertisement,
    // includes a bundle-uri capability pointing at the probe server
    // (e.g. via a custom smart-HTTP handler serving:
    //   001e# service=git-upload-pack\n0000...bundle-uri...\n
    // with the URI set to probe.URL).
    maliciousGit := startMaliciousBundleURIServer(t, probe.URL)
    defer maliciousGit.Close()

    cfg, client := setupRepositoryService(t)
    repo, _ := gittest.CreateRepository(t, ctx, cfg)

    _, err := client.FetchRemote(ctx, &gitalypb.FetchRemoteRequest{
        Repository: repo,
        RemoteParams: &gitalypb.RemoteParams{
            Url: maliciousGit.URL,
        },
    })
    require.NoError(t, err)

    select {
    case <-probeHit:
        t.Fatal("SSRF: probe endpoint was hit via bundle-uri during FetchRemote")
    case <-time.After(2 * time.Second):
        // expected: probe never hit if transfer.bundleURI=false is applied
    }
}
```
Compare against an equivalent test invoking `CreateRepositoryFromURL` with the same malicious server — the probe should never be hit there because of the existing override at `create_repository_from_url.go:75`, demonstrating the inconsistency between the two code paths.

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

**File:** internal/git/localrepo/remote.go (L79-138)
```go
func (repo *Repo) FetchRemote(ctx context.Context, remoteName string, opts FetchOpts) error {
	if err := validateNotBlank(remoteName, "remoteName"); err != nil {
		return err
	}

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

	return nil
}
```

**File:** internal/git/gitcmd/command_description.go (L61-71)
```go
	"clone": {
		opts: func(ctx context.Context) []GlobalOption {
			return append(append([]GlobalOption{
				// See "init" for why we set the template directory to the empty string.
				ConfigPair{Key: "init.templateDir", Value: ""},
				// See "fetch" for why we disable following redirects.
				ConfigPair{Key: "http.followRedirects", Value: "false"},
				ConfigPair{Key: "transfer.bundleURI", Value: "true"},
			}, packConfiguration(ctx)...), fetchFsckConfiguration(ctx)...)
		},
	},
```
