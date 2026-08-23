## Title
HookService RPCs (PreReceiveHook/UpdateHook/ReferenceTransactionHook/PostReceiveHook) are reachable on Gitaly's external gRPC socket even though they are only meant to be invoked internally by `gitaly-hooks`, allowing an authenticated ordinary client to directly drive hook/transaction-vote logic without the actual git operation it is supposed to gate — ([File: internal/gitaly/service/hook/reference_transaction.go])

### Summary
The Astaria report shows that a security check (deadline validation) was correctly implemented in the "front door" (`AstariaRouter`) but omitted in a lower-level function (`VaultImplementation._validateCommitment`) that performs the same operation and is directly reachable. The Gitaly analog is structural rather than a missing field check: the `HookService` RPCs — `PreReceiveHook`, `UpdateHook`, `ReferenceTransactionHook`, and `PostReceiveHook` — are designed to be invoked only internally, by the `gitaly-hooks` helper binary through Gitaly's internal socket, as part of a real `git-receive-pack`/`OperationService` write flow. However, they are registered and reachable on the external gRPC listener as well, so any client holding a valid Gitaly auth token can invoke these RPCs directly, out of band from the actual git operation they are meant to gate.

### Finding Description
Gitaly's hook execution model requires that `PreReceiveHook`, `UpdateHook`, and `ReferenceTransactionHook` only ever run as a side effect of an actual `git-receive-pack` invocation or an `OperationService` RPC driving `UpdaterWithHooks.UpdateReference`, which invokes the hooks in lock-step with the underlying object quarantine migration and ref update [1](#0-0) . The `ReferenceTransactionHook` in particular exists to let Gitaly nodes vote on reference updates for Praefect/WAL consensus, using the (attacker-controllable) `TransactionID` extracted from the hooks payload environment variables and casts votes based on caller-supplied stdin content [2](#0-1) .

