Found it. `RefService.UpdateReferences` (`internal/gitaly/service/ref/update_references.go`) is a MUTATOR RPC that writes reference updates directly via `updateref.New`/`updater.Commit()`, and its implementation does **not** invoke the `pre-receive`/`update` hooks at all — unlike `OperationService` RPCs which go through `UpdaterWithHooks` (`internal/gitaly/hook/updateref/update_with_hooks.go`), and unlike `git-receive-pack` (`PostReceivePack`/`SSHReceivePack`) which invokes the full hook chain via `WithReceivePackHooks` (`internal/git/gitcmd/hooks_options.go`).

This is structurally analogous to the Malt finding: a "whitelisted"/alternate write path (`UpdateReferences`, comparable to `UniswapHandler`) can be used to update refs and thus make pushed objects reachable, while the code path that enforces the protective gate (Rails' `/internal/allowed` access check invoked from `pre-receive`, comparable to the "buy block") is completely bypassed.### Title
Access-check bypass: `RefService.UpdateReferences` writes and reassigns references without invoking pre-receive/update hooks - (File: internal/gitaly/service/ref/update_references.go)

### Summary
Gitaly enforces GitLab's authorization/protected-branch/access-control checks exclusively through the `pre-receive`/`update` Git hooks, which are invoked by `git-receive-pack` (`PostReceivePack`/`SSHReceivePack`) and by `OperationService` RPCs via `updateref.UpdaterWithHooks`. `RefService.UpdateReferences`, however, is a `MUTATOR` RPC that creates, force-updates, and deletes arbitrary fully-qualified references directly through `updateref.New(...).Commit()` without ever constructing a `gitcmd.NewHooksPayload`, invoking `hookManager.PreReceiveHook`, or calling Rails' `/internal/allowed` endpoint. This is structurally the same class of bug as the Malt finding: a differently-gated write path ("whitelisted" `UniswapHandler`/here, the ref-write RPC) can be used to reach the same end state (a branch pointing at attacker-controlled/pre-existing objects) that the primary gate (buy-block / pre-receive access check) was designed to prevent.

### Finding Description
The receive-pack code path enforces all of GitLab's push-time protections (protected branch checks, force-push checks, "no code owner override," push rules) inside the `pre-receive` hook, which posts the ref-update list to Rails' `/internal/allowed` endpoint before anything is migrated out of quarantine (`doc/hooks.md:207-220`, `doc/object_quarantine.md:81-124`). This same gate is reused for `OperationService` RPCs through `updateref.UpdaterWithHooks.UpdateReference`, which explicitly builds a `gitcmd.NewHooksPayload(...)` and calls `u.hookManager.PreReceiveHook(...)` before migrating the quarantine and updating the ref (`internal/gitaly/hook/updateref/update_with_hooks.go:218-236`).

`RefService.UpdateReferences` (`internal/gitaly/service/ref/update_references.go:16-138`) is a different, directly user-reachable gRPC mutator. Its implementation:
- Validates only the ref name format and object ID hex-validity (`update_references.go:57-83`).
- Creates a bare `updateref.Updater` (`updater, err := updateref.New(ctx, repo)`, line 38) and calls `updater.Commit()` (line 99) — with **no** call to `hook.Manager`, no `gitcmd.NewHooksPayload`, and no `PreReceiveHook`/`UpdateHook` invocation anywhere in the function.
- Accepts a fully-qualified reference of the caller's choosing and an arbitrary new object ID, as long as that object already exists in the repository (`git.ValidateReference`, `objectHash.ValidateHex`) — there is no requirement that the object was received via a gated push.

Because the RPC skips the hooks entirely, none of GitLab's server-side authorization logic that lives in the `pre-receive`/`update` hook chain (protected branch enforcement, force-push blocking, "prevent secrets," etc.) is executed for reference changes made through this RPC. Any caller with access to invoke `UpdateReferences` (e.g., through a legitimate but combined feature flow, similar to the Malt PoC's "swap → send to whitelisted contract → addLiquidity/removeLiquidity" pattern) can move a protected branch to point at any object already reachable in the repository — including objects that were rejected, previously advertised, or exist as a byproduct of another operation — entirely outside of the hook-enforced authorization boundary. `internal/gitaly/service/repository/write_ref.go` (the `WriteRef` RPC) has the identical structural gap: it calls `updateref.New(...)`/`u.Commit()` directly with no hook invocation.

### Impact Explanation
This allows a caller to update or delete any reference in a repository (including protected branches) while completely bypassing the `pre-receive`/`update` hook gate that GitLab relies on for all push-time policy enforcement (protected branch rules, force-push restrictions, and any custom server hooks). This mirrors the Malt bug's core problem: a functionally-equivalent effect to the gated operation ("commit a ref pointing to new content") is achievable through an alternate, unguarded interface, undermining the protocol's/product's ability to enforce its central protection guarantee. Because `UpdateReferences`/`WriteRef` are exposed as first-class RPCs (not restricted to the receive-pack code path), any client able to invoke them — directly or via a chained/legitimate feature workflow that ultimately calls them — achieves a hook/authorization bypass.

