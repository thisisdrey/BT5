### Title
Missing transport-scheme allow-listing lets remote URLs invoke arbitrary Git transport helpers (`ext::`, `fd::`, etc.) via `git fetch`/`clone`/`remote` - ([File: internal/git/gitcmd/command_description.go])

### Summary
`commandDescriptions["fetch"]`, `["clone"]`, `["remote"]`, `["push"]`, and `["ls-remote"]` only set `http.followRedirects=false` and related HTTP-specific config to mitigate DNS-rebinding/redirect abuse, but there is no validation anywhere in the RPC handlers that consume attacker-controlled remote URLs (`FetchRemote`, `CreateRepositoryFromURL`, `UpdateRemoteMirror`, `FindRemoteRootRef`) restricting the URL scheme. Since the raw string is passed straight through to `git fetch`/`git clone`/`git remote add` as an operand, Git's own transport-helper dispatch (`ext::`, `fd::`, `file://`) is fully available, allowing command execution or arbitrary local file access on the Gitaly host.

### Finding Description
`buildCommandOpts` in `internal/gitaly/service/repository/fetch_remote.go` takes `req.GetRemoteParams().GetUrl()` and writes it into `remote.inmemory.url` config without any scheme check [1](#0-0) . The only pre-flight validation, `validateFetchRemoteRequest`, checks only that the repository is valid and the URL is non-empty — no scheme allow-listing [2](#0-1) . The same pattern exists in `cloneFromURLCommand` (`internal/gitaly/service/repository/create_repository_from_url.go`), which parses the URL only to strip credentials, never validates the scheme, and passes it directly as an argument to `git clone` [3](#0-2) , and in `updateRemoteMirror` / `findRemoteRootRefCmd`, which feed `remote.GetUrl()` into `remote.<name>.url` git config with no scheme check [4](#0-3) [5](#0-4) .

The command-description allow-list in `internal/git/gitcmd/command_description.go`, which is Gitaly's designated mechanism for "curating" what config/flags are injected around each git subcommand, only adds `http.followRedirects=false` for `fetch`, `clone`, `push`, `remote`, and `ls-remote` [6](#0-5) [7](#0-6) . This config key only affects HTTP(S) redirect-following behavior and has no effect on non-HTTP Git transport helpers. There is no `protocol.allow`, `protocol.ext.allow=never`, or `GIT_ALLOW_PROTOCOL` restriction anywhere in the codebase (confirmed via repo-wide search — the only matches for these terms are in test fixtures), meaning Git's default transport-helper dispatch is fully active. `GetURLAndResolveConfig` in `internal/git/gitcmd/command_resolve.go`, which is invoked for DNS-rebinding protection when `resolved_address` is set, does perform scheme dispatch but only recognizes `http(s)://`, `git://`, `ssh://`, and falls back to SCP-style parsing for everything else — it does not reject or block other schemes such as `ext::`, `fd::`, or `file://`; it simply skips generating IP-pinning config for those and returns the raw URL unmodified [8](#0-7)  — and this path is optional (only triggered if the caller sets `ResolvedAddress`), so it does not act as a scheme allow-list gate at all.

`git fetch ext::sh -c '...'` or a `git remote add`/`git clone` with an `ext::` URL invokes the `git-remote-ext` transport helper, which spawns an arbitrary shell command specified in the URL, directly on the Gitaly host with the Gitaly process's privileges (git checkouts happen server-side, unlike client-configured `protocol.ext.allow=user` restrictions that apply to submodule recursion, not to a URL supplied directly on the command line).

### Impact Explanation
This is remote command execution on the Gitaly host under the Gitaly process's file-system and network privileges, since `ext::` allows spawning an arbitrary subprocess (e.g., `ext::sh -c "id > /tmp/pwned"`), and `file://` allows disclosure of arbitrary local files reachable to the Gitaly service account by making Git treat them as a bare repository transport source. This matches GitLab's "Remote Code Execution" / "SSRF leading to sensitive file disclosure" bounty impact classes, and is a direct violation of the stated invariant that "Git is never re-configured/re-purposed by attacker-controlled input."

### Likelihood Explanation
Any unprivileged authenticated GitLab user who can import a project from a URL (`CreateRepositoryFromURL`), configure a pull mirror (`UpdateRemoteMirror`), or trigger `FetchRemote` (used by project import/mirroring features) controls the `RemoteUrl`/`Remote.Url` field end-to-end. No admin role, secret, or peer compromise is required — this is directly reachable from the standard "import repository by URL" and "configure remote mirror" GitLab features that pass user input straight to these RPCs. The only caveat is that GitLab Rails may perform its own scheme validation before calling Gitaly in some of these flows (e.g., project import URL validators) — that would need confirmation on the Rails side, but Gitaly itself provides zero defense-in-depth here, so any caller (Rails, other future integrators, or Gitaly's own gRPC surface if exposed with a permissive `CreateRepositoryFromURLRequest`) can trigger it.

### Recommendation
Add a scheme allow-list check in Gitaly itself, independent of any caller-side validation. Concretely: (1) parse the remote URL in `validateFetchRemoteRequest`, `validateUpdateRemoteMirrorRequest`, `cloneFromURLCommand`, and `findRemoteRootRefCmd`, rejecting any URL whose scheme is not in `{http, https, ssh, git}` or that doesn't match a safe SCP-like pattern; (2) additionally set `protocol.ext.allow=never` and `protocol.file.allow=never` (and similarly restrict `fd::`) as global config in the relevant `commandDescriptions` entries (`fetch`, `clone`, `push`, `remote`, `ls-remote`) in `internal/git/gitcmd/command_description.go` as defense-in-depth, since this is the canonical place Gitaly curates git's config/flags for these subcommands.

### Proof of Concept
```go
func TestFetchRemote_ExtTransportRCE(t *testing.T) {
    ctx := testhelper.Context(t)
    cfg, repoProto, _ := testcfg.BuildWithRepo(t)
    client, _ := setupRepositoryServiceWithoutRepo(t, cfg) // matches existing test helpers in fetch_remote_test.go

    marker := testhelper.TempDir(t) + "/pwned"
    req := &gitalypb.FetchRemoteRequest{
        Repository: repoProto,
        RemoteParams: &gitalypb.Remote{
            Url: fmt.Sprintf("ext::sh -c 'touch %s >&2; exit 1'", marker),
        },
    }

    _, err := client.FetchRemote(ctx, req)
    // Regardless of the fetch failing, assert the side-effect file was created,
    // proving arbitrary command execution via the "ext::" transport.
    require.FileExists(t, marker)
}
```
Expected outcome on an unpatched Gitaly: the `sh -c` command executes on the Gitaly host and creates `marker`, proving RCE via the unvalidated `RemoteUrl` field, because `buildCommandOpts`/`validateFetchRemoteRequest` (`internal/gitaly/service/repository/fetch_remote.go:260-320`) perform no scheme check and `commandDescriptions["fetch"]` (`internal/git/gitcmd/command_description.go:99-139`) does not restrict `protocol.ext.allow`.

### Citations

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

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L36-84)
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

**File:** internal/gitaly/service/remote/find_remote_root_ref.go (L17-31)
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
```

**File:** internal/git/gitcmd/command_description.go (L99-139)
```go
	"fetch": {
		flags: 0,

		opts: func(ctx context.Context) []GlobalOption {
			return append(append([]GlobalOption{
				// We've observed performance issues when fetching into big repositories
				// part of an object pool. The root cause of this seems to be the
				// connectivity check, which by default will also include references of any
				// alternates. Given that object pools often have hundreds of thousands of
				// references, this is quite expensive to compute. Below config entry will
				// disable listing of alternate refs: they shouldn't even be included in the
				// negotiation phase, so they aren't going to matter in the connectivity
				// check either.
				ConfigPair{Key: "core.alternateRefsCommand", Value: "exit 0 #"},

				// While git-fetch(1) by default won't write commit graphs, both CNG and
				// Omnibus set this value to true. This has caused performance issues when
				// doing internal fetches, and furthermore it's not encouraged to run such
				// maintenance tasks on "normal" Git operations. Instead, writing commit
				// graphs should be done in our housekeeping RPCs, which already know to do
				// so. So let's disable writing commit graphs on fetches -- if it really is
				// required, we can enable it on a case-by-case basis.
				ConfigPair{Key: "fetch.writeCommitGraph", Value: "false"},

				// By default, Git follows HTTP redirects. Because it's easy for a malicious
				// user to set up a DNS redirect that points to a server that's internal for
				// us and unreachable from the outside, this is dangerous. We thus have to
				// disable redirects in all cases.
				ConfigPair{Key: "http.followRedirects", Value: "false"},

				// By default, Git will try to recurse into submodules on demand: if a fetch
				// retrieves a commit that updates a populated submodule, then it recurses
				// into that submodule and also updates it. Computing this condition takes
				// some resources though given that we need to check all fetched commits to
				// find out if any submodule was in fact updated. This is a complete waste
				// of time though because we never populate submodules at all. We thus
				// disable recursion into submodules.
				ConfigPair{Key: "fetch.recurseSubmodules", Value: "no"},
			}, fetchFsckConfiguration(ctx)...), packConfiguration(ctx)...)
		},
	},
```

**File:** internal/git/gitcmd/command_description.go (L292-302)
```go
	"remote": {
		// While git-remote(1)'s `add` subcommand does support `--end-of-options`,
		// `remove` doesn't.
		flags: scNoEndOfOptions,
		opts: func(context.Context) []GlobalOption {
			return []GlobalOption{
				// See "fetch" for why we disable following redirects.
				ConfigPair{Key: "http.followRedirects", Value: "false"},
			}
		},
	},
```

**File:** internal/git/gitcmd/command_resolve.go (L27-48)
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
```
