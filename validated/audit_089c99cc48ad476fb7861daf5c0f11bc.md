### Title
UpdateReferences RPC allows writing to internally-reserved refs (`refs/keep-around/`, `refs/remotes/`, etc.) because it never checks `InternalRefPrefixes` - ([File: internal/gitaly/service/ref/update_references.go])

### Summary
The `UpdateReferences` RPC handler validates each incoming reference name only with `git.ValidateReference`, which is a pure syntax check and has no knowledge of GitLab's internal ref namespaces. Unlike the `git-receive-pack` code path, which explicitly configures `receive.hideRefs` for every entry in `git.InternalRefPrefixes` (both `InternalReferenceTypeHidden` and `InternalReferenceTypeReadonly`) to block writes at the Git level, `UpdateReferences` drives `git-update-ref` directly through `updateref.Updater` and has no equivalent gate.

### Finding Description
`ValidateReference` (`internal/git/reference.go:157-227`) implements Git's `check_or_sanitize_refname()` syntax rules only — it has no concept of `InternalRefPrefixes` (`internal/git/reference.go:47-71`), which classifies prefixes like `refs/keep-around/` and `refs/remotes/` as `InternalReferenceTypeHidden` and `refs/environments/`, `refs/merge-requests/`, `refs/pipelines/` as `InternalReferenceTypeReadonly`.

The `UpdateReferences` gRPC handler (`internal/gitaly/service/ref/update_references.go:57-87`) performs exactly two checks per update: `git.ValidateReference(reference)` for syntax, and `objectHash.ValidateHex(...)` for the object IDs. It then calls `updater.Update(git.ReferenceName(reference), ...)` directly, which drives `git-update-ref` via `updateref.Updater` (`internal/git/updateref/updateref.go:390-396`). There is no call anywhere in this path that consults `git.InternalRefPrefixes` to reject writes to hidden/read-only ref namespaces.

Contrast this with the `git-receive-pack` code path used for a normal git push, where `hiddenReceivePackRefPrefixes` (`internal/git/gitcmd/command_description.go:453-468`) explicitly sets `receive.hideRefs` for **every** entry in `InternalRefPrefixes` — both hidden and read-only types — specifically "so that we make neither of them writeable." This is verified by `TestPostReceivePack_hiddenRefs` (`internal/gitaly/service/smarthttp/receive_pack_test.go:222-260`), which asserts that receive-pack rejects updates to `refs/environments/`, `refs/merge-requests/`, and `refs/pipelines/` with "deny updating a hidden ref."

`UpdateReferences` bypasses `git-receive-pack` entirely (it uses `git-update-ref --stdin` instead), so this hideRefs protection never applies. A caller who can invoke `UpdateReferences` for a repository can therefore push directly to `refs/keep-around/<oid>` or `refs/remotes/origin/x`, both syntactically valid per `ValidateReference` and both classified as `InternalReferenceTypeHidden`, with no rejection.

### Impact Explanation
This breaks the "writes must be gated for internal refs regardless of syntax validity" invariant. Attacker impact:
- Writing arbitrary content into `refs/keep-around/*` can resurrect/pin otherwise-unreachable objects, defeating object-visibility assumptions that rely on keep-around refs being purely internal bookkeeping.
- Writing into `refs/remotes/*` can corrupt or spoof object-pool-linked remote-tracking refs (`git.ObjectPoolRefNamespace = "refs/remotes/origin"`, `internal/git/reference.go:75`), potentially confusing pool/fork replication logic that trusts that namespace.
- More generally, it is an authorization/consistency bypass: refs meant to be hidden from and unwritable by users become directly writable through an RPC that skips both Rails `/allowed` authorization (unlike `OperationService` RPCs such as `UserCreateBranch`, which force the ref into `refs/heads/` via `NewReferenceNameFromBranchName`) and Git's own hideRefs enforcement.

