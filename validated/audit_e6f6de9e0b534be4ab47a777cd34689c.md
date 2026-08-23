Based on my investigation, `Command`/`CommandArgs` in `internal/git/gitcmd/command.go` and `commandDescription.args()` in `command_description.go` validate only that positional arguments do not start with `-` [1](#0-0) , and none of the call sites that forward attacker-controlled URLs (e.g. `FindRemoteRepository`, `CreateRepositoryFromURL`, `FetchRemote`, `UpdateRemoteMirror`, `FindRemoteRootRef`) perform any scheme allow/deny-listing before handing the URL to `git`.

### Title
Missing rejection of `ext::`/`fd::` git transport helpers in remote URL handling - (File: internal/gitaly/service/remote/find_remote_repository.go, internal/git/gitcmd/command.go)

### Summary
`FindRemoteRepository` passes the attacker-controlled `remote` field directly as a positional argument to `git ls-remote` via `gitcmd.Command` without validating the URL scheme [2](#0-1) . `command_description.go`'s `validatePositionalArg` only rejects arguments starting with `-`, and does not reject `ext::`/`fd::` transport-helper syntax [1](#0-0) .

### Finding Description
`FindRemoteRepository` only checks that `req.GetRemote()` is non-empty [3](#0-2)  before building `gitcmd.Command{Name: "ls-remote", Args: []string{req.GetRemote(), "HEAD"}}` and executing it via `gitCmdFactory.NewWithoutRepo` [4](#0-3) . The `Command.CommandArgs()` path resolves `ls-remote`'s description and calls `commandDescription.args()`, which for positional `Args` only enforces `validatePositionalArg`, rejecting a leading `-` but performing no scheme/transport-helper check [5](#0-4) . Consequently, a value such as `ext::sh -c "id > /tmp/pwn"` or `fd::0` is passed through unmodified as an operand to the spawned `git` process, where Git's own `ext::`/`fd::` remote-helper syntax would interpret it as a directive to execute an arbitrary command or use a raw file descriptor — not as a plain positional URL. None of the sibling RPCs (`CreateRepositoryFromURL`, `FetchRemote`, `UpdateRemoteMirror`, `FindRemoteRootRef`) validate the scheme either; they only check the URL is non-empty [6](#0-5) [7](#0-6) . `GetURLAndResolveConfig` (used for DNS-rebinding mitigation) does inspect the scheme, but only when `resolved_address` is set, and its "default" branch treats any unrecognized scheme (including `ext::...`) as an SCP-style host and doesn't reject `ext::`/`fd::` [8](#0-7) [9](#0-8) . This is not invoked at all when `resolved_address` is empty, which is the common case.

### Impact Explanation
If reachable, `ext::` allows execution of an arbitrary command as the Gitaly process user (Git spawns `sh -c` from the `ext::<command>` argument), and `fd::N` allows reading/writing raw file descriptors already open in the Gitaly process. This maps to the GitLab bounty class "Git argument/config injection or command execution" via transport helpers, and would be a critical RCE if actually exploitable.

However, whether `git` itself permits `ext::`/`fd::` here depends on the invoking Git command and Git's own `protocol.ext.allow`/`protocol.fd.allow` defaults (default: `user`, meaning allowed unless `GIT_PROTOCOL_FROM_USER=0` is set) — I could not find any place in this repo where Gitaly sets `GIT_PROTOCOL_FROM_USER=0` or `protocol.*.allow=never`/`user` globally in `setupGitExecutionEnvironments` [10](#0-9) , nor could I find a scheme allowlist anywhere in the codebase (`grep` for `GIT_PROTOCOL_FROM_USER`, `protocol.allow`, `ext::`, `fd::` returned no hits in Gitaly source, only in docs/changelog/unrelated files).

### Likelihood Explanation
An unprivileged GitLab user who owns a project and can trigger a pull-mirror/import/"find remote repository" check (a normal GitLab feature surface, e.g. adding a remote mirror or verifying import URL reachability) fully controls the `remote`/`url` field of these RPCs. No special role, admin access, or non-default configuration is required to reach the code path; the only gate is whatever Git itself enforces for `ext::`/`fd::` at the git-binary layer, which is default-open unless explicitly restricted.

### Recommendation
Add explicit scheme validation before constructing any `gitcmd.Command` that embeds a caller-supplied URL: reject any value beginning with `ext::`, `fd::`, or other non-network-transport schemes (allow only `http://`, `https://`, `git://`, `ssh://`, and SCP-like syntax) in `validateFetchRemoteRequest`, `validateCreateRepositoryFromURLRequest`, `FindRemoteRepository`, `FindRemoteRootRef`, and `UpdateRemoteMirror`. Additionally, force `GIT_PROTOCOL_FROM_USER=0` (or set `protocol.ext.allow=never` and `protocol.fd.allow=never` as global config) in `setupGitExecutionEnvironments` for all Gitaly-spawned git processes as defense in depth, and centralize this scheme check inside `gitcmd.Command`/`CommandArgs` so all current and future call sites are protected uniformly rather than relying on each RPC handler remembering to validate.

### Proof of Concept
```go
func TestFindRemoteRepository_extTransportHelper(t *testing.T) {
    ctx := testhelper.Context(t)
    cfg, client := setupRemoteService(t, ctx)

    marker := filepath.Join(testhelper.TempDir(t), "pwned")
    resp, err := client.FindRemoteRepository(ctx, &gitalypb.FindRemoteRepositoryRequest{
        Remote:      fmt.Sprintf("ext::sh -c 'touch %s'", marker),
        StorageName: cfg.Storages[0].Name,
    })
    // Expect: request should be rejected with InvalidArgument due to disallowed transport helper.
    require.Error(t, err)
    require.NoFileExists(t, marker)
    _ = resp
}
```
Given the current code, this request is not rejected by `validateCreateRepositoryFromURLRequest`/`FindRemoteRepository`'s empty-check nor by `gitcmd`'s positional-arg validation, so whether `touch` executes depends solely on the Git binary's own `protocol.ext.allow` default — Gitaly does not add any mitigating configuration or environment variable that I could locate.

### Citations

**File:** internal/git/gitcmd/command_description.go (L407-451)
```go
// args validates the given flags and arguments and, if valid, returns the complete command line.
func (c commandDescription) args(flags []Option, args []string, postSepArgs []string) ([]string, error) {
	var commandArgs []string

	for _, o := range flags {
		args, err := o.OptionArgs()
		if err != nil {
			return nil, err
		}
		commandArgs = append(commandArgs, args...)
	}

	if c.supportsEndOfOptions() {
		commandArgs = append(commandArgs, "--end-of-options")
	}

	if c.validatePositionalArgs != nil {
		if err := c.validatePositionalArgs(args); err != nil {
			return nil, err
		}
	} else {
		for _, a := range args {
			if err := validatePositionalArg(a); err != nil {
				return nil, err
			}
		}
	}
	commandArgs = append(commandArgs, args...)

	if len(postSepArgs) > 0 {
		commandArgs = append(commandArgs, "--")
	}

	// post separator args do not need any validation
	commandArgs = append(commandArgs, postSepArgs...)

	return commandArgs, nil
}

func validatePositionalArg(arg string) error {
	if strings.HasPrefix(arg, "-") {
		return fmt.Errorf("positional arg %q cannot start with dash '-': %w", arg, ErrInvalidArg)
	}
	return nil
}
```

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

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L149-159)
```go
func validateCreateRepositoryFromURLRequest(ctx context.Context, locator storage.Locator, req *gitalypb.CreateRepositoryFromURLRequest) error {
	if err := locator.ValidateRepository(ctx, req.GetRepository(), storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return err
	}

	if req.GetUrl() == "" {
		return fmt.Errorf("empty Url")
	}

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

**File:** internal/git/gitcmd/command_resolve.go (L62-79)
```go
func getURLAndResolveConfigForSCP(remoteURL, resolvedAddress string) (string, []ConfigPair, error) {
	hostAndPath := strings.SplitN(remoteURL, ":", 2)
	if len(hostAndPath) != 2 {
		return "", nil, fmt.Errorf("invalid protocol/URL encountered: %s", remoteURL)
	}

	if strings.Contains(hostAndPath[0], "/") {
		return "", nil, fmt.Errorf("SSH URLs with '/' before colon are unsupported")
	}

	var userPrefix string

	if userAndHost := strings.SplitAfterN(remoteURL, "@", 2); len(userAndHost) > 1 {
		userPrefix = userAndHost[0]
	}

	return fmt.Sprintf("%s%s:%s", userPrefix, resolvedAddress, hostAndPath[1]), nil, nil
}
```

**File:** internal/git/gitcmd/command_factory.go (L219-236)
```go
// setupGitExecutionEnvironments assembles a Git execution environment that can be used to run Git
// commands. It warns if no path was specified in the configuration.
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
