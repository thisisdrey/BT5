### Title
FetchRemote does not disable `transfer.bundleURI`, allowing SSRF via bundle-URI redirection during remote fetch - (File: internal/gitaly/service/repository/fetch_remote.go)

### Summary
`CreateRepositoryFromURL` and `FetchRemote` are two sibling Gitaly RPCs that both perform a Git operation against a user-supplied remote URL (`git clone` vs `git fetch`). A security fix was applied only to `CreateRepositoryFromURL`, which explicitly disables `transfer.bundleURI` before invoking `git clone` to prevent the Git bundle-URI mechanism from being abused as an SSRF vector [1](#0-0) . `FetchRemote`'s equivalent command-building routine, `buildCommandOpts`, sets up the fetch config (remote URL, refspecs, auth headers, DNS-pinning via `resolvedAddress`) but never disables `transfer.bundleURI` [2](#0-1) .

### Finding Description
This is directly analogous to the reported bug class: a validation/hardening rule was added to one function (`CreateRepositoryFromURL`, the "buy()" analog) but not propagated to a sibling function performing essentially the same class of operation (`FetchRemote`, the "borrow()" analog), even though both are reachable with attacker-controlled remote URLs from an ordinary Gitaly client (e.g., via GitLab's mirror-fetch flow).

Git's bundle-URI feature (`transfer.bundleURI`) allows a remote server being cloned/fetched from to advertise an arbitrary secondary URL that Git will then fetch bundle data from as part of the operation. Because that secondary URL is entirely controlled by whatever server the RPC is told to talk to, it can be pointed at internal/loopback/metadata addresses that the resolved-address DNS-pinning protections applied to the *primary* remote URL do not cover — enabling SSRF against internal services from a machine running Gitaly.

The changelog entry confirms this was recognized and fixed specifically for `CreateRepositoryFromURL`: "repoFromURL: Disable bundle URIs when cloning from URL to prevent SSRF" [3](#0-2) . `FetchRemote` performs the conceptually identical action (contacting a remote at a caller-supplied URL and pulling objects from it) via `quarantineRepo.FetchRemote` with the same kind of `RemoteParams.Url` / `ResolvedAddress` input [4](#0-3) , but the fix was not applied there — it only appended `WithConfigEnv(config...)` to the fetch options without any `transfer.bundleURI=false` global option [5](#0-4) .

### Impact Explanation
If the remote endpoint targeted by `FetchRemote` (attacker-controlled, e.g., a malicious mirror source configured by a project owner) advertises a bundle URI pointing at an internal address (e.g., cloud metadata endpoint, internal admin API, or another Gitaly/Praefect node), Git will follow that URI to fetch bundle data as part of servicing the RPC. This can be used for SSRF: reaching internal-only HTTP endpoints from the Gitaly host, potentially leaking metadata/credentials or triggering unintended internal requests. This mirrors the severity class that motivated fixing `CreateRepositoryFromURL`.

### Likelihood Explanation
`FetchRemote` is a standard mutator RPC callable by any authenticated Gitaly client with permission to configure a mirror or perform a fetch against an arbitrary URL (as used by GitLab's repository mirroring feature), the same threat model that motivated hardening `CreateRepositoryFromURL`. No privileged internal access or malicious peer beyond controlling the target URL/remote server is required — this is exactly the class of "ordinary user-supplied RPC field" scenario the validation rules call for.

### Recommendation
Apply the same hardening used in `CreateRepositoryFromURL` to `FetchRemote`: append `gitcmd.WithGlobalOption(gitcmd.ConfigPair{Key: "transfer.bundleURI", Value: "false"})` (or equivalent) to the command options built in `buildCommandOpts` / passed into `quarantineRepo.FetchRemote` in `internal/gitaly/service/repository/fetch_remote.go`, so bundle-URI following cannot be triggered by a remote server during a fetch.

### Proof of Concept
Not independently executed against a live Gitaly instance; based on static code comparison. Conceptually: 1) Stand up an attacker-controlled Git HTTP remote whose smart-HTTP `info/refs` advertises a `bundle-uri` capability pointing at an internal address (e.g., `http://169.254.169.254/...`). 2) Call `FetchRemote` with `RemoteParams.Url` pointing at this attacker server. 3) Observe that, unlike `CreateRepositoryFromURL`, Git follows the bundle URI because `transfer.bundleURI` was never disabled for the fetch command, causing the Gitaly host to issue an outbound request to the internal address.

Note: I could not execute this PoC or find any Gitaly-side test asserting `FetchRemote` disables bundle URIs; this analysis is based on comparing the two RPCs' command-construction code and the changelog's stated rationale for fixing only `CreateRepositoryFromURL`.

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L72-76)
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

**File:** CHANGELOG.md (L96-98)
```markdown
### Security (1 change)

- [repoFromURL: Disable bundle URIs when cloning from URL to prevent SSRF](gitlab-org/gitaly@c5ce7f5a396b2e8a942be5bc591dafa20b4395f4)
```
