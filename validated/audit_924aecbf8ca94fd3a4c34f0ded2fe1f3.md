Based on my investigation, the strongest and most legitimate analog to the reported bug class is in `PackObjectsHookWithSidechannel`, where the explicit `Repository` argument is never cross-validated against the repository embedded in the opaque, client-controlled `EnvironmentVariables` (`GITALY_HOOKS_PAYLOAD`).

### Title
Repository field in PackObjectsHookWithSidechannel is not cross-validated against the embedded hooks payload's transaction/repository, enabling cross-repository object access via mismatched arguments - ([File: internal/gitaly/service/hook/pack_objects.go])

### Summary
`PackObjectsHookWithSidechannel` is analogous to the reported bug class: it accepts an explicit, individually-validated `Repository` argument alongside an opaque `bytes`-like payload (`EnvironmentVariables`, containing the base64/JSON-encoded `GITALY_HOOKS_PAYLOAD`) that is generated off-process (by `gitaly-hooks`, itself invoked as a Git subprocess). The handler validates `req.GetRepository()` in isolation via `s.locator.ValidateRepository` [1](#0-0)  and separately decodes `hookPayload` from `req.GetEnvironmentVariables()` [2](#0-1) , but never checks that `hookPayload.Repo` and `hookPayload.TransactionID` actually correspond to `req.Repository`.

### Finding Description
The RPC decodes a transaction from `hookPayload.TransactionID` and re-injects it into the context used to run `git-pack-objects` against `req.Repository`: [3](#0-2) . This mirrors exactly the reported bug pattern: an explicit, "primary" argument (`Repository`) is used for one purpose (authorization/target selection) while a separate opaque payload (`EnvironmentVariables` → `HooksPayload`) supplies data (`TransactionID`, `Repo`) that is trusted for a different, more privileged purpose (associating the request with an in-flight write transaction and its quarantine/object visibility state) without checking that the two agree.

The `HooksPayload` itself is base64+JSON encoded and roundtripped without any binding to the specific `Repository` supplied in the same request: `HooksPayloadFromEnv` simply decodes whatever is present in the environment variable list [4](#0-3) , and `EnvironmentVariables` in `PackObjectsHookWithSidechannelRequest` is attacker/process reachable content passed through `os.Environ()` by the `gitaly-hooks git` subcommand [5](#0-4) . Since `gitaly-hooks` is invoked as a `uploadpack.packObjectsHook` subprocess of `git upload-pack`/`git-pack-objects`, it inherits the full process environment set by whatever invoked Git, which is a strictly less trusted path than a direct RPC from Gitaly's internal orchestration code.

The core weakness: `s.txRegistry.Get(hookPayload.TransactionID)` resolves an arbitrary transaction handle from the caller-supplied payload and applies it to the context under which `req.Repository`'s pack-objects run and cache-key computation happen — `computeCacheKey` also uses `storage.ExtractTransaction(ctx)` to substitute the "original repository" for cache-key purposes [6](#0-5) . If the `TransactionID`/`Repo` in the decoded payload do not match `req.Repository` (e.g., because of a stale, forged, or mismatched payload from a different repository's `pack-objects` invocation), the transaction context (with its own quarantine/object-visibility state) would be applied while operating on `req.Repository`, and cache keys could be computed/mismatched across repositories that share the same transaction id space, since no equality check exists between `hookPayload.Repo`/its relative path and `req.GetRepository()`.

### Impact Explanation
If a `TransactionID` or `Repo` value in the hooks payload can be made to diverge from the `Repository` field of the same RPC call — whether through the pack-objects-cache codepath being triggered with a stale/forged environment, or through code that constructs `PackObjectsHookWithSidechannelRequest` from data not perfectly synchronized with the payload's transaction — the pack-objects invocation could run under the object-visibility/quarantine assumptions of a different transaction than the one governing the target repository. This risks exposing quarantined objects across a transaction boundary, or generating a cache key that references a transaction not actually tied to the target repository, silently defeating the transaction/quarantine isolation the object-quarantine design document describes as security-relevant boundary enforcement (see the quarantine mechanics for merging/isolating objects during a push) [7](#0-6) .

### Likelihood Explanation
The likelihood is limited by the fact that `TransactionID` and `Repo` are today always constructed consistently by Gitaly's own `WithPackObjectsHookEnv`/`configureHooks` code paths, which always pass the *same* repository object into both the RPC's `Repository` field and the `HooksPayload.Repo` field [8](#0-7) . There is no currently known reachable path from an ordinary user's fetch/push where these two values are forced to disagree, since the payload is generated by Gitaly itself, not supplied end-to-end by the client. This is a defense-in-depth gap rather than a demonstrated exploitable path today — the missing validation is a "should never happen but is unchecked" gap analogous to trusting the off-chain-generated `swapExtraData` in the reported issue, but I could not find a concrete way for an untrusted actor to independently control `EnvironmentVariables` separately from `Repository` within Gitaly's own trusted request-construction call chain.

### Recommendation
Add an explicit check in `PackObjectsHookWithSidechannel` that the repository embedded in the decoded `HooksPayload` (`hookPayload.Repo`, comparing storage name and relative path) matches `req.GetRepository()` before using `hookPayload.TransactionID` to fetch and apply a transaction context. This closes the "explicit argument vs. opaque payload" trust gap analogous to the 1inch `swapExtraData` issue, ensuring the transaction/quarantine context applied to a pack-objects run is always the one that was actually established for that specific repository, and not merely whatever the environment-derived payload happens to claim.

### Proof of Concept
Not reproducible as an exploit given current code paths: both `req.Repository` and the `HooksPayload.Repo`/`TransactionID` in `EnvironmentVariables` are populated from the same values by Gitaly's own `WithPackObjectsHookEnv` before invoking `git-pack-objects`, so no divergence between them is currently reachable from an external, unprivileged input in the reviewed code. A concrete PoC would require identifying a call site where these two values can be independently influenced (e.g., a caching/retry path that reuses a stale `EnvironmentVariables` blob against a newly-supplied `Repository`), which I was unable to locate within the available index.

### Citations

**File:** internal/gitaly/service/hook/pack_objects.go (L117-145)
```go
func (s *server) computeCacheKey(ctx context.Context, req *gitalypb.PackObjectsHookWithSidechannelRequest, stdinReader io.Reader) (string, io.ReadCloser, error) {
	cacheHash := sha256.New()

	repository := req.GetRepository()
	if tx := storage.ExtractTransaction(ctx); tx != nil {
		// The cache uses the requests as the keys. As the request's repository in the RPC handler has been rewritten
		// to point to the transaction's repository, the handler sees each request as different even if they point to
		// the same repository. Restore the original request to ensure identical requests get the same key.
		repository = tx.OriginalRepository(req.GetRepository())
	}

	cacheKeyPrefix, err := protojson.Marshal(&gitalypb.PackObjectsHookWithSidechannelRequest{
		Repository:  repository,
		Args:        req.GetArgs(),
		GitProtocol: req.GetGitProtocol(),
	})
	if err != nil {
		return "", nil, err
	}
	if _, err := cacheHash.Write(cacheKeyPrefix); err != nil {
		return "", nil, err
	}
	stdin, err := bufferStdin(stdinReader, cacheHash)
	if err != nil {
		return "", nil, err
	}
	cacheKey := hex.EncodeToString(cacheHash.Sum(nil))
	return cacheKey, stdin, nil
}
```

**File:** internal/gitaly/service/hook/pack_objects.go (L369-372)
```go
func (s *server) PackObjectsHookWithSidechannel(ctx context.Context, req *gitalypb.PackObjectsHookWithSidechannelRequest) (*gitalypb.PackObjectsHookWithSidechannelResponse, error) {
	if err := s.locator.ValidateRepository(ctx, req.GetRepository()); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}
```

**File:** internal/gitaly/service/hook/pack_objects.go (L388-391)
```go
	hookPayload, err := gitcmd.HooksPayloadFromEnv(req.GetEnvironmentVariables())
	if err != nil {
		return nil, fmt.Errorf("hook payload from env: %w", err)
	}
```

**File:** internal/gitaly/service/hook/pack_objects.go (L393-404)
```go
	if hookPayload.TransactionID > 0 {
		// If we're running with transactions, we need to restore the transaction into
		// the context so the helpers we use everywhere work in this context as well.
		// This handler is invoked through git and gitaly-hooks which means we're not
		// using the same context as in the actual RPC handler that the led to this call.
		tx, err := s.txRegistry.Get(hookPayload.TransactionID)
		if err != nil {
			return nil, fmt.Errorf("get transaction: %w", err)
		}

		ctx = storage.ContextWithTransaction(ctx, tx)
	}
```

**File:** internal/git/gitcmd/hooks_payload.go (L197-233)
```go
// HooksPayloadFromEnv extracts the HooksPayload from the given environment
// variables. If no HooksPayload exists, it returns a ErrPayloadNotFound
// error.
func HooksPayloadFromEnv(envs []string) (HooksPayload, error) {
	encoded, ok := lookupEnv(envs, EnvHooksPayload)
	if !ok {
		return HooksPayload{}, ErrPayloadNotFound
	}

	decoded, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return HooksPayload{}, err
	}

	var jsonPayload jsonHooksPayload
	if err := json.Unmarshal(decoded, &jsonPayload); err != nil {
		return HooksPayload{}, err
	}

	var repo gitalypb.Repository
	err = protojson.Unmarshal([]byte(jsonPayload.Repo), &repo)
	if err != nil {
		return HooksPayload{}, err
	}

	payload := jsonPayload.HooksPayload
	payload.Repo = &repo

	// If no RequestedHooks are passed down to us, then we need to assume
	// that the caller of this hook isn't aware of this field and thus just
	// pretend that he wants to execute all hooks.
	if payload.RequestedHooks == 0 {
		payload.RequestedHooks = AllHooks
	}

	return payload, nil
}
```

**File:** cmd/gitaly-hooks/hooks.go (L465-476)
```go
	if _, err := hookClient.PackObjectsHookWithSidechannel(
		ctx,
		&gitalypb.PackObjectsHookWithSidechannelRequest{
			Repository:           payload.Repo,
			EnvironmentVariables: os.Environ(),
			Args:                 args,
			GlId:                 glID,
			GlUsername:           glUsername,
			GitProtocol:          gitProtocol,
			RemoteIp:             remoteIP,
		},
	); err != nil {
```

**File:** doc/object_quarantine.md (L60-79)
```markdown
#### Putting it all together

1. `git receive-pack` receives a push
1. `git receive-pack` [creates a quarantine directory `objects/incoming-$RANDOM`](https://gitlab.com/gitlab-org/git/-/blob/v2.24.0/builtin/receive-pack.c#L1715)
1. `git receive-pack` [configures the unpack process](https://gitlab.com/gitlab-org/git/-/blob/v2.24.0/builtin/receive-pack.c#L1721) to write objects into the quarantine directory
1. `git receive-pack` unpacks the objects into the quarantine directory
1. `git receive-pack` [runs the `pre-receive` hook](https://gitlab.com/gitlab-org/git/-/blob/v2.24.0/builtin/receive-pack.c#L1498) with special `GIT_OBJECT_DIRECTORY` and `GIT_ALTERNATE_OBJECT_DIRECTORIES` environment variables that add the quarantine directory to the search path
1. If the `pre-receive` hook rejects the push, `git receive-pack` removes the quarantine directory and its contents. The push is aborted.
1. If the `pre-receive` hook passes, `git receive-pack` [merges the quarantine directory into the main object directory](https://gitlab.com/gitlab-org/git/-/blob/v2.24.0/builtin/receive-pack.c#L1510).
1. `git receive-pack` enters the ref update transaction

Note that by the time the `update` hook runs, the quarantine directory
has already been merged into the main object directory so it no longer
matters. The same goes for the `post-receive` hook which runs even
later.

Because `pre-receive` has the special quarantine configuration data in
environment variables, any `git` process spawned by `pre-receive` will
inherit the quarantine config and will be able to see the objects that
are being pushed.
```

**File:** internal/git/gitcmd/hooks_options.go (L57-94)
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
}
```
