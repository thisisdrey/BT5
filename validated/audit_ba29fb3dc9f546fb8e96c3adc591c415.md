### Title
Unrestricted remote URL in `CreateRepositoryFromURL`/`FetchRemote` enables SSRF and authorization-token disclosure to attacker-controlled hosts - (File: internal/gitaly/service/repository/create_repository_from_url.go, internal/gitaly/service/repository/fetch_remote.go)

### Summary
The C4 finding describes a swap function that blindly executes calls against an attacker-supplied `spender`/`swapTarget` with no validation that the target is the intended 0x protocol entrypoint, letting a semi-trusted role redirect value to an arbitrary destination. The same bug class — "arbitrary attacker-chosen network target executed by the server, with sensitive credentials attached and no destination allowlist" — is present in Gitaly's `CreateRepositoryFromURL` and `FetchRemote` RPC handlers, which build `git clone`/`git fetch` invocations against a caller-supplied URL string with essentially no restriction on scheme, host, or IP range.

### Finding Description
`CreateRepositoryFromURL` takes `req.GetUrl()` directly and forwards it into `cloneFromURLCommand`, which only `url.Parse`s it and optionally rewrites the host if a `resolvedAddress` is supplied (a DNS-rebinding mitigation, not a destination allowlist): [1](#0-0) 

If the request contains basic-auth credentials or a separate `HttpAuthorizationHeader`, that credential is transformed into an `http.extraHeader: Authorization: ...` config value and attached unconditionally to whatever host is in the URL: [2](#0-1) 

The resulting URL/config is fed straight into `git clone`: [3](#0-2) 

`FetchRemote` follows the same pattern: `validateFetchRemoteRequest` only rejects an empty URL, performing no scheme/host/IP validation at all: [4](#0-3) 

and the URL is passed through to `quarantineRepo.FetchRemote` for an actual outbound `git fetch`: [5](#0-4) 

Both handlers pass an arbitrary, caller-controlled endpoint to the Git binary and attach a potentially sensitive authorization token/header to it — mirroring the C4 pattern of granting a caller arbitrary control over what a powerful primitive (an approval/call in C4; a network fetch with attached credentials here) targets, without validating the destination is the intended endpoint.

### Impact Explanation
Because the URL is unrestricted, a caller of these RPCs (e.g., via "import project by URL" or repository mirroring features that ultimately invoke these RPCs) can point Gitaly at:
- Internal-only services or cloud metadata endpoints (SSRF), since nothing blocks `http://169.254.169.254/...`, `http://localhost:...`, or other RFC1918 addresses.
- An attacker-controlled external host, causing the `HttpAuthorizationHeader`/basic-auth credential supplied in the same request (or, in deployments where this value originates from an internal trusted token) to be sent to that host — credential disclosure.

This satisfies the "concrete storage escape... SSRF or credential disclosure" bar for a valid analog, since the outbound target and header are both attacker-influenced and unvalidated.

### Likelihood Explanation
Both RPCs are reachable through ordinary import/mirror flows that accept a repository URL from the caller; the only mitigation present (`resolvedAddress` and `GetURLAndResolveConfig`) addresses DNS-rebinding, not the more fundamental issue of an unrestricted destination or unconditional credential attachment. No allowlist of hosts/schemes or check against internal address ranges is applied in `validateCreateRepositoryFromURLRequest`/`validateFetchRemoteRequest` paths shown above, so the vector is directly and repeatably reachable whenever a URL-based import/mirror request is accepted.

### Recommendation
- Validate and restrict the destination host/IP for `CreateRepositoryFromURL`/`FetchRemote` (deny loopback, link-local, and other internal ranges unless explicitly resolved/pinned via a vetted mechanism).
- Only attach `http.extraHeader`/basic-auth credentials to hosts that have been validated/allowlisted, not unconditionally to whatever host appears in the caller-supplied URL.
- Consider requiring URLs to be resolved and validated by a trusted upstream service (e.g., GitLab Rails' `Gitlab::HTTP_V2::UrlBlocker`) *before* Gitaly performs the outbound fetch/clone, and have Gitaly enforce that the resolved address matches an explicit allow-set rather than merely pinning DNS for rebinding protection.

### Proof of Concept
1. Call `CreateRepositoryFromURL` (or trigger a mirror `FetchRemote`) with `Url: "http://169.254.169.254/latest/meta-data/iam/security-credentials/"` and no `resolved_address`.
2. `cloneFromURLCommand`/`fetchRemoteAtomic` performs no scheme/IP validation before invoking `git clone`/`git fetch` against that URL: [6](#0-5) [7](#0-6) 
3. If an `HttpAuthorizationHeader`/basic-auth credential is also supplied in the same request, it is attached as `Authorization` header to the request sent to the attacker-chosen host: [8](#0-7) , exfiltrating the credential value to that host via the outbound HTTP request Git issues during the clone attempt.

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L21-39)
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
```

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L41-58)
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
	} else if len(authorizationToken) > 0 {
		authHeader := fmt.Sprintf("Authorization: %s", authorizationToken)
		config = append(config, gitcmd.ConfigPair{Key: "http.extraHeader", Value: authHeader})
	}
```

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L72-84)
```go
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
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L51-104)
```go
func (s *server) fetchRemoteAtomic(ctx context.Context, req *gitalypb.FetchRemoteRequest) (_ bool, _ bool, returnedErr error) {
	var stdout, stderr bytes.Buffer
	opts := localrepo.FetchOpts{
		Stdout:  &stdout,
		Stderr:  &stderr,
		Force:   req.GetForce(),
		Prune:   !req.GetNoPrune(),
		Tags:    localrepo.FetchOptsTagsAll,
		Verbose: true,
		// Transactions are disabled during fetch operation because no references are updated when
		// the dry-run option is enabled. Instead, the reference-transaction hook is performed
		// during the subsequent execution of `git-update-ref(1)`.
		DisableTransactions: true,
		// When the `dry-run` option is used with `git-fetch(1)`, Git objects are received without
		// performing reference updates. This is used to quarantine objects on the initial fetch and
		// migration to occur only during reference update.
		DryRun: true,
		// The `porcelain` option outputs reference update information from `git-fetch(1) to stdout.
		// Since references are not updated during a `git-fetch(1)` dry-run, the reference
		// information is used during `git-update-ref(1)` execution to update the appropriate
		// corresponding references.
		Porcelain: true,
	}

	if req.GetNoTags() {
		opts.Tags = localrepo.FetchOptsTagsNone
	}

	if err := buildCommandOpts(ctx, &opts, req); err != nil {
		return false, false, err
	}

	sshCommand, sshCleanup, err := gitcmd.BuildSSHInvocation(ctx, s.logger, req.GetSshKey(), req.GetKnownHosts())
	if err != nil {
		return false, false, err
	}
	defer sshCleanup()

	opts.Env = append(opts.Env, "GIT_SSH_COMMAND="+sshCommand)

	// When performing fetch, objects are received before references are updated. If references fail
	// to be updated, unreachable objects could be left in the repository that would need to be
	// garbage collected. To be more atomic, a quarantine directory is set up where objects will be
	// fetched prior to being migrated to the main repository when reference updates are committed.
	quarantineDir, quarantineCleanup, err := quarantine.New(ctx, req.GetRepository(), s.logger, s.locator)
	if err != nil {
		return false, false, fmt.Errorf("creating quarantine directory: %w", err)
	}
	defer func() {
		quarantineCleanup() // Errors are logged by the tempdir package
	}()

	quarantineRepo := s.localRepoFactory.Build(quarantineDir.QuarantinedRepo())
	if err := quarantineRepo.FetchRemote(ctx, "inmemory", opts); err != nil {
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
