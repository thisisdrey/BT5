[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Title
Attacker-supplied GitConfigOptions can override `core.hooksPath` set by `configureHooks`, disabling Git hook enforcement - ([File: internal/git/gitcmd/hooks_options.go])

### Summary
`configureHooks` (invoked via `WithReceivePackHooks`) appends `core.hooksPath` (and, for proc-receive, `receive.procReceiveRefs`) to the command's `globals` slice. In `internal/gitaly/service/ssh/receive_pack.go` and the equivalent smarthttp handler, `gitcmd.WithConfig(config...)` — built directly from the client-supplied `GitConfigOptions` — is applied after `WithReceivePackHooks`, so the attacker's config pairs land later in the same `globals` slice and thus later in the final `-c` argv sequence passed to `git-receive-pack`. Because git honors the last `-c` occurrence for single-valued keys, an attacker can override `core.hooksPath` to point at an empty/non-existent directory, silently disabling pre-receive/update/post-receive/proc-receive hook execution.

### Finding Description
`configureHooks` sets `cc.globals = append(cc.globals, ConfigPair{Key: "core.hooksPath", Value: gitCmdFactory.HooksPath(ctx)})` [5](#0-4) . This is invoked via `WithReceivePackHooks`, one of the `CmdOpt`s passed into `repo.Exec(...)` in `sshReceivePack`:

```go
cmd, err := repo.Exec(ctx, gitcmd.Command{Name: "receive-pack", Args: []string{repoPath}},
    gitcmd.WithStdin(pr),
    gitcmd.WithStdout(stdout),
    gitcmd.WithStderr(stderr),
    gitcmd.WithReceivePackHooks(objectHash, req, "ssh", transactionsEnabled),
    gitcmd.WithGitProtocol(s.logger, req),
    gitcmd.WithConfig(config...),
)
``` [6](#0-5) 

Here `config` is produced from `gitcmd.ConvertConfigOptions(req.GetGitConfigOptions())` earlier in the same function [7](#0-6) , and `ConvertConfigOptions` performs no allowlisting or denylisting of keys — it simply splits on `=` [4](#0-3) . `WithConfig` appends its pairs to the same `cc.globals` slice used by hooks configuration [8](#0-7) .

Since `CmdOpt`s are applied strictly in the order passed to `repo.Exec`/`combineOpts` [9](#0-8) , `WithReceivePackHooks`'s `core.hooksPath` entry is appended to `cc.globals` before the attacker's `WithConfig(config...)` entries. `combineArgs` then concatenates `cc.globals` verbatim into the final argv as sequential `-c key=value` flags with no deduplication [10](#0-9) . Git resolves duplicate single-valued `-c` keys by using the last occurrence, so an attacker-supplied `core.hooksPath` will win over the Gitaly-configured one.

There is no existing allowlist, denylist, or post-hoc validation anywhere in `ConvertConfigOptions`, `WithConfig`, or `combineArgs` that rejects or strips security-sensitive keys such as `core.hooksPath` or `receive.procReceiveRefs` from client-supplied `GitConfigOptions`.

### Impact Explanation
By supplying `GitConfigOptions: ["core.hooksPath=/nonexistent/or/attacker/dir"]` in an `SSHReceivePackRequest` or `PostReceivePackRequest`, an unprivileged pusher can cause `git-receive-pack` to look for pre-receive/update/post-receive/proc-receive hooks in a directory that does not contain Gitaly's `gitaly-hooks` symlinks. Git silently skips missing hook scripts, so this **disables hook enforcement entirely** for that push — bypassing the pre-receive hook that GitLab Rails uses (via Gitaly's internal API calls embedded in the hook) to enforce protected branches, push rules, quota limits, and other server-side checks, and bypassing `receive.procReceiveRefs`-driven transaction handling. This maps to the "hook or quarantine bypass" impact class and can result in unauthorized ref updates that evade GitLab's access-control and compliance checks. Full arbitrary code execution via attacker-controlled hook scripts is not achievable this way, since the attacker has no filesystem write access to place executable hook files at an arbitrary server path — only the disabling/no-op effect is reliably exploitable.

### Likelihood Explanation
The precondition (attacker controls `GitConfigOptions` in their own push request) is trivially met by any authenticated user capable of pushing to a repository they have write access to (including their own fork), with no special role or config required. The exploit requires no timing tricks and is fully deterministic given git's documented `-c` last-value-wins semantics, making it highly repeatable.

### Recommendation
Strip or reject any client-supplied `GitConfigOptions` keys that match security-sensitive namespaces before converting them (e.g. `core.hooksPath`, `receive.procReceiveRefs`, `core.fsmonitor`, `uploadpack.packObjectsHook`, etc.), or alternatively apply `WithReceivePackHooks`/hook-related globals strictly after user-supplied `WithConfig` entries in `cc.globals` ordering, and additionally have `combineArgs` de-duplicate global config keys so that Gitaly-internal entries always take precedence regardless of insertion order.

### Proof of Concept
```go
func TestSSHReceivePack_HooksPathOverrideByGitConfigOptions(t *testing.T) {
    cfg, repoProto, _ := setupServer(t) // existing test helper
    req := &gitalypb.SSHReceivePackRequest{
        Repository:       repoProto,
        GlId:             "user-123",
        GitConfigOptions: []string{"core.hooksPath=/tmp/does-not-exist"},
    }

    stream, err := client.SSHReceivePack(ctx)
    require.NoError(t, err)
    require.NoError(t, stream.Send(req))
    // send a pack that updates a protected/guarded ref
    require.NoError(t, stream.Send(&gitalypb.SSHReceivePackRequest{Stdin: packData}))
    require.NoError(t, stream.CloseSend())

    // Expected (fixed) behavior: RPC rejects the override or ignores it,
    // hooks still execute (e.g., custom pre-receive hook writes a marker file).
    // Observed (vulnerable) behavior: push succeeds and marker file is absent,
    // proving the pre-receive hook never ran because core.hooksPath was
    // overridden to a directory without gitaly-hooks symlinks.
    require.FileExists(t, markerFilePath)
}
```
Expected assertion in a fixed implementation: the RPC either strips the `core.hooksPath` override before invoking `git-receive-pack`, or fails the request outright when a security-sensitive config key is present in `GitConfigOptions`, and the configured hook always executes.

### Citations

**File:** internal/git/gitcmd/hooks_options.go (L153-182)
```go
	if requestedHooks&ProcReceiveHook != 0 {
		cc.globals = append(cc.globals, ConfigPair{Key: "receive.procReceiveRefs", Value: "refs"})
	}

	payload, err := NewHooksPayload(
		ctx,
		cfg,
		repo,
		objectHash,
		transaction,
		userDetails,
		requestedHooks,
		featureflag.FromContext(ctx),
		storage.ExtractTransactionID(ctx),
	).Env()
	if err != nil {
		return err
	}

	cc.env = append(
		cc.env,
		payload,
		// Setting GOTRACEBACK=none ensures that if the gitaly-hooks process hits a fatal Go
		// runtime error, the runtime does not dump a verbose stacktrace to stderr. This causes
		// a lot of noise on the Git client side.
		"GOTRACEBACK=none",
	)
	cc.env = envInjector(ctx, cc.env)

	cc.globals = append(cc.globals, ConfigPair{Key: "core.hooksPath", Value: gitCmdFactory.HooksPath(ctx)})
```

**File:** internal/gitaly/service/ssh/receive_pack.go (L96-99)
```go
	config, err := gitcmd.ConvertConfigOptions(req.GetGitConfigOptions())
	if err != nil {
		return err
	}
```

**File:** internal/gitaly/service/ssh/receive_pack.go (L160-167)
```go
	cmd, err := repo.Exec(ctx, gitcmd.Command{Name: "receive-pack", Args: []string{repoPath}},
		gitcmd.WithStdin(pr),
		gitcmd.WithStdout(stdout),
		gitcmd.WithStderr(stderr),
		gitcmd.WithReceivePackHooks(objectHash, req, "ssh", transactionsEnabled),
		gitcmd.WithGitProtocol(s.logger, req),
		gitcmd.WithConfig(config...),
	)
```

**File:** internal/git/gitcmd/command_factory.go (L584-588)
```go
	for _, opt := range opts {
		if err := opt(ctx, cf.cfg, cf, &config); err != nil {
			return cmdCfg{}, err
		}
	}
```

**File:** internal/git/gitcmd/command_factory.go (L616-637)
```go
	var commandOpts []GlobalOption
	if commandDescription.opts != nil {
		commandOpts = commandDescription.opts(ctx)
	}

	combinedGlobals := make([]GlobalOption, 0, len(globalConfig)+len(commandOpts)+len(cc.globals)+len(cf.cfg.Git.Config))
	for _, configPair := range globalConfig {
		combinedGlobals = append(combinedGlobals, configPair)
	}
	combinedGlobals = append(combinedGlobals, commandOpts...)
	combinedGlobals = append(combinedGlobals, cc.globals...)
	for _, configPair := range cf.cfg.Git.Config {
		combinedGlobals = append(combinedGlobals, configPair)
	}

	for _, global := range combinedGlobals {
		globalArgs, err := global.GlobalArgs()
		if err != nil {
			return nil, err
		}
		args = append(args, globalArgs...)
	}
```

**File:** internal/git/gitcmd/command_options.go (L117-131)
```go
// ConvertConfigOptions converts `<key>=<value>` config entries into `ConfigPairs`.
func ConvertConfigOptions(options []string) ([]ConfigPair, error) {
	configPairs := make([]ConfigPair, len(options))

	for i, option := range options {
		configPair := strings.SplitN(option, "=", 2)
		if len(configPair) != 2 {
			return nil, fmt.Errorf("cannot convert invalid config key: %q", option)
		}

		configPairs[i] = ConfigPair{Key: configPair[0], Value: configPair[1]}
	}

	return configPairs, nil
}
```

**File:** internal/git/gitcmd/command_options.go (L193-201)
```go
// WithConfig adds git configuration entries to the command.
func WithConfig(configPairs ...ConfigPair) CmdOpt {
	return func(_ context.Context, _ config.Cfg, _ CommandFactory, c *cmdCfg) error {
		for _, configPair := range configPairs {
			c.globals = append(c.globals, configPair)
		}
		return nil
	}
}
```