### Likelihood Explanation
Medium: exploitation requires the caller to have gRPC access to `RefService.UpdateReferences` (or `RepositoryService.WriteRef`) for the target repository, which in Gitaly's model is typically reachable by the same actors permitted to reach `OperationService`/`SmartHTTPService` RPCs for that repository (i.e., ordinary authenticated push/API access, not a privileged operator). Since these RPCs are documented, public, `MUTATOR`-tagged RPCs with straightforward validation (only format/hex checks), no special conditions or race window are needed — an ordinary caller can invoke them directly once objects they want to reference already exist in the repo (e.g., from any prior push, fetch, or fork). This is analogous to the "value can be extracted by bypassing the check" downgrade rationale in the original Malt finding (Medium, not protocol-breaking, but a concrete bypass of a security-relevant gate).

### Recommendation
- Route `RefService.UpdateReferences` and `RepositoryService.WriteRef` through the same hook-invocation path used by `updateref.UpdaterWithHooks` (i.e., invoke `PreReceiveHook`/`UpdateHook`/`ReferenceTransactionHook` with a proper `gitcmd.NewHooksPayload`) whenever the RPC is reachable by non-internal/non-trusted callers, or
- Restrict these RPCs (via Praefect/gitlab-shell authorization policy) so they can only be invoked by trusted internal callers (e.g., replication, backup/restore, housekeeping) that are known not to be an end-user-controllable path for policy-relevant ref changes, and
- Document/enforce that any caller of `UpdateReferences`/`WriteRef` is responsible for performing equivalent authorization checks before calling, and audit all current internal callers (e.g., `internal/backup/repository.go`) to confirm none of them expose this path to end users indirectly.

### Proof of Concept
1. Attacker (or a legitimate feature acting on attacker-influenced input) pushes/creates an object `X` through any normal path (a fetch, a fork, an ordinary non-protected-branch push) so that `X` exists in the repository's object database.
2. Attacker calls `RefService.UpdateReferences` directly with:
   - `reference = refs/heads/<protected-branch>`
   - `new_object_id = X`
   - `old_object_id` empty (force update) or set to the current tip to pass the raceless check.
3. `update_references.go` validates only the ref name format and that `X` is a valid, existing object ID — it never calls `hookManager.PreReceiveHook`/`UpdateHook`, so Rails' `/internal/allowed` check (protected branch/force-push policy) is never consulted.
4. `updater.Commit()` (backed by `git-update-ref`) succeeds and `refs/heads/<protected-branch>` now points at `X`, achieving the exact effect a protected `git push` to that branch would have been blocked from achieving — the "buy block"-equivalent authorization gate has been bypassed via the alternate RPC path. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

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

**File:** internal/gitaly/service/ref/update_references.go (L85-99)
```go
			if err := updater.Update(git.ReferenceName(reference), git.ObjectID(newObjectID), git.ObjectID(oldObjectID)); err != nil {
				return structerr.NewInvalidArgument("queueing update: %w", err)
			}
		}

		if request, err = server.Recv(); err != nil {
			if errors.Is(err, io.EOF) {
				break
			}

			return fmt.Errorf("receiving subsequent request: %w", err)
		}
	}

	if err := updater.Commit(); err != nil {
```

**File:** internal/gitaly/hook/updateref/update_with_hooks.go (L218-236)
```go
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

```

**File:** internal/gitaly/service/repository/write_ref.go (L100-127)
```go
	}

	u, err := updateref.New(ctx, repo)
	if err != nil {
		return fmt.Errorf("error when running creating new updater: %w", err)
	}
	defer func() {
		if err := u.Close(); err != nil && returnedErr == nil {
			returnedErr = fmt.Errorf("close updater: %w", err)
		}
	}()

	if err := u.Start(); err != nil {
		return fmt.Errorf("start reference transaction: %w", err)
	}

	if err := u.Update(git.ReferenceName(req.GetRef()), newObjectID, oldObjectID); err != nil {
		return fmt.Errorf("error when creating update-ref command: %w", err)
	}

	if err := u.Commit(); err != nil {
		var alreadyLockedErr updateref.AlreadyLockedError
		if errors.As(err, &alreadyLockedErr) {
			return structerr.NewAborted("reference is locked already").WithMetadata("reference", alreadyLockedErr.ReferenceName)
		}

		return fmt.Errorf("error when running update-ref command: %w", err)
	}
```

**File:** doc/hooks.md (L205-220)
```markdown
These hooks perform the following functions:

- `pre-receive`: The pre-receive hook receives all reference updates as a whole
  via standard input, where each change is represented by one line with the old
  and new object ID as well the name of the reference that is to be updated. At
  this point, all objects required to satisfy the update have already been
  received, but they are still in a separate "quarantine directory" and are
  therefore detached from the main repository. This hook first increments a
  reference counter that tracks how many pushes are active at the same time.
  Afterwards, it posts all changes to Rails' `/internal/allowed` API endpoint so
  that Rails can determine whether the change is allowed or not. Because objects
  still live in a quarantine directory, Gitaly tells Rails where it can find the
  quarantine directory using the repository's alternative object directory
  fields so that any subsequent RPC calls that check the change can access those
  objects. When the access checks succeed, any existing custom pre-receive hooks
  installed by the administrator are executed.
```
