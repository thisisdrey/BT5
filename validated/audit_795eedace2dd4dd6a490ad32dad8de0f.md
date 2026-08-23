## Analysis

The reported bug class is: an internal service passes a client-supplied URL directly into `git fetch`/`git clone` without restricting the URL scheme, allowing `file://` URLs to be used to import arbitrary local git repositories (an SSRF into the local filesystem).

Gitaly's `RepositoryService.CreateRepositoryFromURL` and `RepositoryService.FetchRemote` RPCs have the exact same root cause: **no scheme allow-listing is performed anywhere in Gitaly before the URL is handed to `git clone`/`git fetch`.**

- `validateCreateRepositoryFromURLRequest` only checks that the URL string is non-empty: [1](#0-0) 
- The URL is then parsed with `net/url` and passed straight to `git clone` as an argument, with no scheme check performed at all: [2](#0-1) [3](#0-2) 
- Similarly `validateFetchRemoteRequest` for `FetchRemote` only checks the URL is not blank: [4](#0-3) 
- `buildCommandOpts` writes the raw URL into `remote.inmemory.url` git config and executes `git fetch`, again without scheme restriction: [5](#0-4) 

The only scheme-aware code path (`GetURLAndResolveConfig`, used for DNS-rebinding protection when `resolved_address` is set) enumerates `http/https/git/ssh` and falls back to SCP-like syntax for anything else — it does not reject other schemes, and it's only invoked when `resolved_address` is non-empty (optional field), so it provides no protection for `file://` URLs: [6](#0-5) .

This means a caller of `CreateRepositoryFromURL` or `FetchRemote` with `url: "file:///var/opt/gitlab/git-data/repositories/@hashed/.../xxx.git"` will cause Gitaly to run `git clone file:///...` / `git fetch file:///...`, reading any git repository the Gitaly process/OS user can access on disk — exactly the class of bug described in the HackerOne report, just manifesting at the Gitaly RPC layer instead of the Rails `BulkImports::Projects::Pipelines::RepositoryPipeline`.

Note: whether this is exploitable by an "ordinary user" in production also depends on whether GitLab Rails (the actual caller of these Gitaly RPCs) performs its own scheme validation (e.g. via `Gitlab::UrlBlocker`) before invoking `CreateRepositoryFromURL`/`FetchRemote`. I don't have visibility into the Rails-side callers in this repo, so I can't confirm whether Gitaly is the only line of defense in the current codebase — but as designed, Gitaly itself performs **no** independent scheme validation, so any caller (including Praefect proxying an ordinary user's import/mirror request) that forwards an unsanitized URL will trigger local file/repo disclosure.

### Title
Missing URL scheme validation in `CreateRepositoryFromURL`/`FetchRemote` allows local repository import via `file://` URLs - (File: internal/gitaly/service/repository/create_repository_from_url.go, internal/gitaly/service/repository/fetch_remote.go)

### Summary
Both `CreateRepositoryFromURL` and `FetchRemote` RPCs accept a client-controlled `url`/`remote_params.url` field and pass it unmodified to `git clone`/`git fetch` without restricting the accepted URL scheme. Because git supports the `file://` transport (and other transports like `ext::`), a caller that can reach these RPCs with an arbitrary URL can make the Gitaly server clone/fetch from any git repository reachable on the local filesystem, rather than only from a legitimate remote.

### Finding Description
`validateCreateRepositoryFromURLRequest` [1](#0-0)  and `validateFetchRemoteRequest` [4](#0-3)  both only verify the URL is non-empty. Neither function nor any downstream code (`cloneFromURLCommand` [2](#0-1) , `buildCommandOpts` [7](#0-6) ) rejects non-network schemes such as `file://` before invoking `git clone`/`git fetch`. This mirrors the root cause in the referenced HackerOne report, where `Gitlab::UrlBlocker.validate!` was called without an explicit `schemes:` allow-list, letting `file://` URLs through to `git fetch`.

### Impact Explanation
An attacker who can control the `url` supplied to `CreateRepositoryFromURL` or `FetchRemote` (e.g. via project import/mirror features that forward user input to these RPCs) can exfiltrate the contents of any git repository stored on disk that the Gitaly process can read — including private repositories belonging to other users/projects — by supplying a `file://` path such as `file:///var/opt/gitlab/git-data/repositories/@hashed/b1/74/<sha256>.git`, matching the storage layout described in the original report.

### Likelihood Explanation
Exploitability depends on whether an intermediate caller (e.g., GitLab Rails) already restricts the URL scheme before calling into Gitaly. Within Gitaly itself, there is no defense — any RPC caller with access to `RepositoryService.CreateRepositoryFromURL`/`FetchRemote` and control of the URL field can trigger this immediately.

### Recommendation
Add explicit scheme allow-listing (e.g., `http`, `https`, `git`, `ssh`) in `validateCreateRepositoryFromURLRequest` and `validateFetchRemoteRequest` (or a shared helper), rejecting `file://`, `ext::`, and any other non-network scheme before constructing the `git clone`/`git fetch` command, analogous to passing `schemes:` to `Gitlab::UrlBlocker.validate!` in the original fix.

### Proof of Concept
```
grpcurl -plaintext -d '{
  "repository": {"storage_name": "default", "relative_path": "imported.git"},
  "url": "file:///var/opt/gitlab/git-data/repositories/@hashed/b1/74/<sha256-of-target-project-id>.git"
}' <gitaly-address> gitaly.RepositoryService/CreateRepositoryFromURL
```
This causes Gitaly to execute `git clone --bare file:///var/opt/gitlab/git-data/repositories/@hashed/b1/74/<sha256>.git <target-path>`, copying the target repository's full history into the attacker-controlled destination repository.

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

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L60-84)
```go
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
```

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L149-158)
```go
func validateCreateRepositoryFromURLRequest(ctx context.Context, locator storage.Locator, req *gitalypb.CreateRepositoryFromURLRequest) error {
	if err := locator.ValidateRepository(ctx, req.GetRepository(), storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return err
	}

	if req.GetUrl() == "" {
		return fmt.Errorf("empty Url")
	}

	return nil
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
