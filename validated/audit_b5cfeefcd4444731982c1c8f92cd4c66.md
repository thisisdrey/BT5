### Title
`RefService.UpdateReferences` bypasses pre-receive/update/post-receive hooks that `OperationService` RPCs enforce for the same state change - ([File: internal/gitaly/service/ref/update_references.go])

### Summary
Gitaly exposes two independent code paths that can produce the exact same observable effect — updating, creating, or deleting a Git reference — but only one of them drives Gitaly's custom-hook/access-check machinery. `OperationService` RPCs (e.g. `UserUpdateBranch`, `UserDeleteBranch`) go through `UpdaterWithHooks.UpdateReference`, which explicitly invokes `PreReceiveHook`, `UpdateHook`, all phases of `ReferenceTransactionHook`, and `PostReceiveHook`. `RefService.UpdateReferences`, however, updates the very same references via a bare `updateref.Updater` that only wires up the reference-transaction hook used for replication voting — it never invokes pre-receive/update/post-receive hooks at all.

### Finding Description
`UpdaterWithHooks.UpdateReference` is the "full" reference-update path used by mutator RPCs in `OperationService`. It runs the complete Git hook chain in order: [1](#0-0) [2](#0-1) 

This chain is what performs the equivalent of the `Minted`/`Burned` events in the referenced report: it lets Rails validate the change via `/internal/allowed` in `pre-receive`, executes any administrator-installed custom hooks in `update`, casts reference-transaction votes for replica consistency, and finally runs `post-receive`.

`RefService.UpdateReferences`, in contrast, builds a raw `updateref.Updater` directly: [3](#0-2) 

`updateref.New` only ever configures the reference-transaction hook (`gitcmd.WithRefTxHook`) for voting, or disables hooks entirely with `WithDisabledTransactions`; it never sets up `ReceivePackHooks` (pre-receive/update/post-receive): [4](#0-3) 

As a result, any caller invoking `UpdateReferences` can create, force-update, or delete arbitrary references (branches, tags, `HEAD`, etc.) in a repository while completely skipping:
- Rails' `/internal/allowed` authorization/protected-branch check (normally invoked from `pre-receive`).
- Any administrator-installed custom `pre-receive`/`update`/`post-receive` hooks.
- The per-repository push reference counter maintained in pre-receive/post-receive (documented in `doc/hooks.md`).

The doc for `RefService`'s hook semantics explicitly documents this as the expected model for OperationService: "Most RPCs in the `OperationService` that write objects into the repository manually invoke these hooks using the `updateref.UpdaterWithHooks` structure," implying other reference-writing RPCs are not architected to do this. [5](#0-4) 

### Impact Explanation
This is directly analogous to the referenced [M02] finding: two functions achieve the same underlying state transition, but only one of them triggers the side effects (event emission / access checks) relied upon by downstream consumers to enforce policy and maintain accounting/audit consistency. Here, `UpdateReferences` lets a caller with repository RPC access silently mutate protected refs (e.g. `refs/heads/main`, tags, `HEAD`) without the access-control gate or audit hooks that GitLab relies on for protected-branch enforcement, push rules, and custom hook execution. This is a hook-bypass vulnerability affecting reference-update integrity for any client able to call this RPC.

### Likelihood Explanation
`UpdateReferences` is a standard mutator RPC on `RefService`, reachable the same way any Gitaly gRPC RPC is reachable by a client holding repository access (e.g., via a compromised/normal GitLab Rails-authenticated session or any other consumer permitted to call Gitaly directly). No malicious peer, MITM, or leaked-token scenario is required — it is simply an alternate, less-restricted reference-mutation entry point that any ordinary caller with RPC access to the repository can use instead of the `OperationService` RPCs.

### Recommendation
Either:
1. Route `RefService.UpdateReferences` through `UpdaterWithHooks` (or an equivalent that invokes `PreReceiveHook`/`UpdateHook`/`PostReceiveHook`) so that ref writes always pass through the same access-check and custom-hook pipeline as `OperationService`, or
2. Explicitly document and restrict `UpdateReferences` as a privileged/internal-only RPC not intended for use where policy enforcement (protected branches, custom hooks) must apply, and ensure Rails/consumers gate its usage accordingly.

### Proof of Concept
1. Obtain access sufficient to call Gitaly's `RefService` (the same access level used to call other repository RPCs).
2. Call `UpdateReferences` with an update targeting a protected reference, e.g. force-updating `refs/heads/main` to an arbitrary commit OID that would normally be rejected by a protected-branch/pre-receive check:
```
stream, _ := refClient.UpdateReferences(ctx)
stream.Send(&gitalypb.UpdateReferencesRequest{
    Repository: repo,
    Updates: []*gitalypb.UpdateReferencesRequest_Update{
        {Reference: []byte("refs/heads/main"), NewObjectId: []byte(attackerCommitOID)},
    },
})
stream.CloseAndRecv()
``` [6](#0-5) 
3. Observe that the update succeeds without any `pre-receive`/`update`/`post-receive` hook execution and without a Rails `/internal/allowed` check, whereas the equivalent change via `UserUpdateBranch` would have invoked those checks through `UpdaterWithHooks.UpdateReference`. [7](#0-6)

### Citations

**File:** internal/gitaly/hook/updateref/update_with_hooks.go (L167-248)
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

	// Now that Rails has told us that the change is okay via the pre-receive hook, we can
	// migrate any potentially quarantined objects into the main repository. This must happen
	// before we start updating the refs because git-update-ref(1) will verify that it got all
	// referenced objects available.
	if quarantineDir != nil {
		if err := quarantineDir.Migrate(ctx); err != nil {
			return fmt.Errorf("migrating quarantined objects: %w", err)
		}

		// We only need to update the hooks payload to the unquarantined repo in case we
		// had a quarantine environment. Otherwise, the initial hooks payload is for the
		// real repository anyway.
		hooksPayload, err = gitcmd.NewHooksPayload(ctx, u.cfg, repoProto, objectHash, transaction, &receiveHooksPayload, gitcmd.ReceivePackHooks, featureflag.FromContext(ctx), storage.ExtractTransactionID(ctx)).Env()
		if err != nil {
			return fmt.Errorf("constructing quarantined hooks payload: %w", err)
		}
	}

	if err := u.hookManager.UpdateHook(ctx, quarantinedRepo, reference.String(), oldrev.String(), newrev.String(), []string{hooksPayload}, &stdout, &stderr); err != nil {
		return fmt.Errorf("running update hooks: %w", wrapHookError(err, gitcmd.UpdateHook, stdout.String(), stderr.String()))
	}
```

**File:** internal/gitaly/hook/updateref/update_with_hooks.go (L277-309)
```go
	if err := u.hookManager.ReferenceTransactionHook(ctx, hook.ReferenceTransactionPreparing, []string{hooksPayload}, strings.NewReader(changes)); err != nil {
		return fmt.Errorf("executing pre-locked reference-transaction hook: %w", err)
	}

	// We need to lock the reference before executing the reference-transaction hook such that
	// there cannot be any concurrent modification.
	if err := updater.Prepare(); err != nil {
		return Error{
			Reference: reference,
			OldOID:    oldrev,
			NewOID:    newrev,
			Cause:     err,
		}
	}

	if err := u.hookManager.ReferenceTransactionHook(ctx, hook.ReferenceTransactionPrepared, []string{hooksPayload}, strings.NewReader(changes)); err != nil {
		return fmt.Errorf("executing preparatory reference-transaction hook: %w", err)
	}

	if err := updater.Commit(); err != nil {
		return Error{
			Reference: reference,
			OldOID:    oldrev,
			NewOID:    newrev,
			Cause:     err,
		}
	}

	if err := u.hookManager.ReferenceTransactionHook(ctx, hook.ReferenceTransactionCommitted, []string{hooksPayload}, strings.NewReader(changes)); err != nil {
		return fmt.Errorf("executing committing reference-transaction hook: %w", err)
	}

	if err := u.hookManager.PostReceiveHook(ctx, repoProto, pushOptions, []string{hooksPayload}, strings.NewReader(changes), &stdout, &stderr); err != nil {
```

**File:** internal/gitaly/service/ref/update_references.go (L36-88)
```go
	request.Repository = nil

	updater, err := updateref.New(ctx, repo)
	if err != nil {
		return fmt.Errorf("creating updater: %w", err)
	}

	if err := updater.Start(); err != nil {
		return fmt.Errorf("starting updater: %w", err)
	}

	for {
		// Only the first request may have its repository set.
		if request.GetRepository() != nil {
			return structerr.NewInvalidArgument("repository set in subsequent request")
		}

		if len(request.GetUpdates()) == 0 {
			return structerr.NewInvalidArgument("no updates specified")
		}

		for _, update := range request.GetUpdates() {
			reference := string(update.GetReference())
			if err := git.ValidateReference(reference); err != nil {
				return structerr.NewInvalidArgument("validating reference: %w", err).
					WithMetadata("reference", reference).
					WithDetail(&gitalypb.UpdateReferencesError{
						Error: &gitalypb.UpdateReferencesError_InvalidFormat{
							InvalidFormat: &gitalypb.InvalidRefFormatError{
								Refs: [][]byte{[]byte(reference)},
							},
						},
					})
			}

			// The old object ID may be empty, in which case we don't care about the current value of the
			// reference but instead do a force update of it.
			oldObjectID := string(update.GetOldObjectId())
			if len(oldObjectID) > 0 {
				if err := objectHash.ValidateHex(oldObjectID); err != nil {
					return structerr.NewInvalidArgument("validating old object ID: %w", err).WithMetadata("old_object_id", oldObjectID)
				}
			}

			newObjectID := string(update.GetNewObjectId())
			if err := objectHash.ValidateHex(newObjectID); err != nil {
				return structerr.NewInvalidArgument("validating new object ID: %w", err).WithMetadata("new_object_id", newObjectID)
			}

			if err := updater.Update(git.ReferenceName(reference), git.ObjectID(newObjectID), git.ObjectID(oldObjectID)); err != nil {
				return structerr.NewInvalidArgument("queueing update: %w", err)
			}
		}
```

**File:** internal/git/updateref/updateref.go (L297-331)
```go
func New(ctx context.Context, repo gitcmd.RepositoryExecutor, opts ...UpdaterOpt) (*Updater, error) {
	var cfg updaterConfig
	for _, opt := range opts {
		opt(&cfg)
	}

	objectHash, err := repo.ObjectHash(ctx)
	if err != nil {
		return nil, fmt.Errorf("detecting object hash: %w", err)
	}

	txOption := gitcmd.WithRefTxHook(objectHash, repo)
	if cfg.disableTransactions {
		txOption = gitcmd.WithDisabledHooks()
	}

	cmdFlags := []gitcmd.Option{gitcmd.Flag{Name: "-z"}, gitcmd.Flag{Name: "--stdin"}}
	if cfg.noDeref {
		cmdFlags = append(cmdFlags, gitcmd.Flag{Name: "--no-deref"})
	}

	var stderr bytes.Buffer
	cmd, err := repo.Exec(ctx,
		gitcmd.Command{
			Name:  "update-ref",
			Flags: cmdFlags,
		},
		txOption,
		gitcmd.WithSetupStdin(),
		gitcmd.WithSetupStdout(),
		gitcmd.WithStderr(&stderr),
	)
	if err != nil {
		return nil, err
	}
```

**File:** doc/hooks.md (L198-204)
```markdown
There are two users of these hooks:

- `PostReceivePack` and `SSHReceivePack` directly invoke `git-receive-pack`,
  which then executes the hooks for us.
- Most RPCs in the `OperationService` that write objects into the repository
  manually invoke these hooks using the `updateref.UpdaterWithHooks` structure.

```
