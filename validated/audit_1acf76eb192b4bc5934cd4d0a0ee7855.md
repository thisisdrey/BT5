### Title
Missing Default Packfile Size Limit in `PostReceivePack`/`SSHReceivePack` Enables Disk/CPU Exhaustion DoS - (`internal/gitaly/service/smarthttp/receive_pack.go`)

### Summary
Gitaly's git-push RPCs (`PostReceivePack` and `SSHReceivePack`) stream client-supplied packfile data directly into `git-receive-pack(1)`'s stdin with no default upper bound on the amount of data accepted. The only mechanism that can cap the packfile size (`receive.maxInputSize`) is a Git config option that must be explicitly supplied by the *caller* via `GitConfigOptions` in the request; Gitaly itself never sets a default value for it.

### Finding Description
`PostReceivePack` reads the raw stream and pipes it straight to `git-receive-pack --stateless-rpc`, only translating whatever `git_config_options` the client passed into `-c key=value` pairs: [1](#0-0) 

The same unbounded pattern exists for the SSH transport: [2](#0-1) 

The command-description layer that builds the default global Git configuration for `receive-pack` sets fsck, push-options, and autogc-related config, but never a size cap such as `receive.maxInputSize`: [3](#0-2) 

The only place `receive.MaxInputSize` appears in the codebase at all is a unit test that demonstrates it can *optionally* be supplied per-request: [4](#0-3) 

This proves the control is opt-in and caller-supplied rather than a hard default enforced by Gitaly. Similarly, `Gitaly`'s global git configuration (`GlobalConfiguration`) that is applied to every git command contains no packfile/input-size limiting, only memory/perf tuning knobs like `core.bigFileThreshold` and `pack.allowPackReuse`: [5](#0-4) 

Because the size cap is not enforced at the Gitaly layer itself, any deployment or client path that fails to inject `receive.maxInputSize` (e.g., a compromised or misconfigured Rails/Workhorse layer, or any alternate caller of the Gitaly gRPC API directly) allows an ordinary authenticated user to push an arbitrarily large packfile. Git will unpack it into the quarantine directory and run `pre-receive`/access checks against the full object set before Gitaly or the hook has any opportunity to reject it based on size, consuming disk, memory, and CPU proportional to the attacker-controlled input.

### Impact Explanation
An oversized push can exhaust disk space in the repository's quarantine/object directory, consume large amounts of memory/CPU during unpack/fsck, and stall the receive-pack process for extended periods, degrading or denying the Gitaly node's ability to serve other repositories/requests. Because there is a per-repository/per-RPC concurrency limit (`doc/backpressure.md`) but no data-size limit, several large pushes can still be queued and processed, amplifying resource pressure and potentially causing repository corruption or storage-node instability — consistent with a DoS analog to the WASM-file-size report.

### Likelihood Explanation
Likelihood depends on the calling layer (e.g., GitLab Rails/Workhorse) reliably passing `receive.maxInputSize` on every `PostReceivePack`/`SSHReceivePack` call. Since Gitaly's own code exposes no default, any gap in that upstream enforcement (misconfiguration, direct gRPC API use bypassing Rails, or an internal API path that forgets to set the option) results in unauthenticated-magnitude push sizes being fully processed by Gitaly. This is a reachable path from an ordinary authenticated git push, requiring no privileged actor or credential leak.

### Recommendation
Enforce a server-side default input/packfile size limit inside Gitaly itself (e.g., always inject a configurable `receive.maxInputSize` default via `GlobalConfiguration`/`commandDescriptions["receive-pack"]`, similar to how `core.bigFileThreshold` and `pack.allowPackReuse` are unconditionally set), so the guarantee doesn't depend entirely on the caller. Make the default overridable through `config.toml` so operators can tune it, but ensure a safe non-zero default exists out of the box.

### Proof of Concept
1. Configure a Gitaly instance directly (bypassing Rails/Workhorse) or call `PostReceivePack`/`SSHReceivePack` via the gRPC API without ever supplying `git_config_options` containing `receive.maxInputSize`.
2. Push a very large packfile (e.g., tens of GB of blob data) to the repository.
3. Observe that `git-receive-pack --stateless-rpc` is spawned and processes the entire packfile without any size ceiling from Gitaly's own configuration, as shown in `postReceivePack` (`internal/gitaly/service/smarthttp/receive_pack.go`), consuming disk/CPU/memory until the push completes, fails on unrelated grounds, or the host runs out of resources.

### Citations

**File:** internal/gitaly/service/smarthttp/receive_pack.go (L94-138)
```go
	repo := s.localRepoFactory.Build(req.GetRepository())

	repoPath, err := repo.Path(ctx)
	if err != nil {
		return err
	}

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
	)
```

**File:** internal/gitaly/service/ssh/receive_pack.go (L91-99)
```go
	repoPath, err := s.locator.GetRepoPath(ctx, req.GetRepository())
	if err != nil {
		return err
	}

	config, err := gitcmd.ConvertConfigOptions(req.GetGitConfigOptions())
	if err != nil {
		return err
	}
```

**File:** internal/git/gitcmd/command_description.go (L273-291)
```go
	"receive-pack": {
		flags: 0,
		opts: func(ctx context.Context) []GlobalOption {
			return append(append(append([]GlobalOption{
				// In case the repository belongs to an object pool, we want to prevent
				// Git from including the pool's refs in the ref advertisement. We do
				// this by rigging core.alternateRefsCommand to produce no output.
				// Because Git itself will append the pool repository directory, the
				// command ends with a "#". The end result is that Git runs `/bin/sh -c 'exit 0 # /path/to/pool.git`.
				ConfigPair{Key: "core.alternateRefsCommand", Value: "exit 0 #"},

				// Make git-receive-pack(1) advertise the push options
				// capability to clients.
				ConfigPair{Key: "receive.advertisePushOptions", Value: "true"},
				// Disable automatic garbage collection. We want to control the GC scheduling ourselves.
				ConfigPair{Key: "receive.autogc", Value: "false"},
			}, hiddenReceivePackRefPrefixes(ctx)...), receiveFsckConfiguration(ctx)...), packConfiguration(ctx)...)
		},
	},
```

**File:** internal/gitaly/service/smarthttp/receive_pack_test.go (L355-386)
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

	requireSideband(t, []string{
		gittest.Pktlinef(t, "\x02fatal: pack exceeds maximum allowed size\n"),
		gittest.Pktlinef(t, "\x01%s", strings.Join([]string{
			gittest.Pktlinef(t, "unpack unpack-objects abnormal exit\n"),
			gittest.Pktlinef(t, "ng %s unpacker error\n", git.DefaultRef),
			"0000",
		}, "")),
	}, response)
}
```

**File:** internal/git/gitcmd/command_factory.go (L662-722)
```go
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
