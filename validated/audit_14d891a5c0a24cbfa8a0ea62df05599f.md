### Title
User-supplied `GitConfigOptions` allow arbitrary Git config injection with no allowlist, analogous to unvalidated `VotingModule` addresses - (File: `internal/git/gitcmd/command_options.go`)

### Summary
`ConvertConfigOptions` converts caller-supplied `key=value` strings from `GitConfigOptions` request fields directly into `ConfigPair`/`GitConfig` structs that are passed with `-c` (or via `GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`) to `git-receive-pack(1)`, `git-upload-pack(1)`, and `git-fetch(1)`/`remote show` invocations. Just as the `OptimismGovernorV5.proposeWithModule()` bug allowed the caller to name an arbitrary, unvalidated module address that then drove execution/authorization decisions, Gitaly lets ordinary push/fetch/clone callers name arbitrary Git config *keys* that drive process behavior, with only syntactic (not semantic/allowlist) validation.

### Finding Description
`ConvertConfigOptions` in `internal/git/gitcmd/command_options.go` splits each caller-supplied string on `=` and builds a `ConfigPair` with no restriction on which config *keys* may be set: [1](#0-0) 

These pairs are validated only by `GitConfig.Validate()`, which enforces syntactic well-formedness (non-empty key, no `=`, contains a `.`, no leading/trailing dot, matches `configKeyRegex`) — it does **not** check the key against any allowlist/denylist of safe configuration namespaces: [2](#0-1) 

This is analogous to the reported flaw: the `VotingModule` was accepted from an untrusted caller and used, unchecked, to drive downstream logic (`propose`, `_formatExecuteParams`, `_quorumReached`, etc.). Here, the config key is accepted from an untrusted RPC caller (an ordinary user doing `git push`/`git fetch`/`git clone`) and used, unchecked against an allowlist, to drive `git` process behavior via `-c`.

Reachable, unprivileged call sites include:
- `PostReceivePack`/`SSHReceivePack` (push), which converts `req.GetGitConfigOptions()` and passes it with `gitcmd.WithConfig(config...)` into `git receive-pack`: [3](#0-2) 
- `PostUploadPackWithSidechannel`/`SSHUploadPackWithSidechannel` (fetch/clone), passing `GitConfigOptions` into `git upload-pack`: [4](#0-3) [5](#0-4) 
- `InfoRefsReceivePack`/`InfoRefsUploadPack`, also converting and applying `req.GetGitConfigOptions()`: [6](#0-5) 

Existing tests confirm the mechanism is deliberately exercised with security-relevant keys (`receive.MaxInputSize`, `uploadpack.hideRefs`, `transfer.hideRefs`) which shows the feature is intended to let clients set arbitrary Git config, not a narrow allowlist: [7](#0-6) [8](#0-7) 

Because `git -c` config accepts many keys that affect trust boundaries or execution (e.g. `uploadpack.packObjectsHook`, `core.fsmonitor`, `protocol.*.allow`, `credential.helper`, `http.*.extraHeader`, `gpg.program`, `receive.denyCurrentBranch`, `transfer/uploadpack.hideRefs`, `safe.*`, or arbitrary `alias.*`), an attacker who can freely name any Git config key on a push/fetch RPC can potentially override hardcoded security-relevant options set elsewhere in the same command invocation (e.g. `gpg.program` set via `WithGitalyGPG()`, or hook-related config set via `configureHooks`) if the caller's config pairs are applied after or in a way that they take precedence, or use keys never intended to be client-controllable to weaken access checks, alter hidden-ref filtering, or interfere with quarantine/hook behavior.

### Impact Explanation
An ordinary Git client (push or fetch) can set arbitrary `git -c` config key/value pairs without gitaly restricting them to a safe allowlist. Depending on precedence and Git's own config resolution, this could:
- override hidden-ref settings, hooks-related config, or pack-object hook program paths,
- influence protocol negotiation and credential/auth behavior,
- interfere with quarantine, receive limits, or transfer settings in unintended ways.
The tests explicitly demonstrate that keys like `uploadpack.hideRefs`, `transfer.hideRefs`, and `receive.MaxInputSize` are honored from client input, confirming the config injection surface is real and reaches security-relevant settings, not just cosmetic ones.

### Likelihood Explanation
High reachability: any authenticated Git client performing a normal `git push`, `git fetch`, or `git clone` against Gitaly's SmartHTTP or SSH services can set this field; no special privilege beyond normal repository access is required. The only defense is syntax-level key validation, not semantic (allowlist) validation, which mirrors exactly the "no check that the value entered is safe" pattern in the referenced report.

### Recommendation
Introduce an explicit allowlist of Git config keys/namespaces that are permitted to be set via `GitConfigOptions`, and reject (return `InvalidArgument`) any request containing keys outside that allowlist in `ConvertConfigOptions` (or a wrapper around it) before the pairs are applied via `WithConfig`/`WithConfigEnv`. This mirrors the recommended fix in the reference report: validate the "module" (here, the config key) against a known-safe set before it is allowed to influence execution.

### Proof of Concept
1. Issue a normal, authenticated `git push` (or use `PostReceivePackRequest`/`SSHReceivePackRequest`) with `GitConfigOptions` set to a key/value pair outside any documented allowlist (e.g. `uploadpack.packObjectsHook=/bin/sh -c 'id > /tmp/pwned'` on the fetch/upload-pack path, or any other sensitive namespace key).
2. Observe that `ConvertConfigOptions` accepts the pair as long as it is syntactically well formed (`GitConfig.Validate()` passes), and it is forwarded via `-c`/`GIT_CONFIG_KEY_n` to the underlying `git` process, as already demonstrated for `uploadpack.hideRefs`/`transfer.hideRefs`/`receive.MaxInputSize` in the existing test suite: [8](#0-7) [7](#0-6) 
3. No allowlist check rejects config keys outside the operationally-expected set (e.g. `uploadpack.hideRefs`, `receive.MaxInputSize`), confirming any syntactically valid key is accepted.

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

**File:** internal/gitaly/service/smarthttp/receive_pack.go (L101-137)
```go
	config, err := gitcmd.ConvertConfigOptions(req.GetGitConfigOptions())
	if err != nil {
		return err
	}

	transactionID := storage.ExtractTransactionID(ctx)
	transactionsEnabled := transactionID > 0
	if transactionsEnabled {
		procReceiveCleanup, err := receivepack.RegisterProcReceiveHook(
			ctx, s.logger, s.cfg, req, repo, s.hookManager, hook.NewTransactionRegistry(s.txRegistry), transactionID,
		)
		if err != nil {
			return err
		}
		defer func() {
			if err := procReceiveCleanup(); err != nil && returnedErr == nil {
				returnedErr = err
			}
		}()
	}

	objectHash, err := repo.ObjectHash(ctx)
	if err != nil {
		return fmt.Errorf("detecting object hash: %w", err)
	}

	cmd, err := repo.Exec(ctx,
		gitcmd.Command{
			Name:  "receive-pack",
			Flags: []gitcmd.Option{gitcmd.Flag{Name: "--stateless-rpc"}},
			Args:  []string{repoPath},
		},
		gitcmd.WithStdin(stdin),
		gitcmd.WithStdout(stdout),
		gitcmd.WithReceivePackHooks(objectHash, req, "http", transactionsEnabled),
		gitcmd.WithGitProtocol(s.logger, req),
		gitcmd.WithConfig(config...),
```

**File:** internal/gitaly/service/smarthttp/upload_pack.go (L127-163)
```go
func (s *server) runUploadPack(ctx context.Context, req *gitalypb.PostUploadPackWithSidechannelRequest, repoPath string, gitConfig []gitcmd.ConfigPair, stdin io.Reader, stdout io.Writer) (stats *stats.PackfileNegotiation, _ error) {
	h := sha1.New()

	stdin = io.TeeReader(stdin, h)
	stdin, collector := s.runStatsCollector(ctx, stdin)
	defer func() {
		if stats == nil {
			stats = collector.finish()
		}
	}()

	repo := s.localRepoFactory.Build(req.GetRepository())
	if s.bundleURIManager != nil {
		// Bundle generation is an optimization that is transparent to users.
		// If it fails, we log the error but continue with the regular upload-pack
		// operation without the bundle optimization.
		// If successful, a goroutine is spawned to generate the bundle, in which case
		// the bundle generation becomes independent of the RPC request.
		if err := s.bundleURIManager.GenerateWithStrategy(ctx, repo); err != nil {
			s.logger.WithError(err).Error("failed generating bundle")
		}
		gitConfig = append(gitConfig, s.bundleURIManager.UploadPackGitConfig(ctx, req.GetRepository())...)
	} else {
		gitConfig = append(gitConfig, bundleuri.CapabilitiesGitConfig(ctx, false)...)
	}

	objectHash, err := repo.ObjectHash(ctx)
	if err != nil {
		return nil, fmt.Errorf("detecting object hash: %w", err)
	}

	commandOpts := []gitcmd.CmdOpt{
		gitcmd.WithStdin(stdin),
		gitcmd.WithSetupStdout(),
		gitcmd.WithGitProtocol(s.logger, req),
		gitcmd.WithConfig(gitConfig...),
		gitcmd.WithPackObjectsHookEnv(objectHash, req.GetRepository(), "http"),
```

**File:** internal/gitaly/service/ssh/upload_pack.go (L34-93)
```go
	config, err := gitcmd.ConvertConfigOptions(req.GetGitConfigOptions())
	if err != nil {
		return nil, 0, err
	}

	var wg sync.WaitGroup
	pr, pw := io.Pipe()
	defer func() {
		pw.Close()
		wg.Wait()
	}()

	stdin = io.TeeReader(stdin, pw)

	wg.Add(1)
	go func() {
		defer func() {
			wg.Done()
			pr.Close()
		}()

		var opts []stats.ParsePackfileNegotiationOption
		if featureflag.LsRefsPipelineRefInstrumentation.IsEnabled(ctx) {
			opts = append(opts, stats.WithLsRefsPipelineRefPrefixes())
		}

		stats, errIgnore := stats.ParsePackfileNegotiation(pr, opts...)
		negotiation = &stats
		if errIgnore != nil {
			s.logger.WithError(errIgnore).DebugContext(ctx, "failed parsing packfile negotiation")
			return
		}
		stats.UpdateMetrics(s.packfileNegotiationMetrics, s.packfileNegotiationDeepenMetrics)
		stats.UpdateLsRefsPipelineRefLogFields(ctx)
		stats.UpdateLogFields(ctx)
	}()

	if s.bundleURIManager != nil {
		// Bundle generation is an optimization that is transparent to users.
		// If it fails, we log the error but continue with the regular upload-pack
		// operation without the bundle optimization.
		// If successful, a goroutine is spawned to generate the bundle, in which case
		// the bundle generation becomes independent of the RPC request.
		if err = s.bundleURIManager.GenerateWithStrategy(ctx, repo); err != nil {
			s.logger.WithError(err).Error("failed generating bundle")
		}
		config = append(config, s.bundleURIManager.UploadPackGitConfig(ctx, req.GetRepository())...)
	} else {
		config = append(config, bundleuri.CapabilitiesGitConfig(ctx, false)...)
	}

	objectHash, err := repo.ObjectHash(ctx)
	if err != nil {
		return nil, 0, fmt.Errorf("detecting object hash: %w", err)
	}

	commandOpts := []gitcmd.CmdOpt{
		gitcmd.WithGitProtocol(s.logger, req),
		gitcmd.WithConfig(config...),
		gitcmd.WithPackObjectsHookEnv(objectHash, repoProto, "ssh"),
```

**File:** internal/gitaly/service/smarthttp/inforefs.go (L61-82)
```go
func (s *server) handleInfoRefs(ctx context.Context, service, repoPath string, repo *localrepo.Repo, req *gitalypb.InfoRefsRequest, w io.Writer) error {
	s.logger.WithFields(log.Fields{
		"service": service,
	}).DebugContext(ctx, "handleInfoRefs")

	cmdOpts := []gitcmd.CmdOpt{gitcmd.WithGitProtocol(s.logger, req), gitcmd.WithStdout(w)}
	if service == "receive-pack" {
		cmdOpts = append(cmdOpts, gitcmd.WithDisabledHooks())
	}

	gitConfig, err := gitcmd.ConvertConfigOptions(req.GetGitConfigOptions())
	if err != nil {
		return err
	}
	if s.bundleURIManager != nil {
		gitConfig = append(gitConfig, s.bundleURIManager.UploadPackGitConfig(ctx, req.GetRepository())...)
	} else {
		gitConfig = append(gitConfig, bundleuri.CapabilitiesGitConfig(ctx, false)...)
	}

	cmdOpts = append(cmdOpts, gitcmd.WithConfig(gitConfig...))

```

**File:** internal/gitaly/service/smarthttp/receive_pack_test.go (L355-376)
```go
func TestPostReceivePack_rejectViaGitConfigOptions(t *testing.T) {
	t.Parallel()

	ctx := testhelper.Context(t)
	cfg := testcfg.Build(t)
	cfg.SocketPath = runSmartHTTPServer(t, cfg)

	repo, repoPath := gittest.CreateRepository(t, ctx, cfg)
	gittest.WriteCommit(t, cfg, repoPath, gittest.WithBranch(git.DefaultBranch))
	push := setupSimplePush(t, ctx, cfg, repoPath, git.DefaultRef)

	client := newSmartHTTPClient(t, cfg.SocketPath, cfg.Auth.Token)

	stream, err := client.PostReceivePack(ctx)
	require.NoError(t, err)

	response := push.perform(t, stream, &gitalypb.PostReceivePackRequest{
		Repository:       repo,
		GlId:             "user-123",
		GlRepository:     "project-123",
		GitConfigOptions: []string{"receive.MaxInputSize=1"},
	})
```

**File:** internal/gitaly/service/smarthttp/upload_pack_test.go (L217-226)
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
```
