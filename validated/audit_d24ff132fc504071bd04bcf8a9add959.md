## Analysis

The report's core bug class is: a function accepts an arbitrary target/arbitrary payload from an untrusted caller and forwards it verbatim into a privileged low-level call, letting the caller redirect that call to achieve an unintended, high-impact operation.

Looking at Gitaly's `GitConfigOptions` mechanism, `ConvertConfigOptions()` in `internal/git/gitcmd/command_options.go` takes a caller-supplied `key=value` string, splits it, and constructs a `ConfigPair` with **no allowlist or denylist of config keys** — it validates only that a `=` separator exists. [1](#0-0) 

This is exposed to unprivileged, ordinary RPC callers via the `GitConfigOptions` field on RPCs such as `PostUploadPackWithSidechannel`, `InfoRefsRequest`, and `SSHUploadPackWithSidechannelRequest`, and gets injected as `-c key=value` (or `GIT_CONFIG_KEY_N`/`GIT_CONFIG_VALUE_N` env vars) directly into the `git upload-pack` invocation for the target repository: [2](#0-1) [3](#0-2) 

The only validation applied to a config *key* is the generic syntactic check in `config.GitConfig.Validate()` (must contain a dot, no `=`, matches a key-format regex) — there is no semantic restriction preventing dangerous keys: [4](#0-3) 

Existing tests demonstrate that this mechanism can already override server-set behavior like `transfer.hideRefs` and `uploadpack.hideRefs`, proving the config value is genuinely applied to the invoked git process with real effect: [5](#0-4) 

Separately, Gitaly's own pack-objects-cache feature relies on setting `uploadpack.packObjectsHook=<gitaly-hooks binary>` as a *global* option appended after per-request config, so that it executes `gitaly-hooks` (a specific trusted binary) instead of `git pack-objects` directly: [6](#0-5) 

However, per-request `GitConfigOptions` are applied via `gitcmd.WithConfig(config...)` *before* `WithPackObjectsHookEnv` appends its own global, and since Git config precedence for repeated `-c`/`GIT_CONFIG_KEY_N` entries generally takes the **last** occurrence, the ordering in which these are appended to `cc.globals`/`c.env` determines whether the client-supplied key would win or lose against the trusted default. I was not able to fully trace the final ordering/precedence logic in `cmdCfg` assembly (where `c.globals` and `c.env` get flattened into the actual `exec.Cmd` arguments/environment) within the remaining budget, so I cannot conclusively confirm whether a client can override `uploadpack.packObjectsHook` (or other security-sensitive keys such as `core.fsmonitor`, `protocol.ext.allow`, or filter/diff driver commands that themselves execute arbitrary programs) end-to-end.

### Title
Unrestricted client-supplied Git config keys via `GitConfigOptions` risk config/argument injection into privileged git-upload-pack invocation - (File: internal/git/gitcmd/command_options.go)

### Summary
`ConvertConfigOptions()` accepts arbitrary `key=value` strings from the `GitConfigOptions` repeated field present on unprivileged, ordinary-user RPCs (`InfoRefsRequest`, `PostUploadPackWithSidechannelRequest`, `SSHUploadPackWithSidechannelRequest`) and turns them into `ConfigPair`s that are injected as `-c` flags / `GIT_CONFIG_KEY_N`/`GIT_CONFIG_VALUE_N` environment variables directly into the `git upload-pack` process Gitaly spawns for that RPC, with no allowlist restricting which git config namespaces/keys a caller may set.

### Finding Description
`ConvertConfigOptions` in `internal/git/gitcmd/command_options.go` (lines 117-131) only validates that the string contains a `=` separator; the resulting key is further checked only for generic syntactic validity (must contain a dot, no `=`, regex match) in `config.GitConfig.Validate()` (`internal/gitaly/config/config.go` lines 436-470). Neither function restricts the *semantic* set of config keys/namespaces that may be set. This is analogous to the reported Solidity bug in that a caller-controlled "target + payload" pair (here: config key + value) is forwarded unfiltered into a privileged operation (the invocation of `git upload-pack`/`git-http-backend` for the target repository), letting the caller redirect that operation's behavior.

The mechanism is wired into unprivileged RPC handlers, e.g. `internal/gitaly/service/ssh/upload_pack.go` (lines 34-37, 90-94), where `req.GetGitConfigOptions()` is converted and applied via `gitcmd.WithConfig(config...)`. Tests confirm the values are genuinely applied and can already change security-relevant behavior of the invoked git process, such as overriding `uploadpack.hideRefs`/`transfer.hideRefs` to unhide otherwise-hidden references (`internal/gitaly/service/smarthttp/upload_pack_test.go` lines 216-235).

### Impact Explanation
If the config-option injection point can reach or interact with keys that control command execution paths (e.g., diff/merge/filter driver commands, `core.fsmonitor`, or ordering interactions with Gitaly's own `uploadpack.packObjectsHook` setting used by the pack-objects cache in `internal/git/gitcmd/hooks_options.go` lines 57-93), an ordinary caller could potentially cause the spawned git process to execute or behave in ways not intended by the server operator, escalating from a hidden-ref-disclosure issue (already demonstrated) toward command/argument injection or gating bypass in the RPC-handling code path.

### Likelihood Explanation
The `GitConfigOptions` field is reachable by any client authorized to call the affected RPCs (no special privilege beyond normal repository read access) and is a first-class, currently supported feature (used deliberately to support `hideRefs`), so the injection surface is definitely reachable. What remains unverified is whether Gitaly's config precedence/ordering (append order of globals/env vars between client-supplied and server-trusted config, e.g. relative to `uploadpack.packObjectsHook`) prevents a client value from overriding a security-critical server-set key — I could not confirm this ordering logic within the available tool budget.

### Recommendation
Introduce an explicit allowlist of git config keys/namespaces permitted via `GitConfigOptions` in `ConvertConfigOptions` (or a wrapper around it), rejecting keys outside a known-safe set (e.g., `uploadpack.hideRefs`, `transfer.hideRefs`, and similar already-relied-upon keys), and ensure any Gitaly-internal security-relevant config (such as `uploadpack.packObjectsHook`) is applied in a way that cannot be shadowed by client-supplied `-c`/`GIT_CONFIG_KEY_N` entries regardless of append order.

### Proof of Concept
Not independently reproduced beyond the existing repository test that demonstrates client-controlled override of hidden-ref config (`internal/gitaly/service/smarthttp/upload_pack_test.go` lines 216-235); a full PoC targeting a security-critical key (e.g. attempting to override `uploadpack.packObjectsHook` via `GitConfigOptions`) was not executed and would require verifying Gitaly's global/env-ordering logic which was not fully traceable within this session.

### Citations

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

**File:** internal/gitaly/service/ssh/upload_pack.go (L34-37)
```go
	config, err := gitcmd.ConvertConfigOptions(req.GetGitConfigOptions())
	if err != nil {
		return nil, 0, err
	}
```

**File:** internal/gitaly/service/ssh/upload_pack.go (L90-94)
```go
	commandOpts := []gitcmd.CmdOpt{
		gitcmd.WithGitProtocol(s.logger, req),
		gitcmd.WithConfig(config...),
		gitcmd.WithPackObjectsHookEnv(objectHash, repoProto, "ssh"),
	}
```

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

**File:** internal/gitaly/service/smarthttp/upload_pack_test.go (L216-235)
```go

	t.Run("failing request because of hidden ref config", func(t *testing.T) {
		rpcRequest := &gitalypb.PostUploadPackWithSidechannelRequest{
			Repository: repo,
			GitConfigOptions: []string{
				"uploadpack.hideRefs=refs/hidden",
				"uploadpack.allowAnySHA1InWant=false",
			},
		}
		response, err := makeRequest(t, ctx, cfg.SocketPath, cfg.Auth.Token, rpcRequest, bytes.NewReader(requestBody.Bytes()))
		testhelper.RequireGrpcError(t, structerr.NewFailedPrecondition("running upload-pack: waiting for upload-pack: exit status 128"), err)

		// The failure message proves that upload-pack failed because of
		// GitConfigOptions, and that proves that passing GitConfigOptions works.
		require.Equal(t,
			gittest.Pktlinef(t, "ERR upload-pack: not our ref %v", hiddenID),
			response.String(),
		)
	})
}
```

**File:** internal/git/gitcmd/hooks_options.go (L57-93)
```go
// WithPackObjectsHookEnv provides metadata for gitaly-hooks so it can act as a pack-objects hook.
func WithPackObjectsHookEnv(objectHash git.ObjectHash, repo *gitalypb.Repository, protocol string) CmdOpt {
	return func(ctx context.Context, cfg config.Cfg, gitCmdFactory CommandFactory, cc *cmdCfg) error {
		if !cfg.PackObjectsCache.Enabled {
			return nil
		}

		if repo == nil {
			return fmt.Errorf("missing repo: %w", ErrInvalidArg)
		}

		userDetails := &UserDetails{
			Protocol: protocol,
			UserID:   metadata.GetValue(ctx, "user_id"),
			Username: metadata.GetValue(ctx, "username"),
			RemoteIP: metadata.GetValue(ctx, "remote_ip"),
		}

		if err := cc.configureHooks(
			ctx,
			cfg,
			objectHash,
			repo,
			gitCmdFactory,
			userDetails,
			PackObjectsHook,
		); err != nil {
			return fmt.Errorf("pack-objects hook configuration: %w", err)
		}

		cc.globals = append(cc.globals, ConfigPair{
			Key:   "uploadpack.packObjectsHook",
			Value: cfg.BinaryPath("gitaly-hooks"),
		})

		return nil
	}
```