These RPCs are exposed via the `HookService` gRPC server, whose handler for `ReferenceTransactionHook` only validates the repository and hook state, then executes voting logic exactly as if the call had originated from a legitimate hook invocation [3](#0-2) . The codebase itself documents that `HookService` is intended to be reached only via the internal socket, and flags that serving it on the external socket is a known, unresolved gap:

> "we should stop serving HookService on the external socket given it is a service intended only to be used internally by Gitaly for hook callbacks." [4](#0-3) 

The server startup code registers the *same* set of services (`setup.RegisterAll`, which includes `HookService`) on all listeners — the external Unix/TCP/TLS socket as well as the internal socket meant only for `gitaly-hooks` callbacks — with only the transactional middleware differing between "external" and "internal" wiring [5](#0-4) [6](#0-5) .

This mirrors the Astaria bug class: the "safe" invocation path (`UpdaterWithHooks.UpdateReference`/`git-receive-pack`) performs hook invocation strictly synchronized with the object migration and reference update it is meant to gate, but the "duplicate" lower-level entry point (calling `HookService` RPCs directly) is reachable by any client with a valid token and does not require that the underlying git operation (real quarantine migration, real ref update, matching `git-receive-pack` process) ever actually take place.

### Impact Explanation
An authenticated Gitaly client (any client holding a valid Gitaly bearer token — the same privilege level required for ordinary RPCs served on the external socket) can call `ReferenceTransactionHook` directly with a forged `TransactionID` and crafted stdin, casting Praefect/WAL votes for an in-flight transaction it does not otherwise participate in, independent of whether a corresponding legitimate reference update occurred. Because vote correctness underpins Gitaly's HA quorum and WAL consistency guarantees (`doc/hooks.md` describes voting as the mechanism ensuring all nodes reach the same state), this can be used to desynchronize or disrupt in-progress transactions of other repositories/pushes (a DoS/consistency-bypass on the RPC handler), and to invoke `PreReceiveHook`/`PostReceiveHook` reference-counter and custom-hook logic without any real push taking place, decoupling GitLab's push-active reference counting from actual push activity.

### Likelihood Explanation
Likelihood is bounded by needing a valid Gitaly auth token, which is the same bar as any other externally-reachable RPC (Gitaly enforces the same token check uniformly across listeners) [7](#0-6) . Any client that can already reach the external Gitaly listener with valid credentials (e.g. Rails/Workhorse token, or any tenant able to call the API) can exploit this without needing elevated privileges or a leaked/forged token beyond what a normal caller already has, and the codebase explicitly acknowledges the design gap remains open.

### Recommendation
Stop registering `HookService` on the external listener (as flagged by the code comment referencing gitlab-org/gitaly#3746), restricting it to the internal socket used exclusively by `gitaly-hooks`. If backward compatibility requires it to remain briefly reachable externally, add an explicit guard (similar to the `SignalPostReceiveReady` backchannel check) requiring proof that the call originates from a legitimate, currently in-flight `git-receive-pack`/`UpdaterWithHooks` invocation (e.g., validating the hooks payload against server-tracked in-flight operation state) before acting on `PreReceiveHook`/`UpdateHook`/`ReferenceTransactionHook`/`PostReceiveHook`.

### Proof of Concept
1. Obtain a valid Gitaly auth token (same one used by any legitimate external caller, e.g. Workhorse/Rails).
2. Construct a `ReferenceTransactionHookRequest` with a `Repository` and a hooks-payload environment variable containing an arbitrary/observed `TransactionID` and `ReferenceTransactionPrepared`/`Committed` state, plus crafted stdin listing reference updates.
3. Call `HookService.ReferenceTransactionHook` directly against Gitaly's external socket (no need to go through `git-receive-pack` or `OperationService`) using `grpcurl`, as documented for local invocation [8](#0-7) .
4. Observe that Gitaly executes vote-casting logic against the referenced transaction exactly as the handler in `internal/gitaly/service/hook/reference_transaction.go` performs it, without any check that a genuine `git-receive-pack` invocation or `UpdaterWithHooks.UpdateReference` call triggered it [9](#0-8) .

### Citations

**File:** internal/gitaly/hook/updateref/update_with_hooks.go (L167-226)
```go
func (u *UpdaterWithHooks) UpdateReference(
	ctx context.Context,
	repoProto *gitalypb.Repository,
	user *gitalypb.User,
	quarantineDir *quarantine.Dir,
	reference git.ReferenceName,
	newrev, oldrev git.ObjectID,
	pushOptions ...string,
) error {
	var transaction *txinfo.Transaction
	if tx, err := txinfo.TransactionFromContext(ctx); err == nil {
		transaction = &tx
	} else if !errors.Is(err, txinfo.ErrTransactionNotFound) {
		return fmt.Errorf("getting transaction: %w", err)
	}

	repo := u.localrepo(repoProto)

	objectHash, err := repo.ObjectHash(ctx)
	if err != nil {
		return fmt.Errorf("detecting object hash: %w", err)
	}

	if reference == "" {
		return fmt.Errorf("reference cannot be empty")
	}
	if err := objectHash.ValidateHex(oldrev.String()); err != nil {
		return fmt.Errorf("validating old value: %w", err)
	}
	if err := objectHash.ValidateHex(newrev.String()); err != nil {
		return fmt.Errorf("validating new value: %w", err)
	}

	changes := fmt.Sprintf("%s %s %s\n", oldrev, newrev, reference)

	receiveHooksPayload := gitcmd.UserDetails{
		UserID:   user.GetGlId(),
		Username: user.GetGlUsername(),
		Protocol: "web",
	}

	// In case there's no quarantine directory, we simply take the normal unquarantined
	// repository as input for the hooks payload. Otherwise, we'll take the quarantined
	// repository, which carries information about the quarantined object directory. This is
	// then subsequently passed to Rails, which can use the quarantine directory to more
	// efficiently query which objects are new.
	quarantinedRepo := repoProto
	if quarantineDir != nil {
		quarantinedRepo = quarantineDir.QuarantinedRepo()
	}

	hooksPayload, err := gitcmd.NewHooksPayload(ctx, u.cfg, quarantinedRepo, objectHash, transaction, &receiveHooksPayload, gitcmd.ReceivePackHooks, featureflag.FromContext(ctx), storage.ExtractTransactionID(ctx)).Env()
	if err != nil {
		return fmt.Errorf("constructing hooks payload: %w", err)
	}

	var stdout, stderr bytes.Buffer
	if err := u.hookManager.PreReceiveHook(ctx, quarantinedRepo, pushOptions, []string{hooksPayload}, strings.NewReader(changes), &stdout, &stderr); err != nil {
		return fmt.Errorf("running pre-receive hooks: %w", wrapHookError(err, gitcmd.PreReceiveHook, stdout.String(), stderr.String()))
	}
```

**File:** internal/gitaly/hook/referencetransaction.go (L29-53)
```go
func (m *GitLabHookManager) ReferenceTransactionHook(ctx context.Context, state ReferenceTransactionState, env []string, stdin io.Reader) error {
	payload, err := gitcmd.HooksPayloadFromEnv(env)
	if err != nil {
		return fmt.Errorf("extracting hooks payload: %w", err)
	}

	objectHash, err := git.ObjectHashByFormat(payload.ObjectFormat)
	if err != nil {
		return fmt.Errorf("looking up object hash: %w", err)
	}

	changes, err := io.ReadAll(stdin)
	if err != nil {
		return fmt.Errorf("reading stdin from request: %w", err)
	}

	var tx storage.Transaction
	if payload.TransactionID > 0 {
		tx, err = m.txRegistry.Get(payload.TransactionID)
		if err != nil {
			return fmt.Errorf("get transaction: %w", err)
		}
	}

	var phase voting.Phase
```

**File:** internal/gitaly/service/hook/reference_transaction.go (L15-53)
```go
func validateReferenceTransactionHookRequest(ctx context.Context, locator storage.Locator, in *gitalypb.ReferenceTransactionHookRequest) error {
	return locator.ValidateRepository(ctx, in.GetRepository())
}

func (s *server) ReferenceTransactionHook(stream gitalypb.HookService_ReferenceTransactionHookServer) error {
	request, err := stream.Recv()
	if err != nil {
		return structerr.NewInternal("receiving first request: %w", err)
	}

	if err := validateReferenceTransactionHookRequest(stream.Context(), s.locator, request); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	var state hook.ReferenceTransactionState
	switch request.GetState() {
	case gitalypb.ReferenceTransactionHookRequest_PREPARING:
		state = hook.ReferenceTransactionPreparing
	case gitalypb.ReferenceTransactionHookRequest_PREPARED:
		state = hook.ReferenceTransactionPrepared
	case gitalypb.ReferenceTransactionHookRequest_COMMITTED:
		state = hook.ReferenceTransactionCommitted
	case gitalypb.ReferenceTransactionHookRequest_ABORTED:
		state = hook.ReferenceTransactionAborted
	default:
		return structerr.NewInvalidArgument("invalid hook state")
	}

	stdin := streamio.NewReader(func() ([]byte, error) {
		req, err := stream.Recv()
		return req.GetStdin(), err
	})

	if err := s.manager.ReferenceTransactionHook(
		stream.Context(),
		state,
		request.GetEnvironmentVariables(),
		stdin,
	); err != nil {
```

**File:** internal/gitaly/service/hook/pre_receive_test.go (L186-192)
```go
	// they come through the external API, not when it comes through the internal socket used by hooks to call into Gitaly.
	// This test setup however is calling the HookService through the external API.
	//
	// For now, include the header so the test runs. In the longer term, we should stop serving HookService on the external
	// socket given it is a service intended only to be used internally by Gitaly for hook callbacks.
	//
	// Related issue: https://gitlab.com/gitlab-org/gitaly/-/issues/3746
```

**File:** internal/cli/gitaly/serve.go (L768-823)
```go
	for _, c := range []starter.Config{
		{Name: starter.Unix, Addr: cfg.SocketPath, HandoverOnUpgrade: true},
		{Name: starter.Unix, Addr: cfg.InternalSocketPath(), HandoverOnUpgrade: false},
		{Name: starter.TCP, Addr: cfg.ListenAddr, HandoverOnUpgrade: true},
		{Name: starter.TLS, Addr: cfg.TLSListenAddr, HandoverOnUpgrade: true},
	} {
		if c.Addr == "" {
			continue
		}

		var srv *grpc.Server
		opts := []server.Option{
			server.WithUnaryInterceptor(bm.UnaryInterceptor()),
			server.WithStreamInterceptor(bm.StreamInterceptor()),
		}

		if c.HandoverOnUpgrade {
			srv, err = gitalyServerFactory.CreateExternal(c.IsSecure(), opts...)
			if err != nil {
				return fmt.Errorf("create external gRPC server: %w", err)
			}
		} else {
			srv, err = gitalyServerFactory.CreateInternal(opts...)
			if err != nil {
				return fmt.Errorf("create internal gRPC server: %w", err)
			}
		}

		setup.RegisterAll(srv, &service.Dependencies{
			Logger:                 logger,
			Cfg:                    cfg,
			GitalyHookManager:      hookManager,
			TransactionManager:     transactionManager,
			StorageLocator:         locator,
			ClientPool:             conns,
			GitCmdFactory:          gitCmdFactory,
			CatfileCache:           catfileCache,
			DiskCache:              diskCache,
			PackObjectsCache:       packObjectStreamCache,
			PackObjectsLimiter:     packObjectsLimiter,
			RepositoryCounter:      repoCounter,
			UpdaterWithHooks:       updaterWithHooks,
			Node:                   node,
			TransactionRegistry:    txRegistry,
			HousekeepingManager:    housekeepingManager,
			BackupSink:             backupSink,
			BackupLocator:          backupLocator,
			LocalRepositoryFactory: localrepoFactory,
			BundleURIManager:       bundleURIManager,
			MigrationStateManager:  migration.NewStateManager(&migrations),
			ArchiveCache:           archiveStreamCache,
			PoolMetadataStore:      poolMetadataStore,
			GitlabClient:           gitlabClient,
			ObjectPoolStateManager: objectPoolStateManager,
			PostReceiveRegistry:    postReceiveRegistry,
		})
```

**File:** internal/gitaly/server/server.go (L192-205)
```go
	// Only requests coming through the external API need to be ran transactionalized. Only the HookService calls
	// should arrive through the internal socket. Requests coming from there would already be running in a
	// transaction as the external request that led to the internal socket call would have been transactionalized
	// already.
	if external {
		// When transactions are enabled, it overrides the relative path of the repository to point to the
		// snapshot directory. Which would make the housekeeping related caches unusable. We should use the
		// original relative path when transaction is enabled, but when request is routed through hook back
		// to gitaly, the original repository is not in the context anymore. Therefore, housekeeping should
		// not be configured in the internal gRPC server used for hooks
		if s.housekeepingMiddleware != nil {
			streamServerInterceptors = append(streamServerInterceptors, s.housekeepingMiddleware.StreamServerInterceptor())
			unaryServerInterceptors = append(unaryServerInterceptors, s.housekeepingMiddleware.UnaryServerInterceptor())
		}
```

**File:** internal/gitaly/server/auth/auth.go (L56-80)
```go
func checkFunc(conf gitalycfgauth.Config) func(ctx context.Context) (context.Context, error) {
	return func(ctx context.Context) (context.Context, error) {
		if len(conf.GetToken()) == 0 {
			countStatus("server disabled authentication", conf.Transitioning).Inc()
			return ctx, nil
		}

		err := gitalyauth.CheckToken(ctx, conf.GetToken(), time.Now())
		switch status.Code(err) {
		case codes.OK:
			countStatus(okLabel(conf.Transitioning), conf.Transitioning).Inc()
		case codes.Unauthenticated:
			countStatus("unauthenticated", conf.Transitioning).Inc()
		case codes.PermissionDenied:
			countStatus("denied", conf.Transitioning).Inc()
		default:
			countStatus("invalid", conf.Transitioning).Inc()
		}

		if conf.Transitioning {
			err = nil
		}

		return ctx, err
	}
```

**File:** doc/invoking_grpc_endpoints_locally.md (L33-51)
```markdown
## 3. Discover the method you need

Gitaly and Praefect both register the gRPC server reflection service, so a protoset is optional.
Against a running server, discovery works without one:

```shell
grpcurl -plaintext -H "authorization: Bearer ${GITALY_TOKEN}" unix://<socket> list
grpcurl -plaintext -H "authorization: Bearer ${GITALY_TOKEN}" unix://<socket> list gitaly.RefService
grpcurl -plaintext -H "authorization: Bearer ${GITALY_TOKEN}" unix://<socket> describe gitaly.FindDefaultBranchNameRequest
```

When you know roughly what a method is called but not which service holds it, list the methods of
every service and search them. There are only a couple of dozen services, so this is quick:

```shell
for service in $(grpcurl -plaintext -H "authorization: Bearer ${GITALY_TOKEN}" unix://<socket> list); do
  grpcurl -plaintext -H "authorization: Bearer ${GITALY_TOKEN}" unix://<socket> list "${service}" 2>/dev/null
done | grep -i <term>
```
```
