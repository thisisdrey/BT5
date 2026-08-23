### Title
`UpdateReferences` RPC omits the internal-reference denylist enforced elsewhere, allowing protected/hidden refs to be created, moved, or deleted - ([File: internal/gitaly/service/ref/update_references.go])

### Summary
Gitaly maintains a canonical denylist of "internal" reference prefixes (`refs/keep-around/`, `refs/remotes/`, `refs/tmp/`, `refs/environments/`, `refs/merge-requests/`, `refs/pipelines/`) that must never be writable by ordinary git operations. This denylist is enforced when Gitaly spawns `git-receive-pack`/`git-upload-pack`, but the same denylist is never consulted by the `RefService.UpdateReferences` RPC, which accepts an arbitrary, fully-qualified reference name straight from the request and only validates its *syntax*, not whether it targets a protected namespace. This mirrors the BPT bug class: a resource that must never be mutated by an untrusted path is protected in one code path but the check is missing in a sibling path that reaches the same underlying resource.

### Finding Description
`git.InternalRefPrefixes` is defined once and documented as needing "special treatment... to restrict writing to them": [1](#0-0) 

That map is consumed to hide/protect these refs specifically for `git-receive-pack`/`git-upload-pack` invocations: [2](#0-1) 

and this is verified by the `TestPostReceivePack_hiddenRefs` test, which shows an ordinary push to `refs/environments/*`, `refs/merge-requests/*`, `refs/pipelines/*` is rejected with "deny updating a hidden ref": [3](#0-2) 

However, `RefService.UpdateReferences` — a separate, directly-callable gRPC RPC that performs raceless reference creation/update/deletion via `updateref.Updater` — never consults `git.InternalRefPrefixes`. It only validates general reference-name syntax and object-ID hex format: [4](#0-3) 

There is no call to any function checking the reference against `git.InternalRefPrefixes`, and a repo-wide search confirms `git.InternalRefPrefixes` is referenced only from `command_description.go`, `cleaner.go`, and a test file — never from the `ref` or `operations` service packages that implement `UpdateReferences`/`DeleteRefs`. This RPC therefore allows a caller supplying a crafted `reference` field to create, force-update, or delete refs under `refs/keep-around/`, `refs/remotes/`, `refs/tmp/`, `refs/environments/`, `refs/merge-requests/`, and `refs/pipelines/` — namespaces that GitLab and Gitaly's own hook/hiding logic assume are never writable by external actors and are exclusively managed internally.

### Impact Explanation
- Deleting `refs/keep-around/<sha>` refs removes the only anchor keeping GC from pruning objects referenced by merge requests/notes/pipelines that aren't reachable from a branch, causing silent, unrecoverable data loss (objects vanish on the next `git gc`).
- Overwriting `refs/merge-requests/*/head` or `refs/pipelines/*` lets a caller point these refs at attacker-chosen commits. GitLab and CI tooling trust the contents of these refs as authoritative (e.g., to fetch what a pipeline/MR is built from), so this is an integrity bypass of data that's supposed to be read-only from the client's perspective.
- Creating spurious `refs/remotes/*` or `refs/tmp/*` entries can corrupt state assumptions made elsewhere in Gitaly/GitLab code that treats these namespaces as exclusively internal.
- This bypasses the exact protection ("deny updating a hidden ref") that `git-receive-pack` enforces for the identical resource, purely because a second write path forgot to apply the same check — directly analogous to the missing `BALANCER_POOL_TOKEN` check in `AuraStakingMixin` while `ConvexStakingMixin` correctly excluded its LP token.

### Likelihood Explanation
`UpdateReferences` is a first-class, streaming gRPC RPC (`RefService.UpdateReferences`) reachable by any client holding a valid Gitaly auth token that can address a repository — the same trust boundary as other mutator RPCs used on behalf of ordinary user actions (backups, replication, and potentially GitLab-side code paths that proxy user-driven operations). Exploitation requires no special repository state, no race, and only crafting the `reference` byte field of the request; unlike `OperationService.UserUpdateBranch`/`UserDeleteBranch`, which always prefix branch names with `refs/heads/` and thus cannot target internal namespaces, `UpdateReferences` takes the fully-qualified reference verbatim.

### Recommendation
Before queuing any update in `UpdateReferences` (and `DeleteRefs`), reject references whose name matches any prefix in `git.InternalRefPrefixes`, consistent with the enforcement already done for `git-receive-pack`/`git-upload-pack`. Consider centralizing this check into a shared helper (e.g., `git.IsInternalReference(name)`) used by every reference-mutating code path (both the ref service and hook/receive-pack hiding logic) so future write paths cannot omit it.

### Proof of Concept
1. As a client holding a valid Gitaly auth token, open a `RefService.UpdateReferences` stream against a target repository.
2. Send an `UpdateReferencesRequest` with:
   - `reference = "refs/keep-around/<sha-of-an-otherwise-unreachable-object>"`, `new_object_id = <all-zero OID>` (deletion), or
   - `reference = "refs/merge-requests/1/head"`, `new_object_id = <attacker-controlled commit>`, `old_object_id = ""` (force update).
3. Observe that `update_references.go` only validates format via `git.ValidateReference` and `objectHash.ValidateHex` and then calls `updater.Update(...)` without any internal-reference check, so the update succeeds — in contrast to attempting the same ref name via `git push`, which `git-receive-pack` rejects with "deny updating a hidden ref" due to `hiddenReceivePackRefPrefixes`. [5](#0-4)

### Citations

**File:** internal/git/reference.go (L45-71)
```go
// InternalRefPrefixes is an array of all reference prefixes which are used internally by GitLab.
// These need special treatment in some cases, e.g. to restrict writing to them.
var InternalRefPrefixes = map[string]InternalReferenceType{
	// Environments may be interesting to the user in case they want to figure out what exact
	// reference an environment has been constructed from.
	"refs/environments/": InternalReferenceTypeReadonly,

	// Keep-around references are only used internally to keep alive objects, and thus they
	// shouldn't be exposed to the user.
	"refs/keep-around/": InternalReferenceTypeHidden,

	// Merge request references should be readable by the user so that they can still fetch the
	// changes of specific merge requests.
	"refs/merge-requests/": InternalReferenceTypeReadonly,

	// Pipelines may be interesting to the user in case they want to figure out what exact
	// reference a specific pipeline has been running with.
	"refs/pipelines/": InternalReferenceTypeReadonly,

	// Remote references shouldn't typically exist in repositories nowadays anymore, and there
	// is no reason to expose them to the user.
	"refs/remotes/": InternalReferenceTypeHidden,

	// Temporary references are used internally by Rails for various operations and should not
	// be exposed to the user.
	"refs/tmp/": InternalReferenceTypeHidden,
}
```

**File:** internal/git/gitcmd/command_description.go (L453-486)
```go
func hiddenReceivePackRefPrefixes(ctx context.Context) []GlobalOption {
	config := make([]GlobalOption, 0, len(git.InternalRefPrefixes))

	for refPrefix, refType := range git.InternalRefPrefixes {
		switch refType {
		case git.InternalReferenceTypeReadonly, git.InternalReferenceTypeHidden:
			// We want to hide both read-only and hidden refs in git-receive-pack(1) so
			// that we make neither of them writeable.
			config = append(config, ConfigPair{Key: "receive.hideRefs", Value: refPrefix})
		default:
			panic(fmt.Sprintf("unhandled internal reference type: %v", refType))
		}
	}

	return config
}

func hiddenUploadPackRefPrefixes(context.Context) []GlobalOption {
	config := make([]GlobalOption, 0, len(git.InternalRefPrefixes))

	for refPrefix, refType := range git.InternalRefPrefixes {
		switch refType {
		case git.InternalReferenceTypeHidden:
			config = append(config, ConfigPair{Key: "uploadpack.hideRefs", Value: refPrefix})
		case git.InternalReferenceTypeReadonly:
			// git-upload-pack(1) doesn't allow writing references, and we do want to
			// announce read-only references that aren't hidden.
		default:
			panic(fmt.Sprintf("unhandled internal reference type: %v", refType))
		}
	}

	return config
}
```

**File:** internal/gitaly/service/smarthttp/receive_pack_test.go (L222-259)
```go
func TestPostReceivePack_hiddenRefs(t *testing.T) {
	t.Parallel()

	ctx := testhelper.Context(t)
	cfg := testcfg.Build(t)
	cfg.SocketPath = runSmartHTTPServer(t, cfg)
	testcfg.BuildGitalyHooks(t, cfg)

	client := newSmartHTTPClient(t, cfg.SocketPath, cfg.Auth.Token)

	for _, ref := range []string{
		"refs/environments/1",
		"refs/merge-requests/1/head",
		"refs/merge-requests/1/merge",
		"refs/pipelines/1",
	} {
		t.Run(ref, func(t *testing.T) {
			t.Parallel()

			repoProto, repoPath := gittest.CreateRepository(t, ctx, cfg)
			repoProto.GlProjectPath = "project/path"

			gittest.WriteCommit(t, cfg, repoPath, gittest.WithReference(ref))
			push := setupSimplePush(t, ctx, cfg, repoPath, git.ReferenceName(ref))

			stream, err := client.PostReceivePack(ctx)
			require.NoError(t, err)

			response := push.perform(t, stream, &gitalypb.PostReceivePackRequest{
				Repository:   repoProto,
				GlUsername:   "user",
				GlId:         "123",
				GlRepository: "project-456",
			})

			require.Contains(t, response, fmt.Sprintf("%s deny updating a hidden ref", ref))
		})
	}
```

**File:** internal/gitaly/service/ref/update_references.go (L47-88)
```go
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
