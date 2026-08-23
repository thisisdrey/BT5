### Title
Missing access control on `HookService` RPCs allows any authenticated gRPC client to invoke push hooks and forge quarantine object directories on arbitrary repositories - (File: internal/gitaly/service/hook/pre_receive.go)

### Summary
The Jigsaw `claimRewards()` bug is a missing-caller-restriction flaw: a function meant to be invoked only through a trusted intermediary (`StrategyManager`) instead accepted calls from any address and acted on attacker-supplied target data. Gitaly's `HookService` (`PreReceiveHook`, `PostReceiveHook`, `UpdateHook`, `ReferenceTransactionHook`) has the same class of defect: these RPCs are meant to be invoked exclusively by the `gitaly-hooks` helper binary over Gitaly's *internal* Unix socket during an in-flight `git-receive-pack` invocation, but they are also registered and reachable on Gitaly's regular external gRPC socket, and the handlers perform no check that the call actually originates from a legitimate, in-progress push. Any client holding a standard Gitaly auth token can call these RPCs directly with attacker-chosen repository, user identity, and environment-variable parameters.

### Finding Description
`PreReceiveHook` only validates that the target repository proto is well-formed via `locator.ValidateRepository` [1](#0-0) ; it performs no check that the caller is the internal `gitaly-hooks` process, that a corresponding push/transaction is actually underway, or that the `EnvironmentVariables`/`GitPushOptions`/`Changes` supplied in the request correspond to real objects being pushed. The handler then forwards these entirely caller-controlled fields into `GitLabHookManager.preReceiveHook`, which:

- Extracts `GIT_OBJECT_DIRECTORY` / `GIT_ALTERNATE_OBJECT_DIRECTORIES` straight from the attacker-supplied `EnvironmentVariables` and installs them onto the `repo` proto as the quarantine directories used for subsequent lookups [2](#0-1) .
- Builds `gitlab.AllowedParams` from these caller-controlled `RepoPath`, `GitObjectDirectory`, `GLRepository`, `GLID`, `Changes`, etc., and calls the Rails `/internal/allowed` endpoint [3](#0-2) .
- On success, executes any admin-installed custom `pre-receive` hook with attacker-controlled `stdin`/env [4](#0-3) .

A code comment in the test suite explicitly acknowledges that `HookService` is unintentionally reachable from the external API and that this is a known, unresolved issue (linked to gitlab-org/gitaly#3746): "we should stop serving HookService on the external socket given it is a service intended only to be used internally by Gitaly for hook callbacks" [5](#0-4) . The design documentation confirms the intended trust model — the payload (including `GLID`, protocol, and object-directory info) is supposed to be generated exclusively by Gitaly itself and handed to `gitaly-hooks` via an internal, authenticated channel [6](#0-5) , and the internal-socket dial path uses a distinct `InternalSocketToken` specifically for this trust boundary [7](#0-6) . None of that is enforced server-side for the RPC itself — the RPC handlers accept the same request shape regardless of which socket/caller it arrives from.

### Impact Explanation
This mirrors the `claimRewards()` root cause precisely: an operation intended to run only as a sub-step of a trusted flow (there: `StrategyManager.invest`; here: `git-receive-pack` executed by Gitaly itself) instead accepts direct invocation with attacker-supplied target/context data. Consequences:
- **Cross-repository/forged quarantine object access**: an attacker-controlled `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` value is trusted and passed on to Rails and to `git` subprocesses as the "quarantine" location for a claimed push, letting a caller assert an arbitrary object directory is associated with a repository it doesn't actually control the push for.
- **Hook/quarantine gating bypass**: custom admin `pre-receive`/`update`/`post-receive` hooks can be triggered outside of any real push, with forged `GLID`/`Username`/`Changes`, causing hook side effects (audit logging, external notifications, protected-branch decisions) to be executed under a spoofed identity/ref-update that never happened.
- **Auth/business-logic bypass towards Rails**: forged `AllowedParams` (GLRepository, GLID, Changes) sent to `/internal/allowed` can be used to probe or manipulate access-control decisions tied to a specific repository without a corresponding legitimate push.

### Likelihood Explanation
Exploitation requires only a valid Gitaly auth token for the external gRPC endpoint (the same level of access needed to call any other Gitaly RPC, e.g. from a compromised/malicious client using a legitimate service account, similarly to how the `claimRewards()` PoC used an ordinary unprivileged "attacker" address) — it does not require internal-socket access, node compromise, or any special privilege beyond what an ordinary Gitaly RPC client already has. The test file itself demonstrates that `HookService.PreReceiveHook` can be invoked over the same external client used for standard RPCs [8](#0-7) , and the maintainers' own comment confirms awareness that this exposure is unintended.

### Recommendation
Restrict `HookService` RPCs so they can only be served/accepted on the internal socket (or otherwise validated to be invoked only from the `gitaly-hooks` process for an in-flight transaction), analogous to adding the `onlyStrategyManager`-style modifier in the Jigsaw fix. Concretely: stop registering `HookService` on the external gRPC listener (per the referenced upstream issue #3746), and/or require and verify the `InternalSocketToken`/transaction binding server-side for every `HookService` call regardless of which socket it arrives on, rejecting calls that aren't tied to a legitimate, currently-running transaction/push for the specified repository.

### Proof of Concept
Not independently reproduced in this analysis; the vulnerable code paths were located and the design assumption (HookService should be internal-only) is documented and acknowledged as unresolved in the repository's own test comments [5](#0-4) , but I was unable to execute Gitaly to empirically confirm end-to-end exploitation (e.g., that `git-receive-pack`/object-availability checks don't independently block a standalone `PreReceiveHook` call from taking effect). This should be validated in a running environment (e.g., by calling `HookService.PreReceiveHook` on the external socket with a forged `GLID` and quarantine env vars against a real repository) to determine the exact blast radius, since the getRelativeObjectDirs() path-validation logic referenced in `internal/gitaly/hook/prereceive.go` could not be fully reviewed in this session.

### Citations

**File:** internal/gitaly/service/hook/pre_receive.go (L67-69)
```go
func validatePreReceiveHookRequest(ctx context.Context, locator storage.Locator, in *gitalypb.PreReceiveHookRequest) error {
	return locator.ValidateRepository(ctx, in.GetRepository())
}
```

**File:** internal/gitaly/hook/prereceive.go (L108-116)
```go
	if gitObjDir, gitAltObjDirs := env.ExtractValue(envs, "GIT_OBJECT_DIRECTORY"), env.ExtractValue(envs, "GIT_ALTERNATE_OBJECT_DIRECTORIES"); gitObjDir != "" && gitAltObjDirs != "" {
		gitObjectDirRel, gitAltObjectDirRel, err := getRelativeObjectDirs(repoPath, gitObjDir, gitAltObjDirs)
		if err != nil {
			return structerr.NewInternal("getting relative git object directories: %w", err)
		}

		repo.GitObjectDirectory = gitObjectDirRel
		repo.GitAlternateObjectDirectories = gitAltObjectDirRel
	}
```

**File:** internal/gitaly/hook/prereceive.go (L135-148)
```go
	params := gitlab.AllowedParams{
		RepoPath:                      repoPath,
		RelativePath:                  repo.GetRelativePath(),
		GitObjectDirectory:            repo.GetGitObjectDirectory(),
		GitAlternateObjectDirectories: repo.GetGitAlternateObjectDirectories(),
		GLRepository:                  repo.GetGlRepository(),
		GLID:                          payload.UserDetails.UserID,
		GLProtocol:                    payload.UserDetails.Protocol,
		Changes:                       string(changes),
		PushOptions:                   pushOptions,
		ClientContext:                 payload.GitalyClientContext,
	}

	allowed, message, err := m.gitlabClient.Allowed(ctx, params)
```

**File:** internal/gitaly/hook/prereceive.go (L176-195)
```go
	executor, err := m.newCustomHooksExecutor(ctx, repo, "pre-receive")
	if err != nil {
		return fmt.Errorf("creating custom hooks executor: %w", err)
	}

	customEnv, err := m.customHooksEnv(ctx, payload, pushOptions, envs)
	if err != nil {
		return structerr.NewInternal("constructing custom hook environment: %w", err)
	}

	if err = executor(
		ctx,
		nil,
		customEnv,
		bytes.NewReader(changes),
		stdout,
		stderr,
	); err != nil {
		return fmt.Errorf("executing custom hooks: %w", err)
	}
```

**File:** internal/gitaly/service/hook/pre_receive_test.go (L180-204)
```go
	}

	// Rails sends the repository's relative path from the access checks as provided by Gitaly. If transactions are enabled,
	// this is the snapshot's relative path.
	//
	// Transaction middleware fails if this metadata is not present but this is not correct. We only start transactions when
	// they come through the external API, not when it comes through the internal socket used by hooks to call into Gitaly.
	// This test setup however is calling the HookService through the external API.
	//
	// For now, include the header so the test runs. In the longer term, we should stop serving HookService on the external
	// socket given it is a service intended only to be used internally by Gitaly for hook callbacks.
	//
	// Related issue: https://gitlab.com/gitlab-org/gitaly/-/issues/3746
	ctx = metadata.AppendToOutgoingContext(ctx, storagemgr.MetadataKeySnapshotRelativePath, repo.GetRelativePath())

	stream, err := client.PreReceiveHook(ctx)
	require.NoError(t, err)
	require.NoError(t, stream.Send(&req))

	stdout, stderr, status := receivePreReceive(t, stream, stdin)

	require.Equal(t, int32(0), status)
	assert.Equal(t, "", text.ChompBytes(stderr), "hook stderr")
	assert.Equal(t, "", text.ChompBytes(stdout), "hook stdout")
}
```

**File:** doc/hooks.md (L15-36)
```markdown
We inject some information into Git commands to set up the hook execution
environment for both Git and ourselves so that the `gitaly-hooks` executable is
able to derive required information:

- We inject the `core.hooksPath` config entry, which points to the directory
  containing our global hooks. Global hooks are set up as a temporary directory
  containing symlinks to a wrapper script which is able to locate the
  `gitaly-hooks` executable.
- We inject `GITALY_HOOKS_PAYLOAD`, which contains JSON-formatted data. This
  payload encodes various information:
  - Which hooks have been requested. Gitaly uses this to only configure a
    subset of hooks even if the executed Git command would run additional
    hooks.
  - Information about the repository the hook is executed in.
  - Information about how to connect to Gitaly.
  - Information about the ongoing transaction, if any.
  - Information about the user who triggered the hook. This is required for
    access checks.

The `gitaly-hooks` executable is only a gateway to be able to use gRPC and
execute the hook logic inside of the Gitaly server process. It uses the injected
information to connect to Gitaly and execute the respective RPC call. The
```

**File:** internal/git/gitcmd/hooks_payload.go (L83-87)
```go
	// InternalSocket is the path to Gitaly's internal socket.
	InternalSocket string `json:"internal_socket"`
	// InternalSocketToken is the token required to authenticate with
	// Gitaly's internal socket.
	InternalSocketToken string `json:"internal_socket_token"`
```
