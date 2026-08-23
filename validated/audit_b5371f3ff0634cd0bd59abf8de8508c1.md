### Title
Git config keys/values pushed through `WithConfigEnv` bypass `GitConfig.Validate()`, allowing unvalidated attacker-controlled data into `GIT_CONFIG_KEY_N`/`GIT_CONFIG_VALUE_N` - ([File: internal/git/gitcmd/command_options.go])

### Summary
### Finding Description
`config.GitConfig` (aliased as `gitcmd.ConfigPair`) has a `Validate()` method that enforces that a config key is non-empty, contains no `=`, contains a section (a `.`), does not start/end with `.`, and matches `configKeyRegex` [1](#0-0) . This validation is only invoked from `GlobalArgs()`, which is used when config pairs are passed via the `-c` flag through `WithConfig()` and consumed in `combineArgs()` (`global.GlobalArgs()`) [2](#0-1) [3](#0-2) .

However, the sibling code path `ConfigPairsToGitEnvironment()`, used by `WithConfigEnv()` to inject the same `ConfigPair` values as `GIT_CONFIG_KEY_N`/`GIT_CONFIG_VALUE_N` environment variables, never calls `Validate()` at all — it directly does `fmt.Sprintf("GIT_CONFIG_KEY_%d=%s", i, configPair.Key)` [4](#0-3) [5](#0-4) . This is exactly the "constructor vs. setter" inconsistency described in the external report: one code path that produces a `ConfigPair`-derived Git argument validates the key format, the other equivalent path that produces the same kind of Git configuration input skips validation entirely.

This unvalidated path is directly reachable with attacker-controlled data: in `FindRemoteRootRef`, the RPC builds `ConfigPair{Key: fmt.Sprintf("http.%s.extraHeader", request.GetRemoteUrl()), ...}` directly from the client-supplied `RemoteUrl` and `HttpAuthorizationHeader` fields, and passes it straight to `WithConfigEnv(config...)` without any prior call to `GitConfig.Validate()` [6](#0-5) . The same `WithConfigEnv` sink is also used from other remote/URL-handling call sites (`command_resolve.go`, `localrepo/refs.go`, `localrepo/remote.go`, `create_repository_from_url.go`, `fetch_remote.go`, `update_remote_mirror.go`, `localrepo/bundle.go`), all of which take remote URLs or resolve-config data that ultimately becomes `ConfigPair` keys/values fed through the same unvalidated `ConfigPairsToGitEnvironment` sink.

### Impact Explanation
Because `ConfigPairsToGitEnvironment` skips the format checks that `GitConfig.Validate()` performs (no `=` in key, must contain a dot-delimited section, must match `configKeyRegex`), a caller-controlled remote URL or HTTP header value can be embedded verbatim into a Git config key without any of the sanitization/rejection that is enforced on the equivalent `-c`-flag path. This is a clear parity break between two functions that are supposed to produce the same kind of git-configuration input (one from a "constructor"-style path, one from a "setter"-style path), consistent with the class of bug identified in the report. At minimum this allows malformed/attacker-shaped config keys to reach Git's environment-based config-parsing machinery unfiltered, which can result in unexpected git config being applied or git erroring/behaving inconsistently depending on git version and key contents, since it bypasses the intentional guardrails Gitaly built specifically to keep config keys well-formed.

### Likelihood Explanation
Reachable directly from an unprivileged RPC caller: `FindRemoteRootRef` takes `RemoteUrl` and `HttpAuthorizationHeader` from the request and uses them to build the config key/value pair that flows into `WithConfigEnv` with zero validation [7](#0-6) . No special privileges are required beyond calling the RPC.

### Recommendation
Call `configPair.Validate()` for every entry inside `ConfigPairsToGitEnvironment()` (or in `WithConfigEnv()` before building the environment slice), mirroring the validation performed in `GitConfig.GlobalArgs()`, so that both code paths that turn a `ConfigPair` into git input enforce the same key/value sanity checks.

### Proof of Concept
Not directly runnable without live Gitaly infrastructure to observe the resulting git behavior; the code-path analysis above demonstrates the missing call by contrasting `internal/gitaly/config/config.go:472-480` (`GlobalArgs`, which validates) against `internal/git/gitcmd/command_options.go:48-61` (`ConfigPairsToGitEnvironment`, which does not), with `internal/gitaly/service/remote/find_remote_root_ref.go:17-51` as the concrete attacker-reachable call site that builds a `ConfigPair` from `RemoteUrl`/`HttpAuthorizationHeader` and routes it through the unvalidated `WithConfigEnv` sink.

### Citations

**File:** internal/gitaly/config/config.go (L436-470)
```go
// Validate validates that the Git configuration conforms to a format that Git understands.
func (cfg GitConfig) Validate() error {
	// Even though redundant, this block checks for a few things up front to give better error
	// messages to the administrator in case any of the keys fails validation.
	if cfg.Key == "" {
		return cfgerror.NewValidationError(cfgerror.ErrNotSet, "key")
	}
	if strings.Contains(cfg.Key, "=") {
		return cfgerror.NewValidationError(
			fmt.Errorf(`key %q cannot contain "="`, cfg.Key),
			"key",
		)
	}
	if !strings.Contains(cfg.Key, ".") {
		return cfgerror.NewValidationError(
			fmt.Errorf("key %q must contain at least one section", cfg.Key),
			"key",
		)
	}
	if strings.HasPrefix(cfg.Key, ".") || strings.HasSuffix(cfg.Key, ".") {
		return cfgerror.NewValidationError(
			fmt.Errorf("key %q must not start or end with a dot", cfg.Key),
			"key",
		)
	}

	if !configKeyRegex.MatchString(cfg.Key) {
		return cfgerror.NewValidationError(
			fmt.Errorf("key %q failed regexp validation", cfg.Key),
			"key",
		)
	}

	return nil
}
```

**File:** internal/gitaly/config/config.go (L472-480)
```go
// GlobalArgs generates a git `-c <key>=<value>` flag. Returns an error if `Validate()` fails to
// validate the config key.
func (cfg GitConfig) GlobalArgs() ([]string, error) {
	if err := cfg.Validate(); err != nil {
		return nil, fmt.Errorf("invalid configuration key %q: %w", cfg.Key, err)
	}

	return []string{"-c", fmt.Sprintf("%s=%s", cfg.Key, cfg.Value)}, nil
}
```

**File:** internal/git/gitcmd/command_factory.go (L631-637)
```go
	for _, global := range combinedGlobals {
		globalArgs, err := global.GlobalArgs()
		if err != nil {
			return nil, err
		}
		args = append(args, globalArgs...)
	}
```

**File:** internal/git/gitcmd/command_options.go (L48-61)
```go
// ConfigPairsToGitEnvironment converts the given config pairs into a set of environment variables
// that can be injected into a Git executable.
func ConfigPairsToGitEnvironment(configPairs []ConfigPair) []string {
	env := make([]string, 0, len(configPairs)*2+1)

	for i, configPair := range configPairs {
		env = append(env,
			fmt.Sprintf("GIT_CONFIG_KEY_%d=%s", i, configPair.Key),
			fmt.Sprintf("GIT_CONFIG_VALUE_%d=%s", i, configPair.Value),
		)
	}

	return append(env, fmt.Sprintf("GIT_CONFIG_COUNT=%d", len(configPairs)))
}
```

**File:** internal/git/gitcmd/command_options.go (L203-211)
```go
// WithConfigEnv adds git configuration entries to the command's environment. This should be used
// in place of `WithConfig()` in case config entries may contain secrets which shouldn't leak e.g.
// via the process's command line.
func WithConfigEnv(configPairs ...ConfigPair) CmdOpt {
	return func(_ context.Context, _ config.Cfg, _ CommandFactory, c *cmdCfg) error {
		c.env = append(c.env, ConfigPairsToGitEnvironment(configPairs)...)
		return nil
	}
}
```

**File:** internal/gitaly/service/remote/find_remote_root_ref.go (L17-51)
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

	if authHeader := request.GetHttpAuthorizationHeader(); authHeader != "" {
		config = append(config, gitcmd.ConfigPair{
			Key:   fmt.Sprintf("http.%s.extraHeader", request.GetRemoteUrl()),
			Value: "Authorization: " + authHeader,
		})
	}

	repo := s.localRepoFactory.Build(request.GetRepository())

	return repo.Exec(ctx,
		gitcmd.Command{
			Name:   "remote",
			Action: "show",
			Args:   []string{"inmemory"},
		},
		gitcmd.WithDisabledHooks(),
		gitcmd.WithConfigEnv(config...),
		gitcmd.WithSetupStdout(),
	)
```
