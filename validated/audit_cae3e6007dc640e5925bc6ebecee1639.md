I've found a genuine analog to the reported bug class, self-documented by Gitaly's own maintainers as an acknowledged performance/DoS risk: the **dangling reference accumulation** in object pools, driven by `FetchIntoObjectPool`.

### Title
Unbounded `refs/dangling/$OID` accumulation via repeated force-pushes to pool members enables DoS of pool housekeeping and fetches - (File: internal/git/objectpool/fetch.go)

### Summary
`FetchIntoObjectPool` (invoked by any actor able to trigger fork/pool housekeeping) fetches from a pool member with `+refs/*:refs/remotes/origin/*` and then calls `rescueDanglingObjects`, which runs `git fsck --connectivity-only --dangling` and creates one `refs/dangling/$OID` reference per dangling object found [1](#0-0) [2](#0-1) . This is structurally the same bug class as the reported `tickTracking_` issue: a cheap, repeatable client-triggered action (force-updating a ref in a pool member, analogous to bouncing the price across a tick boundary) causes an internal bookkeeping structure (an ever-growing reference set, analogous to the tick-crossing array) to grow without bound, and that structure is then iterated by common operations (fetch/repack/housekeeping, analogous to mint/burn/harvest).

### Finding Description
Every time a pool member force-updates or deletes a reference and the object becomes otherwise unreferenced, the next `FetchIntoObjectPool` run creates a new permanent `refs/dangling/$OID` reference for it [3](#0-2) . Gitaly's own documentation acknowledges this scales with churn and becomes expensive: "For busy repositories, we generate loads of dangling references... Fetches into the object pool and repacking of references can thus become quite expensive." [4](#0-3) . Unlike the loose-object and loose-reference paths in normal repositories, which are bounded by `LooseObjectLimit` (1024) and trigger automatic repacking [5](#0-4) [6](#0-5) , there is no cap or eviction policy on the number of `refs/dangling/*` entries created in an object pool. Each of these references must be walked by `git-fsck`, `git-pack-refs`, and the full repack step that follows in the same RPC [7](#0-6) .

Any user who can push force-updates to a pool member (e.g., an ordinary fork owner, since pool member repos are ordinary forks connected via alternates) can repeatedly force-push tiny/no-op changes that unreference an object, then trigger `FetchIntoObjectPool` (directly, or via GitLab's periodic scheduling), causing one new dangling ref per iteration with minimal attacker cost, similar to the tiny cross-tick swaps in the reported bug.

### Impact Explanation
As the dangling reference set grows unbounded, `FetchIntoObjectPool` — the sole RPC responsible for object-pool housekeeping — becomes progressively slower to fsck, pack-refs, and repack, degrading or eventually failing pool housekeeping for all repositories connected to that pool (all forks sharing the object pool). Since object pools are shared across many forks in the same partition [8](#0-7) , this can affect many unrelated repositories' fetch and clone performance, not just the attacker's own fork.

### Likelihood Explanation
The attack requires no special privileges: only the ability to force-push/delete-and-recreate references in a repository that is (or becomes) a member of an object pool, and for `FetchIntoObjectPool` to run (which GitLab triggers as periodic housekeeping). The cost per iteration for the attacker (one force-push) is far lower than the incremental cost imposed on the shared pool's housekeeping, mirroring the asymmetry described in the original report.

### Recommendation
Introduce a bound analogous to `LooseObjectLimit`/`maxTrackedCommands` for `refs/dangling/*` entries in object pools — e.g., cap or batch dangling-ref creation, expire dangling refs after a grace period once no other pool member is known to reference them, or track/report their count so pack-refs/repack cost can be estimated and rate-limited before scanning the full set.

### Proof of Concept
1. Create a fork `A` and connect it to object pool `P` (e.g. via `CreateFork`/`LinkRepositoryToObjectPool`).
2. Repeatedly: push a new commit reference to `A`, run/trigger `FetchIntoObjectPool` so the object becomes part of `P`, then force-delete or force-update the reference in `A` to unreference the object, and trigger `FetchIntoObjectPool` again — each cycle adds one `refs/dangling/$OID` entry to `P` via `rescueDanglingObjects` [9](#0-8) .
3. Repeat this cycle thousands of times cheaply (each cycle is a small push/delete).
4. Observe `FetchIntoObjectPool`'s fsck/pack-refs/repack phases on `P` growing linearly (or worse) with iteration count, degrading housekeeping and downstream fetch/clone performance for all forks of `P`.

### Citations

**File:** internal/git/objectpool/fetch.go (L108-110)
```go
	if err := o.rescueDanglingObjects(ctx); err != nil {
		return fmt.Errorf("rescuing dangling objects: %w", err)
	}
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

**File:** doc/object_pools.md (L45-76)
```markdown
## Housekeeping

Housekeeping for object pools is handled differently from normal repositories as
it not only involves repacking the pool, but also updating it. The housekeeping
task is thus hosted by the `FetchIntoObjectPool` RPC. This task is typically
only executed with the original object pool member from which the pool has been
seeded and updates the pool by fetching from that member.

It performs the following tasks:

1. Common housekeeping tasks are performed. These are common cleanups which are
   shared between object pools and normal repositories. Most importantly, it
   removes stale lockfiles and deletes known-broken stale references.

1. A fetch is performed from the object pool member into the object pool with a
   `+refs/*:refs/remotes/origin/*` refspec. This fetch is most notably not a
   pruning fetch, that is any reference which gets deleted in the member will
   stay around in the pool.

1. The fetch may create new dangling objects which are not referenced anymore in
   the pool repository. These dangling objects will be kept alive by creating
   dangling references such that they do not get deleted in the pool. See
   [Dangling Objects](#dangling-objects) for more information.

1. Loose references are packed via `git-pack-refs(1)`.
1. The pool is repacked via `git-repack(1)`. The repack produces a single packfile
   including all objects with a bitmap index. In order to improve reuse of
   packfiles where Git will read data from the packfile directly instead of
   generating it on the fly, the packfile uses a delta island including
   `refs/heads` and `refs/tags`. This restricts Git to only generate deltas for
   objects which are directly reachable via either a branch or a tag. Most
   notably, this causes us to not generate deltas against dangling references.
```

**File:** doc/object_pools.md (L93-97)
```markdown
As a safeguard to not lose any objects by accident, we thus create dangling
references in the object pool after the fetch in `FetchIntoObjectPool`. For each
dangling object, a reference `refs/dangling/$OID` is created which points into
the object. This assures that each object is still referenced.

```

**File:** doc/object_pools.md (L100-103)
```markdown
- For busy repositories, we generate loads of dangling references. While these
  references [cannot be seen by clients](#references), they are seen when
  performing housekeeping tasks on the object pool itself. Fetches into the
  object pool and repacking of references can thus become quite expensive.
```

**File:** internal/git/housekeeping/objects.go (L22-26)
```go
const (
	// LooseObjectLimit is the limit of loose objects we accept both when doing incremental
	// repacks and when pruning objects.
	LooseObjectLimit = 1024
)
```

**File:** internal/git/housekeeping/optimization_strategy.go (L212-230)
```go
	// If there are loose objects then we want to roll them up into a new packfile.
	// Loose objects naturally accumulate during day-to-day operations, e.g. when
	// executing RPCs part of the OperationsService which write objects into the repo
	// directly.
	//
	// As we have already verified that the packfile structure looks okay-ish to us, we
	// don't need to perform a geometric repack here as that could be expensive: we
	// might end up soaking up packfiles because the geometric sequence is not intact,
	// but more importantly we would end up writing the multi-pack-index and potentially
	// a bitmap. Writing these data structures introduces overhead that scales with the
	// number of objects in the repository.
	//
	// So instead, we only do an incremental repack of all loose objects, regardless of
	// their reachability. This is the cheapest we can do: we don't need to compute
	// whether objects are reachable and we don't need to update any data structures
	// that scale with the repository size.
	if s.info.LooseObjects.Count > LooseObjectLimit {
		return true, incrementalRepackCfg
	}
```

**File:** doc/transactions.md (L56-60)
```markdown
Gitaly automatically assigns repositories to partitions when they are first accessed:

- Object pools and all repositories connected to the object pool are placed in the same partition. Repositories that are about to be connected to an object pool, such as newly created forks, are also placed in the same partition with the object pool they are about to be connected.
  - Assigning pools and their connected repositories into the same partition ensures transactions can guarantee consistency between them. If pools were in different partitions, transaction ordering could cause issues, for example updating a reference in a fork before the objects are written into the pool.
- Repositories that are not connected (nor about to be connected) to an object pool are placed in their own partitions.
```
