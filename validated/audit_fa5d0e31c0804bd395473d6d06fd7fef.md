### Title
Reachability verification for reference/pack dependencies accepts pre-existing objects reachable via shared object-pool alternates, not just objects actually delivered in the push - (File: internal/gitaly/storage/storagemgr/partition/transaction_manager.go)

### Summary
The 1inch report's root cause is a slippage check that validates an absolute post-state ("is `_assetOut` balance ≥ min") instead of the actual delta produced by the operation being checked ("did *this* swap deliver ≥ min"), so pre-existing balance in the contract masks a failed/short swap. The closest reachable analog in Gitaly is `TransactionManager.verifyObjectsExist` / `packObjects`, which determines whether a push's reference updates are backed by real object data using an absolute presence check ("does object X exist anywhere in the repository's object database") rather than verifying that the object was actually supplied by (or already legitimately reachable within) *this* repository's own history prior to the push.

### Finding Description
`packObjects` walks new reference tips and the quarantine directory; any object it needs that is *not* found in the quarantine is treated as a "dependency" rather than a corruption: [1](#0-0) 

These dependencies are later checked only for bare existence in the *target repository's full object database* — which, via `objects/info/alternates`, includes every object in the linked object pool shared by all fork members: [2](#0-1) [3](#0-2) 
`verifyObjectsExist` uses `cat-file --batch-check` against the repository (with alternates in effect), which returns "exists" for any object reachable through the pool, regardless of whether that object was ever legitimately part of the target repository's own reachable history: [4](#0-3) 

Gitaly's own object-pool design confirms that alternates make the *entire* pool's object bytes physically readable by every pool member, and that reachability/pruning safety for a member repo is deliberately separated from "does the byte exist somewhere in this object store": [5](#0-4) 

This is the same class of bug as the 1inch report: a "does X exist/satisfy N" check is performed against an absolute, shared pool of state (the pooled ODB, analogous to the contract's pre-existing token balance) instead of verifying that the object was actually delivered by, or already reachable in, the specific repository being mutated. A user pushing a ref update that points to an OID that is *not* in their own repository's reachable history, and *not* included in their quarant­ine/pack, can pass `verifyObjectsExist` purely because some other, unrelated (potentially private) fork sharing the same object pool happens to contain that blob/tree/commit. The result: a reference in the pusher's own (possibly public) repository becomes reachable pointing at content the pusher never actually possessed or was authorized to disclose, and the push succeeds without git ever validating that the referenced object was supplied.

The code explicitly acknowledges that packed/dependency objects are unvalidated at this stage ("The packed objects are not yet checked for validity"), which is the maintainers' own confirmation that this check-time gap exists: [6](#0-5) 

### Impact Explanation
If exploitable, this allows disclosure of object content across repository boundaries within the same object pool (e.g., a private upstream repository's commits/blobs becoming reachable — and thus fetchable — from a public fork or sibling member), without ever needing valid push access to the source repository holding that content. This is a cross-repository object/data disclosure and integrity issue in exactly the object-pool/alternates isolation area called out as in-scope.

### Likelihood Explanation
Likelihood is Low: it requires (a) a repository that is a member of an object pool (fork network) whose pool also holds objects from other members not otherwise reachable in the attacker's repo, and (b) the ability to craft a push whose reference update points to such an OID while supplying a "thin" pack that omits the OID (so it is classified as a "dependency" rather than a corrupted push). Ordinary git clients typically won't produce such a request, but the request is fully attacker-controlled at the RPC/pack level (crafted `git-receive-pack`/transactional push), so a malicious client can construct it deliberately.

### Recommendation
When verifying object dependencies for a push, do not rely solely on `cat-file --batch-check`/existence in the full alternates-enabled object database. Instead, verify dependencies against the pre-push reachable set of the *target repository only* (e.g., using `git rev-list --objects --all` boundaries or `--not --all` style connectivity checks scoped to the member repository, excluding objects only reachable via the pool but not via the member's own refs), or require the client to supply a genuinely thin pack against a known, previously-advertised set of "haves" for that specific repository, mirroring how git's server-side `connectivity-check`/`--shallow-file`/negotiated `have` lines are supposed to scope validity to what the specific repository is entitled to reference.

### Proof of Concept
Not directly executable from static analysis alone; a concrete PoC would require: (1) creating an object pool with two members A (private, contains secret commit `S`) and B (public); (2) as the owner of B, issuing a transactional push/reference update in B that sets some ref to an OID belonging to a tree/blob only present in A's private history but physically stored in the shared pool, while the packfile sent for that push omits `S` (relying on it being classified as a "dependency"); (3) observing that `verifyObjectsExist`/`packObjects` accepts the push because the object is physically present via `objects/info/alternates`, making `S` reachable (and thus fetchable/dumpable) from repository B. Verifying whether existing pack/ref negotiation elsewhere in Gitaly (e.g., in `internal/gitaly/service/smarthttp` or `internal/gitaly/service/ssh`) already blocks such "have" spoofing before reaching `packObjects` was not confirmed within the available context, so this should be validated end-to-end in a live Gitaly instance before treating it as conclusively exploitable.

### Citations

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L1306-1319)
```go
// packObjects walks the objects in the quarantine directory and the new reference tips. All objects in
// the quarantine directory that are encountered during the walk are included in a packfile that gets
// committed with the transaction. All encountered objects that are missing from the quarantine directory
// are considered the transaction's dependencies. The dependencies are later verified to exist in the
// repository before committing the transaction, and they will be guarded against concurrent pruning
// operations. The final pack is staged in the WAL directory of the transaction ready for committing.
// The pack's index and reverse index is also included.
//
// Objects that already exist in the repository are included in the packfile if the client wrote them
// into the quarantine directory.
//
// The packed objects are not yet checked for validity. See the following issue for more
// details on this: https://gitlab.com/gitlab-org/gitaly/-/issues/5779
func (mgr *TransactionManager) packObjects(ctx context.Context, transaction *Transaction) (returnedErr error) {
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L1812-1818)
```go
				// Verify that all objects this transaction depends on are present in the repository. The dependency
				// objects are the reference tips set in the transaction and the objects the transaction's packfile
				// is based on. If an object dependency is missing, the transaction is aborted as applying it would
				// result in repository corruption.
				if err := mgr.verifyObjectsExist(ctx, targetRepository, transaction.objectDependencies); err != nil {
					return commitResult{error: fmt.Errorf("verify object dependencies: %w", err)}
				}
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L2030-2059)
```go
// verifyObjectsExist verifies that all objects passed in to the method exist in the repository.
// If an object is missing, an InvalidObjectError error is raised.
func (mgr *TransactionManager) verifyObjectsExist(ctx context.Context, repository *localrepo.Repo, oids map[git.ObjectID]struct{}) error {
	defer trace.StartRegion(ctx, "verifyObjectsExist").End()

	if len(oids) == 0 {
		return nil
	}

	revisions := make([]git.Revision, 0, len(oids))
	for oid := range oids {
		revisions = append(revisions, oid.Revision())
	}

	objectHash, err := repository.ObjectHash(ctx)
	if err != nil {
		return fmt.Errorf("object hash: %w", err)
	}

	if err := checkObjects(ctx, repository, revisions, func(revision git.Revision, oid git.ObjectID) error {
		if objectHash.IsZeroOID(oid) {
			return localrepo.InvalidObjectError(revision)
		}

		return nil
	}); err != nil {
		return fmt.Errorf("check objects: %w", err)
	}

	return nil
```

**File:** internal/gitaly/storage/storagemgr/partition/check_objects.go (L18-36)
```go
func checkObjects(ctx context.Context, repository *localrepo.Repo, revisions []git.Revision, callback func(revision git.Revision, objectID git.ObjectID) error) (returnedErr error) {
	defer trace.StartRegion(ctx, "checkObjects").End()

	var stderr bytes.Buffer
	cmd, err := repository.Exec(ctx,
		gitcmd.Command{
			Name: "cat-file",
			Flags: []gitcmd.Option{
				gitcmd.Flag{Name: "--batch-check=%(objectname)"},
				gitcmd.Flag{Name: "--buffer"},
			},
		},
		gitcmd.WithSetupStdin(),
		gitcmd.WithSetupStdout(),
		gitcmd.WithStderr(&stderr),
	)
	if err != nil {
		return structerr.New("exec cat-file: %w", err)
	}
```

**File:** doc/object_pools.md (L1-12)
```markdown
# Object Pools

When creating forks of a repository, most of the objects for forked repository
and the repository it forked from are shared. Storing those shared objects
multiple times is a waste of disk space and also of CPU time, given that those
shared objects would have to be repacked for both repositories. To fix this
waste of resources, we use object pools, which are essentially a repository
which holds the shared objects of both repositories.

The sharing of objects for a given repository and its object pool is done via
alternate object directories which Gitaly sets up when linking a repository to
an object pool by writing the `objects/info/alternates` file.
```
