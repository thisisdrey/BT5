### Title
Unbounded creation of `refs/dangling/*` references in shared object pools allows any pool member to DoS pool housekeeping and reference enumeration - (File: internal/git/objectpool/fetch.go)

### Summary
Any repository that is a member of a Gitaly object pool (e.g. any fork of a project) can, through ordinary pushes, force-pushes, and branch/tag churn, cause an unbounded number of "insignificant" `refs/dangling/$OID` references to be created inside the *shared* object pool repository during `FetchIntoObjectPool`. Nothing in `rescueDanglingObjects` caps how many dangling refs get created, and the object pool's own reference-transaction/prune logic (`pruneReferences`) is only capable of *adding* to this set, never removing it, since dangling objects can never be safely proven unreferenced. This mirrors the Ajna `PositionManager.memorializePositions` finding: an unprivileged actor (a fork owner) can attach unlimited, low-value entries to a shared resource (the pool) that other members (other forks, the origin repo) depend on, without the consent of those other members, degrading operations that must iterate over that resource (ref enumeration, `git-pack-refs`, `git-repack`, subsequent `FetchIntoObjectPool` runs).

### Finding Description
Object pools deduplicate objects across many repositories linked via `objects/info/alternates` (`internal/git/objectpool/link.go`, `doc/object_pools.md`). Housekeeping is driven by `FetchIntoObjectPool`, which fetches `+refs/*:refs/remotes/origin/*` from a pool member into the pool [1](#0-0) , then unconditionally converts every object git-fsck reports as dangling into a permanent reference: [2](#0-1) 

There is no limit on how many such references may be created, and by design they can never be deleted, because "there is no point in time where it's safe to delete objects from the object pool" [3](#0-2) . The documentation itself acknowledges the scaling hazard this creates: [4](#0-3) 

Any ordinary repository owner whose repo is (or becomes, e.g. via fork creation) a pool member can drive this growth entirely through routine, unprivileged Git operations against their *own* repository: repeatedly force-pushing branches, creating and deleting many refs, or rewriting history. None of that requires access to the pool itself or to any other member — analogous to how, in the Ajna finding, `memorializePositions` let an unrelated caller attach cheap entries to someone else's position storage. Each subsequent `FetchIntoObjectPool` run (triggered by GitLab's normal housekeeping for that member) will pull in the churn as `refs/remotes/origin/*` and then permanently pin every resulting dangling object with a `refs/dangling/$OID` ref in the shared pool, which is invisible to and outside the control of the pool's other members.

`FetchIntoObjectPool`'s request validation only checks that the `origin` and `object_pool` share the same storage name — it performs no bound on the number of references/objects being fetched, and the underlying `git fetch`/`git fsck`/ref-creation loop has no cap either: [5](#0-4) 

### Impact Explanation
As dangling references accumulate without bound in the pool:
- `git for-each-ref`, `git-pack-refs(1)`, and the delta-island-aware `git-repack(1)` step that `FetchFromOrigin` runs on every housekeeping cycle all become increasingly expensive, as explicitly called out in `doc/object_pools.md` ("Fetches into the object pool and repacking of references can thus become quite expensive") [4](#0-3) .
- Because all pool members share the same alternates-linked object pool, this is a resource-exhaustion/DoS vector that one low-privilege pool member (e.g., a fork owner with no relationship to the origin project's maintainers) can inflict on the housekeeping and performance of every other member sharing that pool, i.e. a cross-repository impact from an action confined to the attacker's own repository.
- `rescueDanglingObjects` and the surrounding fetch/fsck/repack pipeline run with no ceiling on iteration count, so sufficiently large churn can turn routine housekeeping RPCs (`FetchIntoObjectPool`) into a persistent operational burden or timeout/DoS risk for the storage node handling that partition.

### Likelihood Explanation
Triggering this requires nothing beyond ordinary push access to a single fork/pool member — no cross-repository RPC, no elevated Gitaly permissions, and no crafted protobuf fields are needed. The only precondition is that the attacker-controlled repository is (or can become, e.g. via forking a public project) a member of an object pool, which is a normal and common GitLab feature. Because the growth is monotonic and irreversible by design, sustained low-effort activity (repeated force-pushes/branch churn) is sufficient over time.

### Recommendation
- Cap or rate-limit the number of dangling references `rescueDanglingObjects` will create per `FetchIntoObjectPool` invocation, and/or track/report growth so operators can detect abusive pool members.
- Consider isolating per-member churn (e.g., quotas on ref creation/deletion rate for pool members) so that one member cannot unilaterally inflate the shared pool's reference set.
- Explore expiring/consolidating `refs/dangling/*` entries using a safer object-liveness proof (e.g., periodic connectivity checks across all *current* members) instead of permanently pinning every dangling object encountered at fetch time.

### Proof of Concept
1. Create a project and fork it so that the fork is linked to the project's object pool (`CreateObjectPool` + `LinkRepositoryToObjectPool`, as GitLab does automatically for forks).
2. As the fork owner (no special privileges), repeatedly: create a branch, push a commit, then force-push a divergent commit to the same branch, then delete the branch. Repeat this loop thousands of times using only standard `git push`.
3. Trigger (or wait for GitLab's scheduled) `FetchIntoObjectPool` for that fork. Observe in `internal/git/objectpool/fetch.go`'s `rescueDanglingObjects` that every discarded commit/blob/tree from the churn becomes a permanent `refs/dangling/$OID` reference in the shared pool [6](#0-5) .
4. Measure the increasing time taken by `git-pack-refs`, `git-repack`, and subsequent `FetchIntoObjectPool` calls on the pool as the dangling-ref count grows, demonstrating degraded housekeeping performance shared by all pool members, none of whom initiated or consented to the churn.

### Citations

**File:** internal/git/objectpool/fetch.go (L25-25)
```go
var objectPoolRefspec = fmt.Sprintf("+refs/*:%s/*", git.ObjectPoolRefNamespace)
```

**File:** internal/git/objectpool/fetch.go (L277-337)
```go
// rescueDanglingObjects creates refs for all dangling objects if finds
// with `git fsck`, which converts those objects from "dangling" to
// "not-dangling". This guards against any object ever being deleted from
// a pool repository. This is a defense in depth against accidental use
// of `git prune`, which could remove Git objects that a pool member
// relies on. There is currently no way for us to reliably determine if
// an object is still used anywhere, so the only safe thing to do is to
// assume that every object _is_ used.
func (o *ObjectPool) rescueDanglingObjects(ctx context.Context) (returnedErr error) {
	stderr := &bytes.Buffer{}
	fsck, err := o.Repo.Exec(ctx, gitcmd.Command{
		Name:  "fsck",
		Flags: []gitcmd.Option{gitcmd.Flag{Name: "--connectivity-only"}, gitcmd.Flag{Name: "--dangling"}},
	},
		gitcmd.WithStderr(stderr),
		gitcmd.WithSetupStdout(),
	)
	if err != nil {
		return err
	}

	updater, err := updateref.New(ctx, o.Repo, updateref.WithDisabledTransactions())
	if err != nil {
		return err
	}
	defer func() {
		if err := updater.Close(); err != nil && returnedErr == nil {
			returnedErr = fmt.Errorf("cancel updater: %w", err)
		}
	}()

	if err := updater.Start(); err != nil {
		return fmt.Errorf("start reference transaction: %w", err)
	}

	objectHash, err := o.ObjectHash(ctx)
	if err != nil {
		return fmt.Errorf("detecting object hash: %w", err)
	}

	scanner := bufio.NewScanner(fsck)
	for scanner.Scan() {
		split := strings.SplitN(scanner.Text(), " ", 3)
		if len(split) != 3 {
			continue
		}

		if split[0] != "dangling" {
			continue
		}

		danglingObjectID, err := objectHash.FromHex(split[2])
		if err != nil {
			return fmt.Errorf("parsing object ID %q: %w", split[2], err)
		}

		ref := git.ReferenceName(danglingObjectNamespace + split[2])
		if err := updater.Create(ref, danglingObjectID); err != nil {
			return err
		}
	}
```

**File:** doc/object_pools.md (L87-91)
```markdown
This issue is kind of unsolvable: there is no point in time where it's safe to
delete objects from the object pool, as we do not know which repositories may be
linked to it. And even if we knew, we cannot determine all references of all
repositories at once in a race-free manner. We thus must consider each object to
still be referenced somewhere.
```

**File:** doc/object_pools.md (L98-103)
```markdown
Having unreachable objects kept alive in this fashion does have its problems:

- For busy repositories, we generate loads of dangling references. While these
  references [cannot be seen by clients](#references), they are seen when
  performing housekeeping tasks on the object pool itself. Fetches into the
  object pool and repacking of references can thus become quite expensive.
```

**File:** internal/gitaly/service/objectpool/fetch_into_object_pool.go (L102-117)
```go
func validateFetchIntoObjectPoolRequest(req *gitalypb.FetchIntoObjectPoolRequest) error {
	if req.GetOrigin() == nil {
		return errors.New("origin is empty")
	}

	if req.GetObjectPool() == nil {
		return errors.New("object pool is empty")
	}

	originRepository, poolRepository := req.GetOrigin(), req.GetObjectPool().GetRepository()

	if originRepository.GetStorageName() != poolRepository.GetStorageName() {
		return errors.New("origin has different storage than object pool")
	}

	return nil
```
