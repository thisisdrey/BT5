### Title
Unbounded growth of `refs/dangling/*` object-pool references causes unbounded-cost `FetchIntoObjectPool` housekeeping, DoS-ing all fork-network members - ([File: internal/git/objectpool/fetch.go])

### Summary
The bug in the report is a state-growth griefing DoS: an unprivileged actor performs cheap operations that inflate a shared, per-victim array with no effective size limit, so that a later routine operation on that state becomes prohibitively (or permanently) expensive/unusable for the victim. Gitaly's object-pool "dangling references" mechanism has the same structural weakness: any member of a fork network can, via ordinary force-pushes, cause the shared pool repository to accumulate an unbounded number of `refs/dangling/$OID` references, which are then rewalked in full on every subsequent `FetchIntoObjectPool` housekeeping run and by `git-repack`/`git-fsck` invoked against the pool, degrading or effectively DoS-ing the shared operation for all members of that fork network (not just the actor).

### Finding Description
`ObjectPool.FetchFromOrigin` (`internal/git/objectpool/fetch.go:31-142`) is the RPC-level operation behind `FetchIntoObjectPool` (`proto/objectpool.proto:80-88`), reachable by any pool member (i.e. any user who owns a fork that is linked to the shared pool) with normal fetch/push privileges — no admin/operator role required, matching the "ordinary user's push/fetch" reachability required by the validation rules.

The flow:
1. `pruneReferences` (`fetch.go:144-273`) force-deletes references in the pool that were deleted/force-updated in the origin member, per `doc/object_pools.md:78-85`: "any other member of the pool may still use any of those unreferenced objects. Deleting them would thus potentially cause corrupt repositories."
2. To avoid ever losing an object, `rescueDanglingObjects` (`fetch.go:285-348`) runs `git fsck --connectivity-only --dangling` over the *entire pool* and, for every dangling object found, creates a permanent reference `refs/dangling/$OID` (`fetch.go:275, 328-336`). These references are never deleted — `doc/object_pools.md:87-91`: "there is no point in time where it's safe to delete objects from the object pool... We thus must consider each object to still be referenced somewhere."
3. `doc/object_pools.md:98-103` explicitly documents the resulting cost: "For busy repositories, we generate loads of dangling references... Fetches into the object pool and repacking of references can thus become quite expensive."