### Likelihood Explanation
Exploitability hinges entirely on whether `UpdateReferences` is reachable by a caller holding only a repository-scoped Gitaly token (as opposed to being restricted to trusted internal callers such as Praefect replication or the backup subsystem, both of which are its only in-repo callers found: `internal/backup/repository.go`, `internal/gitaly/storage/storagemgr/partition/transaction_manager.go`). I was unable to confirm from the available proto annotations (`proto/ref.proto`) whether `UpdateReferences` carries an op-type restriction or ACL that limits it to internal/maintenance callers versus being reachable the same way any other `RefService` RPC is reachable given a valid token for the target repository. This is the key open question for likelihood — if it is reachable with just a normal repo-scoped auth token (as the question's threat model assumes), the exploit is trivial and fully repeatable; if it is gated to internal-only callers by additional infrastructure not visible in this repo, the practical likelihood is much lower.

### Recommendation
Add a check in `UpdateReferences` (and any other RPC that writes references directly via `updateref.Updater` without going through `git-receive-pack`'s hideRefs configuration) that rejects updates whose reference name has a prefix present in `git.InternalRefPrefixes`, mirroring the protection already applied to `git-receive-pack` in `hiddenReceivePackRefPrefixes`. This should return a distinct, well-typed error (not merely reuse the `ValidateReference` invalid-format error) so callers can differentiate a syntax problem from an authorization/internal-ref rejection.

### Proof of Concept
```go
func TestUpdateReferences_rejectsInternalRefs(t *testing.T) {
    t.Parallel()
    ctx := testhelper.Context(t)
    cfg, client := setupRefService(t)

    repoProto, repoPath := gittest.CreateRepository(t, ctx, cfg)
    commitID := gittest.WriteCommit(t, cfg, repoPath)

    for refName := range git.InternalRefPrefixes {
        target := refName + "1"
        require.NoError(t, git.ValidateReference(target), "ref must pass syntax validation")

        stream, err := client.UpdateReferences(ctx)
        require.NoError(t, err)
        require.NoError(t, stream.Send(&gitalypb.UpdateReferencesRequest{
            Repository: repoProto,
            Updates: []*gitalypb.UpdateReferencesRequest_Update{
                {
                    Reference:   []byte(target),
                    NewObjectId: []byte(commitID),
                },
            },
        }))

        _, err = stream.CloseAndRecv()
        // EXPECTED: a permission/validation error rejecting writes to internal refs.
        // ACTUAL (current behavior): err == nil and the internal ref is written.
        require.Error(t, err, "internal ref %s should not be writable", target)
    }
}
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** internal/git/reference.go (L157-227)
```go
func ValidateReference(name string) error {
	if name == "HEAD" {
		return fmt.Errorf("HEAD reference not allowed")
	}

	// TODO: this can eventually be converted to use `strings.CutPrefix()`.
	if !strings.HasPrefix(name, "refs/") {
		return fmt.Errorf("reference is not fully qualified")
	}
	name = name[len("refs/"):]

	if len(name) == 0 {
		return fmt.Errorf("refs/ is not a valid reference")
	}

	if strings.HasSuffix(name, "/") {
		return fmt.Errorf("reference must not end with slash")
	}

	if strings.HasSuffix(name, ".") {
		return fmt.Errorf("reference must not end with dot")
	}

	if strings.Contains(name, "@{") {
		return fmt.Errorf("reference must not contain @{")
	}

	if strings.Contains(name, "..") {
		return fmt.Errorf("reference must not contain double dots")
	}

	for _, c := range name {
		switch c {
		case ' ', '\t', '\n':
			return fmt.Errorf("reference must not contain space characters")
		case ':', '?', '[', '\\', '^', '~', '*', '\177':
			return fmt.Errorf("reference must not contain special characters")
		}

		// Note that we treat some of the characters below 32 specially in the switch above so
		// that we can report back more precise error messages.
		if c < 32 {
			return fmt.Errorf("reference must not contain control characters")
		}
	}

	// We need to check the components individually as components aren't allowed to have some specific constructs.
	for {
		component, tail, _ := strings.Cut(name, "/")

		if component == "" {
			if tail != "" {
				return fmt.Errorf("empty component is not allowed")
			}

			// Otherwise, if both component and tail are empty, we have fully verified the complete
			// reference and can thus return successfully.
			return nil
		}

		if strings.HasPrefix(component, ".") {
			return fmt.Errorf("component must not start with dot")
		}

		if strings.HasSuffix(component, ".lock") {
			return fmt.Errorf("component must not end with .lock")
		}

		name = tail
	}
}
```

**File:** internal/gitaly/service/ref/update_references.go (L57-87)
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
```

**File:** internal/git/updateref/updateref.go (L390-396)
```go
func (u *Updater) Update(reference git.ReferenceName, newOID, oldOID git.ObjectID) error {
	if err := u.expectState(stateStarted); err != nil {
		return err
	}

	return u.write("update %s\x00%s\x00%s\x00", reference.String(), newOID, oldOID)
}
```

**File:** internal/git/gitcmd/command_description.go (L453-468)
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
```

**File:** internal/gitaly/service/smarthttp/receive_pack_test.go (L222-260)
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
}
```

**File:** internal/gitaly/service/operations/user_create_branch.go (L65-67)
```go
	referenceName := git.NewReferenceNameFromBranchName(string(req.GetBranchName()))

	if err := s.updateReferenceWithHooks(ctx, req.GetRepository(), req.GetUser(), quarantineDir, referenceName, startPointOID, objectHash.ZeroOID); err != nil {
```
