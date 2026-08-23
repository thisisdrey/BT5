### Title
Missing transport-scheme allowlisting allows `ext::`/`fd::` git transport helper injection via user-controlled remote URLs - (File: `internal/git/gitcmd/command_factory.go`)

### Summary
`combineArgs` (and the whole `gitcmd` command construction pipeline it participates in) never inspects or restricts the *content* of URL/positional operands — it only combines global config and validated flags/args coming from `Command.CommandArgs()`. Nowhere in `gitcmd`, `create_repository_from_url.go`, `fetch_remote.go`, or `find_remote_root_ref` is a Git transport-scheme allowlist (e.g. `protocol.ext.allow`/`GIT_ALLOW_PROTOCOL`) enforced, so a URL value that begins with `ext::` or `fd::` is passed straight through to `git clone`/`git fetch` as a plain operand and is interpreted by Git itself as a transport helper invocation.

### Finding Description
`combineArgs` at `internal/git/gitcmd/command_factory.go:597-645` builds the final argv by concatenating global config args (`GlobalArgs()`), and the subcommand's own args produced by `sc.CommandArgs()`. It performs no scheme/content validation of any string value [1](#0-0) .

The environment set up for every git invocation in `setupGitExecutionEnvironments` only forces locale, disables terminal prompts, and points config files to `/dev/null` — it never sets `GIT_ALLOW_PROTOCOL` or restricts allowed transports [2](#0-1) .

Concrete callers place attacker-controlled URLs directly as positional `Args` to `git clone`:
`cloneFromURLCommand` in `create_repository_from_url.go` parses the URL only to strip credentials and optionally rewrite the host for DNS-rebinding protection, but never validates the scheme against an allowlist (http/https/git/ssh); the resulting `urlString` is placed unmodified into `Args: []string{urlString, repositoryFullPath}` for the `clone` command [3](#0-2) . The only request-level validation is that the URL is non-empty: `validateCreateRepositoryFromURLRequest` checks `req.GetUrl() == ""` and does nothing else [4](#0-3) .

Similarly, `GetURLAndResolveConfig` in `command_resolve.go`, used by `FindRemoteRootRef`/`FetchRemote` DNS-rebinding protection, dispatches purely based on string prefix (`http://`, `https://`, `git://`, `ssh://`) and falls back to treating anything else as an SCP-like address — an `ext::` or `fd::` URL would simply fall into the SCP branch and be passed through un-rejected [5](#0-4) .

A search across the codebase for any handling of `ext::`, `fd::`, transport/remote-helper restriction, or `GIT_ALLOW_PROTOCOL`/`protocol.allow` config found no matches in `gitcmd` or the repository/remote service packages, confirming that no allowlist exists at the point where `combineArgs` assembles the final command line.

Because Git's own URL parser treats any string beginning with `ext::<command>` as an instruction to execute `<command>` as a remote-helper transport (and `fd::<fd-numbers>` to use raw file descriptors), passing such a value as an operand does not need a leading `-`, `--upload-pack=`, or `-c` — the transport-helper interpretation happens purely due to the string's own syntax, which `combineArgs`/`CommandArgs` do not special-case or reject.

### Impact Explanation
If an unprivileged user can control the `Url` field of `CreateRepositoryFromURLRequest`, `Remote.Url` in `FetchRemoteRequest`, or `RemoteUrl` in `FindRemoteRootRefRequest` (e.g. via a "fork/import from URL" or "add mirror" action that flows through to these RPCs without a Rails-side scheme allowlist, or by directly invoking the RPC), supplying `ext::sh -c "id > /tmp/pwn"` (or `ext::sh -c 'attacker-command'`) as the URL causes Gitaly's spawned `git clone`/`git fetch` process to execute the attacker's shell command with Gitaly's own privileges. This is arbitrary command execution on the Gitaly host, matching the GitLab HackerOne "Git argument/config injection or command execution" impact class.

### Likelihood Explanation
The attacker capability required is exactly what the question specifies: an unprivileged user who can trigger an import-from-URL, mirror, or fetch-remote flow, supplying an arbitrary URL string. No admin/operator role, no compromised secrets, and no non-default configuration are needed — the missing check is a structural gap in `gitcmd`/service code, not a misconfiguration. The primary uncertainty is whether GitLab Rails performs its own scheme allowlisting (via `Gitlab::UrlBlocker`) before these RPC fields ever reach Gitaly; if such Rails-side filtering is bypassed or absent (e.g., a caller that talks to Gitaly directly, or a Rails code path that forwards raw user input), the request reaches Gitaly unfiltered and Gitaly itself provides no defense-in-depth.

### Recommendation
Add explicit transport-scheme validation before any URL-derived string is placed into `Command.Args`/`PostSepArgs`: reject URLs whose scheme is not in an allowlist (`http`, `https`, `git`, `ssh`, and SCP-like syntax), explicitly refusing `ext::` and `fd::` (and any other unrecognized scheme). Additionally, set `GIT_ALLOW_PROTOCOL=http:https:git:ssh` (or the Git config equivalent `protocol.allow`) in the shared execution environment configured in `setupGitExecutionEnvironments` (`internal/git/gitcmd/command_factory.go`) as defense-in-depth so that Git itself refuses to invoke `ext`/`fd`/`file` helpers regardless of what value flows through `combineArgs`.

### Proof of Concept
```go
func TestCreateRepositoryFromURL_extTransportRejected(t *testing.T) {
	t.Parallel()
	ctx := testhelper.Context(t)
	cfg, client := setupRepositoryService(t)

	importedRepo := &gitalypb.Repository{
		RelativePath: "imports/ext-poc.git",
		StorageName:  cfg.Storages[0].Name,
	}

	_, err := client.CreateRepositoryFromURL(ctx, &gitalypb.CreateRepositoryFromURLRequest{
		Repository: importedRepo,
		Url:        `ext::sh -c "touch${IFS}/tmp/gitaly-poc"`,
	})

	// Expected (fixed) behavior: InvalidArgument rejecting the ext:: transport.
	// Actual (vulnerable) behavior: request proceeds to `git clone`, spawning
	// `sh -c "touch /tmp/gitaly-poc"` on the Gitaly host, and /tmp/gitaly-poc exists.
	require.Error(t, err)
}
```
Expected assertion on a patched Gitaly: `err` is a `structerr.InvalidArgument` referencing a disallowed transport scheme, and no `/tmp/gitaly-poc` file is created. On the current code, `validateCreateRepositoryFromURLRequest` only checks for an empty URL, so the request proceeds to `cloneFromURLCommand`, and the crafted `ext::` URL is executed by Git.

### Citations

**File:** internal/git/gitcmd/command_factory.go (L221-236)
```go
func setupGitExecutionEnvironments(cfg config.Cfg, factoryCfg execCommandFactoryConfig, logger log.Logger) ([]ExecutionEnvironment, func(), error) {
	sharedEnvironment := []string{
		// Force English locale for consistency on output messages and to help us debug in
		// case we get bug reports from customers whose system-locale would be different.
		"LANG=en_US.UTF-8",
		// Ask Git to never prompt us for any information like e.g. credentials.
		"GIT_TERMINAL_PROMPT=0",
		// Prevent the environment from affecting git calls by ignoring the configuration files.
		// This should be done always but we have to wait until 15.0 due to backwards compatibility
		// concerns.
		//
		// See https://gitlab.com/gitlab-org/gitaly/-/issues/3617.
		"GIT_CONFIG_GLOBAL=/dev/null",
		"GIT_CONFIG_SYSTEM=/dev/null",
		"XDG_CONFIG_HOME=/dev/null",
	}
```

**File:** internal/git/gitcmd/command_factory.go (L639-644)
```go
	scArgs, err := sc.CommandArgs()
	if err != nil {
		return nil, err
	}

	return append(args, scArgs...), nil
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

**File:** internal/git/gitcmd/command_resolve.go (L41-48)
```go
	switch {
	case strings.HasPrefix(remoteURL, "http://"), strings.HasPrefix(remoteURL, "https://"), strings.HasPrefix(remoteURL, "git://"):
		return getURLAndResolveConfigForURL(remoteURL, resolvedAddress)
	case strings.HasPrefix(remoteURL, "ssh://"):
		return getURLAndResolveConfigForSSH(remoteURL, resolvedAddress)
	default:
		return getURLAndResolveConfigForSCP(remoteURL, resolvedAddress)
	}
```