Because any pool member can force-push new commits/branches and then delete or rewrite them, each cycle can create new dangling objects that get pinned forever via `refs/dangling/*`. There is no cap (analogous to `MAX_DELEGATES`) on the number of dangling references that can accumulate in the pool. Every subsequent `FetchIntoObjectPool` call — which is meant to be routine housekeeping run by any legitimate pool member — must re-run `git fsck --dangling` and `git-repack` (`doc/object_pools.md:69-76`) over the ever-growing dangling-ref set, and `logStats`'s `for-each-ref` walk (`fetch.go:368-407`) also scans this entire, unboundedly growing namespace on every fetch. As the count of dangling refs grows without bound, the CPU/IO/memory cost of `FetchIntoObjectPool` and other maintenance actions grows correspondingly, eventually causing the RPC to blow past Gitaly's operation timeouts or per-RPC resource limits, denying the operation to every member of the fork network — including the one who did nothing wrong — exactly mirroring the "cheaper to delegate from a short list to a long list, victim can no longer withdraw/transfer" griefing pattern in the report (an attacker's inexpensive push/force-push action inflates shared state that a victim, or any pool member, is forced to pay for later).

### Impact Explanation
`FetchIntoObjectPool` is the sole mechanism that keeps object pools (used to deduplicate fork-network objects) up to date; if it times out or becomes unacceptably slow, housekeeping stalls for every repository sharing that pool, degrading fetch/clone/push efficiency and repository health for the whole fork network. This is a resource-exhaustion DoS reachable by any unprivileged member with fetch/push access to a forked repository — no privileged role required.

### Likelihood Explanation
Likelihood is moderate: it requires deliberate, repeated force-pushing (create-then-delete/rewrite refs) by a pool member over time to accumulate a large number of dangling objects, similar to the "attacker creates a new address/lock and delegates repeatedly" pattern in the report. Because `refs/dangling/*` entries are permanent by design and each `FetchIntoObjectPool` invocation walks the full pool, the cost is cumulative and one-directional (never shrinks), so a patient attacker with ordinary fork/push access can reliably grow the cost over multiple fetch cycles.

### Recommendation
- Introduce a cap or eviction/consolidation strategy for `refs/dangling/*` entries (e.g., group multiple dangling objects behind a bounded number of "keep-alive" references, such as a single reflog-like or packed representation, instead of one ref per dangling object) so the walk cost in `rescueDanglingObjects` and `logStats` does not scale unboundedly with attacker-controlled churn.
- Rate-limit or monitor per-member contributions of dangling objects to the pool, and consider reclaiming/consolidating dangling refs contributed by a specific member during a grace period.
- Bound the amount of work `FetchIntoObjectPool` performs per invocation (e.g., process dangling objects in batches with a maximum count/time budget) so a single call cannot be forced into unbounded runtime, and surface `RESOURCE_EXHAUSTED` early instead of running until timeout.

### Proof of Concept
1. Create an object pool and link two member repositories A (victim) and B (attacker) to it, per `doc/object_pools.md` lifecycle.
2. As the attacker (owner of B, or any user with push access to a repo participating in the pool network), repeatedly: push a new branch with a large number of objects, then force-push to rewrite/delete it, across many cycles — each cycle is an ordinary, unprivileged git push.
3. Each time `FetchIntoObjectPool` runs (`internal/git/objectpool/fetch.go:31`), `pruneReferences` deletes the stale refs from the pool and `rescueDanglingObjects` (`fetch.go:285`) walks `git fsck --dangling` and creates a new permanent `refs/dangling/$OID` for every object made unreachable, growing `refs/dangling/*` monotonically.
4. After enough cycles, measure wall-clock time and resource usage of subsequent `FetchIntoObjectPool` calls (which include `git fsck --dangling`, `git-pack-refs`, and full `git-repack`, per `doc/object_pools.md:69-76`) and observe growth proportional to the accumulated dangling-ref count, eventually exceeding Gitaly's RPC timeout/resource limits and denying housekeeping to the entire pool (including victim A), while requiring only unprivileged push access from the attacker. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** internal/git/objectpool/fetch.go (L49-67)
```go
	// Ideally we wouldn't want to prune old references at all so that we can keep alive all
	// objects without having to create loads of dangling references. But unfortunately keeping
	// around old refs can lead to D/F conflicts between old references that have since
	// been deleted in the pool and new references that have been added in the pool member we're
	// fetching from. E.g. if we have the old reference `refs/heads/branch` and the pool member
	// has replaced that since with a new reference `refs/heads/branch/conflict` then
	// the fetch would now always fail because of that conflict.
	//
	// Due to the lack of an alternative to resolve that conflict we are thus forced to enable
	// pruning. This isn't too bad given that we know to keep alive the old objects via dangling
	// refs anyway, but I'd sleep easier if we didn't have to do this.
	//
	// Note that we need to perform the pruning separately from the fetch: if the fetch is using
	// `--atomic` and `--prune` together then it still wouldn't be able to recover from the D/F
	// conflict. So we first to a preliminary prune that only prunes refs without fetching
	// objects yet to avoid that scenario.
	if err := o.pruneReferences(ctx, origin); err != nil {
		return fmt.Errorf("pruning references: %w", err)
	}
```

**File:** internal/git/objectpool/fetch.go (L275-348)
```go
const danglingObjectNamespace = "refs/dangling/"

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

	if err := scanner.Err(); err != nil {
		return err
	}

	if err := fsck.Wait(); err != nil {
		return fmt.Errorf("git fsck: %w, stderr: %q", err, stderr.String())
	}

	return updater.Commit()
}
```

**File:** doc/object_pools.md (L78-112)
```markdown
## Dangling Objects

When fetching from pool members into the object pool, then any force-updated
references may cause objects in the pool to not be referenced anymore. For
normal repositories, it is perfectly fine to delete those references after a
certain time. In the context of object pools, any other member of the pool may
still use any of those unreferenced objects. Deleting them would thus
potentially cause corrupt repositories.

This issue is kind of unsolvable: there is no point in time where it's safe to
delete objects from the object pool, as we do not know which repositories may be
linked to it. And even if we knew, we cannot determine all references of all
repositories at once in a race-free manner. We thus must consider each object to
still be referenced somewhere.

As a safeguard to not lose any objects by accident, we thus create dangling
references in the object pool after the fetch in `FetchIntoObjectPool`. For each
dangling object, a reference `refs/dangling/$OID` is created which points into
the object. This assures that each object is still referenced.

Having unreachable objects kept alive in this fashion does have its problems:

- For busy repositories, we generate loads of dangling references. While these
  references [cannot be seen by clients](#references), they are seen when
  performing housekeeping tasks on the object pool itself. Fetches into the
  object pool and repacking of references can thus become quite expensive.

- Keeping dangling references alive makes Git consider them as reachable. While
  this is the exact effect we want to achieve, it will also cause Git to
  generate packfiles which may use such objects as delta bases which would under
  normal circumstances be considered as unreachable. The resulting packfile is
  thus potentially suboptimal. Gitaly works around this issue by using a delta
  island for `refs/heads/` and `refs/tags/`. This can only be considered a
  best-effort strategy, as it only considers a single object pool member's
  reachability while ignoring potential reachability by any other pool member.
```

**File:** proto/objectpool.proto (L80-88)
```text
  // FetchIntoObjectPool fetches all references from a pool member into an object pool so that
  // objects shared between this repository and other pool members can be deduplicated. This RPC
  // will perform housekeeping tasks after the object pool has been updated to ensure that the pool
  // is in an optimal state.
  rpc FetchIntoObjectPool(FetchIntoObjectPoolRequest) returns (FetchIntoObjectPoolResponse) {
    option (op_type) = {
      op: MUTATOR
    };
  }
```
