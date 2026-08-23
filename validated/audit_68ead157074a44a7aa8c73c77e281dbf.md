### Title
Missing GlId ownership verification allows arbitrary identity spoofing in `SSHReceivePack`/`PostReceivePack` hook authorization - ([File: internal/gitaly/service/ssh/receive_pack.go], [File: internal/gitaly/hook/prereceive.go])

### Summary
This is structurally the same bug class as the reported `create_stop_order_ticket` issue: a piece of caller-supplied identity data (`account_id` embedded in `encrypted_details` there; `GlId`/`GlUsername` here) is trusted at face value and threaded into a downstream authorization/audit decision, without verifying that it actually corresponds to the entity making the call.

### Finding Description
`SSHReceivePackRequest` and `PostReceivePackRequest` carry a client-supplied `gl_id` / `gl_username` field [1](#0-0) , [2](#0-1) . Gitaly's only validation of this field is that it is non-empty [3](#0-2) , [4](#0-3) . Gitaly then directly copies this value into `gitcmd.UserDetails.UserID`/`Username` used to build the hooks payload [5](#0-4) , which becomes the `GLID` parameter sent to the `pre-receive` "Allowed" authorization check and the `GL_ID` environment variable exposed to custom server hooks [6](#0-5) , [7](#0-6) .

Exactly like the Move ticket, which set `user_address` from the caller and then let a different function (`place_stop_order`) act on the embedded `account_id` without cross-checking, Gitaly builds its authorization/audit identity from a value taken directly from the RPC payload rather than from an independently verified session/token identity, and forwards it unchanged into the access-check call and hook environment.

### Impact Explanation
Whoever can issue an `SSHReceivePack`/`PostReceivePack` RPC to Gitaly (this is typically `gitlab-shell`/Workhorse acting on behalf of an SSH/HTTP git client) fully controls the `GlId`/`GlUsername` fields. Downstream:
- The Rails `Allowed` (pre-receive) authorization decision is keyed off this attacker-suppliable `GLID`.
- Custom server-side hooks receive `GL_ID`/`GL_USERNAME` env vars that are trusted as the acting user's identity for audit/enforcement logic in those hooks.

If the trust boundary that is supposed to guarantee `GlId` was set correctly by an already-authenticated Workhorse/gitlab-shell caller is bypassed or misconfigured anywhere in the chain (e.g., a compromised or overly-permissive internal caller, or any code path that forwards a request with attacker-influenced `GlId`), Gitaly performs authorization checks and generates audit trails against an impersonated identity, exactly mirroring the "ticket claims another user's account" vulnerability pattern in the report — allowing actions to be attributed to, and access-checked against, an arbitrary chosen `GlId` instead of the real caller.

### Likelihood Explanation
Reaching this path only requires the ability to send a `PostReceivePack`/`SSHReceivePack` RPC to Gitaly directly (bypassing or spoofing the normal gitlab-shell/Workhorse call), since Gitaly itself performs no cross-check that the supplied `GlId` matches an authenticated caller identity — it validates only non-emptiness. Any internal API surface, misconfigured token boundary, or component with network access to Gitaly's internal RPC socket can trigger this without git-specific tricks.

### Recommendation
Do not derive the authorization/hook identity solely from client-supplied `GlId`/`GlUsername` proto fields. Bind the acting user's identity to the already-verified session established via the internal auth token/Workhorse handshake, and validate that the `GlId` embedded in the request corresponds to that verified identity before it is passed to `gitlabClient.Allowed`/`PostReceive` or exposed as `GL_ID` to custom hooks — analogous to adding the issuer/account-ID cross-check recommended for `place_stop_order`.

### Proof of Concept
Any client with the ability to invoke Gitaly's `SSHReceivePack`/`PostReceivePack` RPC directly can set an arbitrary `GlId`:
```go
stream.Send(&gitalypb.SSHReceivePackRequest{
    Repository: victimRepo,
    GlId:       "victim-user-id",
    GlUsername: "victim",
})
```
As shown in the test helpers, this value is accepted with no cross-check and flows straight into the hooks payload and `GL_ID` environment variable [8](#0-7) , [9](#0-8) , causing the `pre-receive`/`post-receive` authorization and custom hook execution to run under the spoofed identity.

### Citations

**File:** proto/ssh.proto (L61-76)
```text
message SSHReceivePackRequest {
  // repository is the repository where git-receive-pack(1) is spawned.
  Repository repository = 1 [(target_repository)=true];
  // stdin is a chunk of raw data to be copied to git-receive-pack(1) standard input
  bytes stdin = 2;
  // gl_id is the GitLab ID of the user. This is used by Git {pre,post}-receive hooks.
  string gl_id = 3;
  // gl_repository refers to the GitLab repository. This is used by Git {pre,post}-receive hooks.
  string gl_repository = 4;
  // gl_username is the GitLab Username of the user. This is used by Git {pre,post}-receive hooks.
  string gl_username = 5;
  // git_protocol is the git protocol version.
  string git_protocol = 6;
  // git_config_options are parameters to use with git -c (key=value pairs).
  repeated string git_config_options = 7;
}
```

**File:** proto/smarthttp.proto (L93-114)
```text
// PostReceivePackRequest is the request for the PostReceivePack rpc. It is a stream used to
// transfer the raw data from the client to the servers stdin of git-receive-pack(1) process.
message PostReceivePackRequest {
  // repository is the repository on which to operate.
  // It should only be present in the first message of the stream.
  Repository repository = 1 [(target_repository)=true];
  // data is the raw data to be copied to stdin of 'git receive-pack'.
  bytes data = 2;
  // gl_id is the GitLab ID of the user. This is used by Git {pre,post}-receive hooks.
  // It should only be present in the first message of the stream.
  string gl_id = 3;
  // gl_repository refers to the GitLab repository. This is used by Git {pre,post}-receive hooks.
  // It should only be present in the first message of the stream.
  string gl_repository = 4;
  // gl_username is the GitLab Username of the user. This is used by Git {pre,post}-receive hooks.
  // It should only be present in the first message of the stream.
  string gl_username = 5;
  // git_protocol is the git protocol version.
  string git_protocol = 6;
  // git_config_options are parameters to use with git -c (key=value pairs).
  repeated string git_config_options = 7;
}
```

**File:** internal/gitaly/service/ssh/receive_pack.go (L200-207)
```go
func validateFirstReceivePackRequest(ctx context.Context, locator storage.Locator, req *gitalypb.SSHReceivePackRequest) error {
	if req.GetGlId() == "" {
		return errors.New("empty GlId")
	}
	if req.Stdin != nil {
		return errors.New("non-empty data in first request")
	}
	return locator.ValidateRepository(ctx, req.GetRepository())
```

**File:** internal/gitaly/service/smarthttp/receive_pack.go (L150-161)
```go
func validateReceivePackRequest(ctx context.Context, locator storage.Locator, req *gitalypb.PostReceivePackRequest) error {
	if req.GetGlId() == "" {
		return structerr.NewInvalidArgument("empty GlId")
	}
	if req.Data != nil {
		return structerr.NewInvalidArgument("non-empty Data")
	}
	if err := locator.ValidateRepository(ctx, req.GetRepository()); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	return nil
```

**File:** internal/gitaly/hook/receivepack/receive_pack.go (L253-294)
```go
func setupHooksPayloadEnv(ctx context.Context, cfg config.Cfg, req gitcmd.ReceivePackRequest, repo *localrepo.Repo, hook gitcmd.Hook) (string, error) {
	var protocol string
	switch req.(type) {
	case *gitalypb.SSHReceivePackRequest:
		protocol = "ssh"
	case *gitalypb.PostReceivePackRequest:
		protocol = "http"
	}

	var praefectTx *txinfo.Transaction
	if tx, err := txinfo.TransactionFromContext(ctx); err == nil {
		praefectTx = &tx
	} else if !errors.Is(err, txinfo.ErrTransactionNotFound) {
		return "", fmt.Errorf("getting transaction: %w", err)
	}

	objectHash, err := repo.ObjectHash(ctx)
	if err != nil {
		return "", fmt.Errorf("detecting object hash: %w", err)
	}

	hooksPayload, err := gitcmd.NewHooksPayload(
		ctx,
		cfg,
		req.GetRepository(),
		objectHash,
		praefectTx,
		&gitcmd.UserDetails{
			UserID:   req.GetGlId(),
			Username: req.GetGlUsername(),
			Protocol: protocol,
		},
		hook,
		featureflag.FromContext(ctx),
		storage.ExtractTransactionID(ctx),
	).Env()
	if err != nil {
		return "", fmt.Errorf("new hooks payload env: %w", err)
	}

	return hooksPayload, nil
}
```

**File:** internal/gitaly/hook/prereceive.go (L102-146)
```go
func (m *GitLabHookManager) preReceiveHook(ctx context.Context, payload gitcmd.HooksPayload, repo *gitalypb.Repository, pushOptions, envs []string, changes []byte, stdout, stderr io.Writer) error {
	repoPath, err := m.locator.GetRepoPath(ctx, repo)
	if err != nil {
		return structerr.NewInternal("getting repo path: %w", err)
	}

	if gitObjDir, gitAltObjDirs := env.ExtractValue(envs, "GIT_OBJECT_DIRECTORY"), env.ExtractValue(envs, "GIT_ALTERNATE_OBJECT_DIRECTORIES"); gitObjDir != "" && gitAltObjDirs != "" {
		gitObjectDirRel, gitAltObjectDirRel, err := getRelativeObjectDirs(repoPath, gitObjDir, gitAltObjDirs)
		if err != nil {
			return structerr.NewInternal("getting relative git object directories: %w", err)
		}

		repo.GitObjectDirectory = gitObjectDirRel
		repo.GitAlternateObjectDirectories = gitAltObjectDirRel
	}

	if len(changes) == 0 {
		return structerr.NewInternal("hook got no reference updates")
	}

	if repo.GetGlRepository() == "" {
		return structerr.NewInternal("repository not set")
	}
	if payload.UserDetails == nil {
		return structerr.NewInternal("payload has no receive hooks info")
	}
	if payload.UserDetails.UserID == "" {
		return structerr.NewInternal("user ID not set")
	}
	if payload.UserDetails.Protocol == "" {
		return structerr.NewInternal("protocol not set")
	}

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
```

**File:** internal/gitaly/hook/custom.go (L213-220)
```go
	return append(customEnvs,
		"GIT_DIR="+repoPath,
		"GL_REPOSITORY="+payload.Repo.GetGlRepository(),
		"GL_PROJECT_PATH="+payload.Repo.GetGlProjectPath(),
		"GL_ID="+payload.UserDetails.UserID,
		"GL_USERNAME="+payload.UserDetails.Username,
		"GL_PROTOCOL="+payload.UserDetails.Protocol,
	), nil
```

**File:** internal/gitaly/service/ssh/receive_pack_test.go (L207-213)
```go
	lHead, rHead, err := setupRepoAndPush(t, ctx, cfg, &gitalypb.SSHReceivePackRequest{
		Repository:   remoteRepo,
		GlId:         "123",
		GlUsername:   "user",
		GlRepository: remoteRepo.GetGlRepository(),
	})
	require.NoError(t, err)
```

**File:** internal/gitaly/service/operations/user_create_branch_test.go (L308-324)
```go
	request := &gitalypb.UserCreateBranchRequest{
		Repository: repo,
		BranchName: []byte("new-branch"),
		StartPoint: []byte(commitID),
		User:       gittest.TestUser,
	}

	hookContent := []byte("#!/bin/sh\necho GL_ID=$GL_ID\nexit 1")

	expectedObject := "GL_ID=" + gittest.TestUser.GetGlId()

	for _, hookName := range gitlabPreHooks {
		gittest.WriteCustomHook(t, repoPath, hookName, hookContent)

		_, err := client.UserCreateBranch(ctx, request)

		testhelper.RequireGrpcError(t, structerr.NewPermissionDenied("creation denied by custom hooks: running pre-receive hooks: GL_ID=user-123\n").WithDetail(
```
