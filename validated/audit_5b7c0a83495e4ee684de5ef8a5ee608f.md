### Title
`UpdateReferences` RPC allows raw reference writes that bypass the pre-receive/update access-check hooks enforced by every other reference-mutating RPC - ([File: internal/gitaly/service/ref/update_references.go])

### Summary
The reported Solidity bug is a class of "gate exists on the primary mutation path, but a second, functionally-equivalent path to reach the same state change omits the gate." In Gitaly, reference mutations normally must pass through `pre-receive`/`update` hooks, which perform GitLab's authorization ("access check") logic — branch protection, force-push restriction, and the "Not Allowed" push rules described in `doc/hooks.md`. `RefService.UpdateReferences`, however, is a `MUTATOR` RPC that updates references directly via `updateref.New` without invoking any hook manager, `PreReceiveHook`, or `UpdateHook` call.

### Finding Description
Every other ref-mutating code path in Gitaly is deliberately wired through hook invocation before the ref is actually written:
- `git-receive-pack`-based flows (`SSHReceivePack`, `PostReceivePack`) execute Git's native `pre-receive`/`update`/`post-receive` hooks, as documented in `doc/hooks.md` (`internal/gitaly/service/ssh/receive_pack.go`, `internal/gitaly/service/smarthttp/receive_pack.go`, both via `gitcmd.WithReceivePackHooks`). [1](#0-0) 
- `OperationService` RPCs (e.g. `UserCherryPick`, `UserUpdateBranch`) manually drive the equivalent hook sequence via `UpdaterWithHooks.UpdateReference`, which calls `PreReceiveHook` (the GitLab `/internal/allowed` access check) and `UpdateHook` before the ref is actually updated with `git-update-ref`. [2](#0-1) 

In contrast, `RefService.UpdateReferences` builds an `updateref.Updater` directly from the raw repository and queues/commits updates with **no call whatsoever** to `hookManager.PreReceiveHook` or `UpdateHook`: [3](#0-2) [4](#0-3) 

The RPC is registered as a standard `op: MUTATOR` in the public proto surface with only a repository field and per-update old/new OID validation — there is no gate requiring that the caller already passed a pre-receive/update hook check, and no marker distinguishing "trusted internal caller" (e.g., backup restore, replication) from an arbitrary RPC client: [5](#0-4) 

This mirrors the reported bug precisely: the "primary" mutation path (`OperationService`/`receive-pack`, analogous to `mint`) is properly gated by the hook-based access-check mechanism (analogous to `whenNotPaused`), while an alternate RPC that achieves the same end state — moving a reference to point at a new object, i.e., depositing/updating repository state — omits that gate (analogous to `onERC721Received` bypassing the pause check).

### Impact Explanation
If `UpdateReferences` is reachable with the same trust level as other repository-scoped mutator RPCs (e.g., through Praefect routing/replication paths, or if Rails' internal-API authorization is misconfigured or the RPC is exposed to a broader trust boundary than intended), a caller could force-update or delete arbitrary references — including protected branches — without triggering GitLab's push-rule/branch-protection checks that are enforced exclusively inside the `pre-receive` hook's `/internal/allowed` call. This would let branch protection, and any other pre-receive/update-hook-based control, be silently bypassed for any repository the caller can address, while giving administrators/users the false impression that those controls are always enforced (matching the source report's "confusion for users" framing, but with a stronger integrity impact here — bypass of push protection rather than a fund-timing inconvenience).

### Likelihood Explanation
Likelihood depends on whether callers of `UpdateReferences` are trusted-only (its known internal use is backup/restore in `internal/backup/repository.go`) or whether the RPC is reachable by a caller that has not already passed through pre-receive/access checks. Because it is a normally-registered `MUTATOR` RPC with the same repository-scoped authorization surface as every other write RPC (no additional internal-only guard visible in the reviewed code), and Gitaly's RPC surface is generally designed on the assumption that "any mutator RPC could theoretically be invoked directly," this is a plausible, code-supported path rather than a purely theoretical one. However, I could not fully confirm from the indexed code whether an additional authorization layer (e.g. Rails scope restrictions, Praefect-side ACLs) restricts `UpdateReferences` to trusted internal callers only — this would need direct verification in a live/full checkout.

### Recommendation
- Require `UpdateReferences` to route through the same `pre-receive`/`update` hook-based access-check pipeline (via `UpdaterWithHooks`) as other reference mutators, or explicitly restrict/authenticate its use to trusted internal callers only (e.g., backup/restore, replication), with that restriction enforced in code, not just by convention.
- If the RPC must remain hook-free for legitimate raceless-restore use cases, add compensating controls (dedicated internal-only scope/permission bit, or an explicit flag confirming the caller already performed access checks) so it cannot be used as a silent bypass of branch protection and other push-rule enforcement.

### Proof of Concept
Not directly reproducible from the indexed code alone; the analysis is based on structural comparison of `internal/gitaly/service/ref/update_references.go` (no hook invocation) against `internal/gitaly/hook/updateref/update_with_hooks.go` and `internal/gitaly/service/ssh/receive_pack.go`/`internal/gitaly/service/smarthttp/receive_pack.go` (both of which invoke pre-receive/update hooks before any ref write). A concrete PoC would require confirming, in a running GitLab/Gitaly deployment, whether `RefService.UpdateReferences` is reachable by a caller lacking the equivalent Rails-side authorization that normally gates ref mutation via `/internal/allowed`, then invoking it against a protected branch to observe whether the update succeeds without a push-rule/access-check rejection.

### Citations

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

**File:** internal/gitaly/hook/updateref/update_with_hooks.go (L222-248)
```go

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

**File:** internal/gitaly/service/ref/update_references.go (L16-45)
```go
func (s *server) UpdateReferences(server gitalypb.RefService_UpdateReferencesServer) error {
	ctx := server.Context()

	request, err := server.Recv()
	if err != nil {
		return fmt.Errorf("receiving initial request: %w", err)
	}

	if err := s.locator.ValidateRepository(ctx, request.GetRepository()); err != nil {
		return err
	}
	repo := s.localRepoFactory.Build(request.GetRepository())

	objectHash, err := repo.ObjectHash(ctx)
	if err != nil {
		return fmt.Errorf("detecting object hash: %w", err)
	}

	// Unset the repository so that we can more easily verify inside of the loop that all incoming requests
	// ain't got a repository set anymore.
	request.Repository = nil

	updater, err := updateref.New(ctx, repo)
	if err != nil {
		return fmt.Errorf("creating updater: %w", err)
	}

	if err := updater.Start(); err != nil {
		return fmt.Errorf("starting updater: %w", err)
	}
```

**File:** internal/gitaly/service/ref/update_references.go (L57-88)
```go
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

**File:** proto/ref.proto (L88-96)
```text
  // UpdateReferences atomically updates a set of references to a new state. This RPC allows creating
  // new references, deleting old references and updating existing references in a raceless way.
  //
  // Updating symbolic references with this RPC is not allowed.
  rpc UpdateReferences(stream UpdateReferencesRequest) returns (UpdateReferencesResponse) {
    option (op_type) = {
      op: MUTATOR
    };
  }
```
