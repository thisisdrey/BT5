### Title
Git transport-helper argument/protocol injection via unsanitized `Url` in `cloneFromURLCommand` allows RCE - (File: internal/gitaly/service/repository/create_repository_from_url.go)

### Summary
`cloneFromURLCommand` parses the attacker-supplied `CreateRepositoryFromURLRequest.Url` with `url.Parse`, strips only credentials, and passes the re-serialized string as a plain positional argument to `git clone --bare/--mirror <url> <path>` with no scheme allowlist. Git natively supports non-network "transport helper" schemes such as `ext::`, `fd::`, and `file::` that can execute arbitrary local commands or read arbitrary local files, and since Gitaly does not set `GIT_PROTOCOL_FROM_USER=0` or restrict `protocol.*.allow`, Git treats this argument as directly user-supplied and permits it by default.

### Finding Description
`cloneFromURLCommand` at [1](#0-0)  only performs `url.Parse` to strip embedded basic-auth credentials, and then re-serializes the URL with `u.String()`, which is passed unchanged (aside from the credential-resolve rewrite) as `Args: []string{urlString, repositoryFullPath}` in the `git clone` invocation at [2](#0-1) . There is no allowlist of schemes (e.g. restricting to `http`/`https`/`git`/`ssh`) anywhere in this function or in `validateCreateRepositoryFromURLRequest` [3](#0-2) , which only checks that the field is non-empty.

The `gitcmd.Command.Args` field is documented as unsafe for values that could start with `-` [4](#0-3) , but that protection is only about flag-style (`-`-prefixed) argument injection, not about Git's own scheme-based transport-helper dispatch (`ext::`, `fd::`), which is a distinct attack surface not addressed by `CommandArgs()`'s validation.

I inspected `ExecCommandFactory.newCommand`/`setupGitExecutionEnvironments`/`GlobalConfiguration` [5](#0-4) [6](#0-5)  and found no `GIT_ALLOW_PROTOCOL` environment variable, no `GIT_PROTOCOL_FROM_USER=0`, and no `protocol.ext.allow`/`protocol.fd.allow` configuration set anywhere in the shared environment or global git config applied to every command. Because Gitaly never marks this URL as "not user-provided," Git's own default protocol policy (which treats CLI-supplied remote URLs as user input and allows `ext`/`fd`/`file` transports by default under that classification) is not overridden, so the malicious scheme is not blocked at the Git level either.

### Impact Explanation
If reachable, a value like `Url: "ext::sh -c $@|sh . id>/tmp/pwn"` would cause Git to invoke an arbitrary shell command in the Gitaly process's execution context instead of performing a network clone, i.e., full remote command execution scoped to the Gitaly host/container. This corresponds to GitLab's "Remote Code Execution" bounty impact class, well beyond the intended "create a repo by cloning a URL" functionality.

### Likelihood Explanation
Exploitability depends entirely on whether GitLab Rails (or another caller in front of Gitaly) restricts the URL scheme before invoking `CreateRepositoryFromURL`. This repository (Gitaly) contains no such restriction of its own, so if any caller — GitLab Rails project-import, a Workhorse hook, or a directly-authorized gRPC client with a valid Gitaly auth token — forwards a user-supplied URL verbatim, the injection succeeds without further conditions. I was unable to confirm from this repo alone whether GitLab Rails performs scheme validation upstream of this RPC (that logic lives outside Gitaly), which is the deciding factor for real-world reachability by an "unauthenticated-scope"/unprivileged actor as framed in the question.

### Recommendation
In `cloneFromURLCommand` (or `validateCreateRepositoryFromURLRequest`), explicitly allowlist the URL scheme to `http`, `https`, `git`, and `ssh` (rejecting `ext`, `fd`, `file`, and any other scheme) before constructing the clone command, and additionally set `GIT_PROTOCOL_FROM_USER=0` combined with an explicit `protocol.allow`/`GIT_ALLOW_PROTOCOL=http:https:git:ssh` environment restriction on the spawned `git clone` process as defense in depth.

### Proof of Concept
```go
func TestCloneFromURLCommand_RejectsExtProtocol(t *testing.T) {
    // Craft a CreateRepositoryFromURLRequest with a malicious ext:: URL.
    req := &gitalypb.CreateRepositoryFromURLRequest{
        Repository: newRepoProto(t),
        Url:        "ext::sh -c $@|sh . id>/tmp/pwn",
    }
    _, err := client.CreateRepositoryFromURL(ctx, req)
    // Expected (after fix): InvalidArgument due to disallowed scheme.
    // Actual (current code): request proceeds to `git clone --bare "ext::sh -c $@|sh . id>/tmp/pwn" <path>`,
    // and /tmp/pwn is created on the Gitaly host if `id` is treated as user-provided by Git.
    require.Error(t, err)
}
```
Note: full confirmation of remote-triggerability requires verifying, outside of this repo, whether GitLab Rails or another upstream caller restricts the `Url` scheme before invoking this RPC; that verification is outside the scope of what is present in this Gitaly repository.

### Citations

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L36-60)
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
```

**File:** internal/gitaly/service/repository/create_repository_from_url.go (L77-84)
```go
	return s.gitCmdFactory.NewWithoutRepo(ctx,
		gitcmd.Command{
			Name:  "clone",
			Flags: cloneFlags,
			Args:  []string{urlString, repositoryFullPath},
		},
		append(opts, gitcmd.WithConfigEnv(config...))...,
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

**File:** internal/git/gitcmd/command.go (L28-38)
```go
	// Args is the arguments that shall be passed after all flags. These arguments must not be
	// flags and thus cannot start with `-`. Note that it may be unsafe to use this field in the
	// case where arguments are directly user-controlled. In that case it is advisable to use
	// `PostSepArgs` instead.
	Args []string
	// PostSepArgs is the arguments that shall be passed as positional arguments after the `--`
	// separator. Git recognizes that separator as the point where it should stop expecting any
	// options and treat the remaining arguments as positionals. This should be used when
	// passing user-controlled input of arbitrary form like for example paths, which may start
	// with a `-`.
	PostSepArgs []string
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

**File:** internal/git/gitcmd/command_factory.go (L649-722)
```go
func (cf *ExecCommandFactory) GlobalConfiguration(ctx context.Context) ([]ConfigPair, error) {
	// As global options may cancel out each other, we have a clearly defined order in which
	// globals get applied. The order is similar to how git handles configuration options from
	// most general to most specific. This allows callsites to override options which would
	// otherwise be set up automatically. The exception to this is configuration specified by
	// the admin, which always overrides all other items. The following order of precedence
	// applies:
	//
	// 1. Globals which get set up by default for all git commands.
	// 2. Globals which get set up by default for a given git command.
	// 3. Globals passed via command options, e.g. as set up by
	//    `WithReftxHook()`.
	// 4. Configuration as provided by the admin in Gitaly's config.toml.
	config := []ConfigPair{
		// Disable automatic garbage collection as we handle scheduling
		// of it ourselves.
		{Key: "gc.auto", Value: "0"},

		// Disable automatic maintenance as we never enable any tasks.
		{Key: "maintenance.auto", Value: "0"},

		// CRLF line endings will get replaced with LF line endings when writing blobs to the
		// object database. No conversion is done when reading blobs from the object database.
		// This is required for the web editor. With feature flag "autocrlf_false" enabled
		// CRLF line endings will not get replaced and be left alone.
		{Key: "core.autocrlf", Value: "false"},

		// Git allows the use of replace refs, where a given object ID can be replaced with a
		// different one. The result is that Git commands would use the new object instead of the
		// old one in almost all contexts. This is a security threat: an adversary may use this
		// mechanism to replace malicious commits with seemingly benign ones. We thus globally
		// disable this mechanism.
		{Key: "core.useReplaceRefs", Value: "false"},

		// Change the size of files we consider to be big from 512MB to 50MB. This setting influences a bunch of
		// things for blobs that are larger than this size:
		//
		// - They will not be slurped into memory anymore, but will instead use streaming interfaces. This
		//   should reduce memory consumption as we don't have to allocate up to 512MB buffers anymore.
		//
		// - They will not be diffed anymore. This should significantly reduce the time it
		//   takes to computes diffs when such diffs contain huge blobs. This is of course at the cost of not
		//   being able to show any such diffs anymore, but overall it seems unreasonable to compute diffs for
		//   any such huge files anyway.
		//
		// - They will not be deltified anymore. This should ultimately be a no-op for us as we have already
		//   been setting `pack.windowSize=100m` already, which restricts the maximum window size. The value of
		//   50MB has thus been chosen such that it matches 2 times the window size.
		//
		// So ultimately, this should not lead to larger packfiles as we have already been restricting the
		// packfile window anyway while it should on the other hand lead to lower memory consumption and faster
		// computation of diffs when large blobs are involved.
		{Key: "core.bigFileThreshold", Value: fmt.Sprintf("%dm", BigFileThresholdMB)},

		// When generating packfiles, Git tries to reuse parts of the packfile verbatim so that it does not have
		// to recompute deltas. The goal of this is to speed up the generation of the packfile while reducing
		// computational resources required. This optimization only works for the preferred packfile though,
		// which typically is the largest packfile in the repository. Especially larger repositories may have a
		// significant amount of objects stored in different packfiles, so none of those objects would be
		// reused.
		//
		// Git has recently introduced multi-pack reuse of objects to address this inefficiency and make reuse
		// of objects work across multiple packfiles as long as those objects are mapped by a multi-pack index
		// with an accompanying bitmap. As this is a recent addition to Git this feature is not enabled by
		// default yet, but needs to be opted in with a configuration.
		{Key: "pack.allowPackReuse", Value: "multi"},

		// Force MIDX v1 during the transition period where GIT_VERSION_PREV writes
		// version 1 while GIT_VERSION_MASTER and nightly builds write version 2
		// (upstream 105a22cf69, "Merge branch 'tb/incremental-midx-part-3.2'", 2026-03-25).
		// Once GIT_VERSION_PREV is bumped past that commit, this config can be removed
		// and the test updated to expect version 2.
		{Key: "midx.version", Value: "1"},
	}
```
