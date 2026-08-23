### Title
Unvalidated arbitrary remote-URL / transport-scheme acceptance across Gitaly remote RPCs enables SSRF and local file/protocol abuse - (File: `internal/gitaly/service/remote/find_remote_repository.go`, `internal/gitaly/service/repository/fetch_remote.go`, `internal/gitaly/service/repository/create_repository_from_url.go`)

### Summary
Several Gitaly RPCs (`FetchRemote`, `CreateRepositoryFromURL`, `FindRemoteRepository`, `FindRemoteRootRef`, `UpdateRemoteMirror`) accept a caller-supplied remote URL string and pass it essentially unvalidated into `git-fetch(1)`, `git-clone(1)`, `git-ls-remote(1)`, or `git-remote(1)`. Only the URL scheme's *hostname*, for DNS-rebinding mitigation, is optionally rewritten when a `resolved_address` is supplied — there is no allow-listing of transport schemes (e.g. `file://`, `ext::`, `fd::`) and no restriction preventing the URL from pointing at internal/loopback/metadata addresses when `resolved_address` is not provided. This mirrors the "arbitrary token addresses" bug class: user-controlled, unvalidated identifiers (URLs instead of contract addresses) are handed to a powerful downstream executor (`git`) without a whitelist.

### Finding Description
`FindRemoteRepository` takes `req.GetRemote()` directly from the request and passes it as a positional argument to `git ls-remote` with zero scheme/format validation: [1](#0-0) 

`FetchRemote`'s validation only checks the URL is non-empty; it does not validate scheme: [2](#0-1) 

The URL is fed straight into `git-fetch(1)` via `remote.inmemory.url` git config, with only optional DNS-rebinding hardening applied if a `resolved_address` is explicitly supplied by the (already-arbitrary) caller: [3](#0-2) 

`CreateRepositoryFromURL` similarly parses the URL for embedded credentials but never validates or restricts the scheme before invoking `git clone` with it, only explicitly disabling `transfer.bundleURI` because that specific SSRF-like vector was previously identified and patched: [4](#0-3) 

The DNS-rebinding helper `GetURLAndResolveConfig` only supports rewriting `http`, `https`, `git`, and `ssh` URLs to a pinned IP — it performs no scheme allow-listing and is opt-in (invoked only when the caller supplies `resolved_address`): [5](#0-4) 

Because none of these code paths restrict the git transport scheme (there is no `GIT_ALLOW_PROTOCOL` allow-list or equivalent set anywhere in the codebase, as confirmed by search), a caller able to invoke these RPCs (e.g., through project-import/mirror features that plumb a user-supplied URL down to Gitaly) can supply:
- `file:///path/on/gitaly/host` to have Gitaly clone/fetch from an arbitrary local path on the Gitaly server, disclosing local repository data.
- `http://169.254.169.254/...`, `http://127.0.0.1:<internal-port>/...`, or other internal-only endpoints to conduct SSRF from the Gitaly host's network context (partially mitigated only when the calling layer chooses to supply a `resolved_address`, which is not enforced here).
- Any other transport git supports (subject to git's own build-time protocol allow-list, which Gitaly does nothing to further restrict at the RPC layer).

### Impact Explanation
An attacker capable of driving these RPCs with a crafted URL (via GitLab features like repository mirroring or project import from URL, which route through `CreateRepositoryFromURL`/`FetchRemote`/`UpdateRemoteMirror`) can use the Gitaly server as an SSRF pivot against internal services, or read local filesystem content on the Gitaly host reachable via `file://`. This is a server-side request forgery / local file disclosure risk, not merely a theoretical design choice — unlike the "arbitrary token address" case in the original report where the vendor deemed it intended, here the analogous unvalidated identifier is passed to a network/file I/O subsystem rather than an isolated smart-contract call, so a "user picks the target" design has real network/file-system blast radius on the Gitaly host.

### Likelihood Explanation
Likelihood is moderate: these RPCs are reachable through ordinary product workflows (repository mirroring, "import project by URL") that ordinary, non-admin users can typically trigger in GitLab-like deployments, and the request protobufs (`gitalypb.Remote`, `CreateRepositoryFromURLRequest`, `FindRemoteRepositoryRequest`) expose the URL field directly to the caller with no scheme filtering enforced at the Gitaly boundary. The main gating factor is whether the calling layer (e.g., GitLab Rails) applies its own URL/SSRF validation before calling into Gitaly — Gitaly itself provides no defense-in-depth check.

### Recommendation
Add explicit scheme allow-listing at the Gitaly RPC boundary (e.g., only permit `http`, `https`, `git`, `ssh` and reject `file`, `ext`, `fd`, and other local/helper transports) in `validateFetchRemoteRequest`, `validateCreateRepositoryFromURLRequest`, `FindRemoteRepository`, and `UpdateRemoteMirror`/`FindRemoteRootRef`. Additionally, consider making resolved-address/DNS-pinning mandatory (or providing an option to reject private/loopback/link-local resolved IPs) rather than optional, so SSRF protections cannot be bypassed simply by omitting `resolved_address`.

### Proof of Concept
1. Call `CreateRepositoryFromURL` (or `FetchRemote`) with `Url: "file:///var/opt/gitlab/git-data/repositories/some/other-project.git"` (a path on the Gitaly host outside the caller's own storage/repo) — `cloneFromURLCommand` will happily invoke `git clone file://... targetPath`, copying data the caller should not have access to.
2. Call `FetchRemote`/`UpdateRemoteMirror` with `Url: "http://127.0.0.1:9100/internal-admin-endpoint"` (an internal-only service on the Gitaly host's network) without supplying `resolved_address` — the request is passed straight to `git fetch` as the remote URL, causing the Gitaly host to issue the request itself (SSRF), since no scheme/host validation exists in `buildCommandOpts` or `validateFetchRemoteRequest`. [3](#0-2) [6](#0-5)

### Citations

**File:** internal/gitaly/service/remote/find_remote_repository.go (L13-28)
```go
func (s *server) FindRemoteRepository(ctx context.Context, req *gitalypb.FindRemoteRepositoryRequest) (*gitalypb.FindRemoteRepositoryResponse, error) {
	if req.GetRemote() == "" {
		return nil, structerr.NewInvalidArgument("empty remote can't be checked.")
	}

	var output bytes.Buffer
	cmd, err := s.gitCmdFactory.NewWithoutRepo(ctx,
		gitcmd.Command{
			Name: "ls-remote",
			Args: []string{
				req.GetRemote(),
				"HEAD",
			},
		},
		gitcmd.WithStdout(&output),
	)
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

**File:** internal/gitaly/service/repository/fetch_remote.go (L306-320)
```go
func (s *server) validateFetchRemoteRequest(ctx context.Context, req *gitalypb.FetchRemoteRequest) error {
	if err := s.locator.ValidateRepository(ctx, req.GetRepository()); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	if req.GetRemoteParams() == nil {
		return structerr.NewInvalidArgument("missing remote params")
	}

	if req.GetRemoteParams().GetUrl() == "" {
		return structerr.NewInvalidArgument("blank or empty remote URL")
	}

	return nil
}
```

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L36-85)
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
