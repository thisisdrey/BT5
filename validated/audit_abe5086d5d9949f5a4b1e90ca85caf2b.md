### Title
Object-pool disconnect hard-links defeat the `PruneUnreachableObjects` grace-period safety check, enabling premature deletion of objects that are about to become reachable - (File: internal/gitaly/service/repository/prune_unreachable_objects.go)

### Summary
`PruneUnreachableObjects` relies on a 30-minute "not recently touched" grace period as a hard safety requirement before deleting unreachable loose objects, to avoid racing with concurrent operations that are about to make those objects reachable. This is the same pattern as the DittoETH bug: a destructive/security-sensitive action is gated by an object's on-disk modification timestamp, but a legitimate code path that makes an object "newly relevant" to the repository does not refresh that timestamp, defeating the safety buffer.

### Finding Description
`PruneUnreachableObjects` explicitly documents and enforces a grace period intended as a correctness/safety invariant: [1](#0-0) 

```
// PruneUnreachableObjects prunes objects which aren't reachable from any of its references. To
// ensure that concurrently running commands do not reference those objects anymore when we execute
// the prune we enforce a grace-period: objects will only be pruned if they haven't been accessed
// for at least 30 minutes.
```

The cutoff is computed once from wall-clock time and passed straight through to both loose-object pruning and cruft-pack repacking: [2](#0-1) 

This "grace period" logic depends on each loose object's on-disk modification time (`mtime`) faithfully reflecting how recently the object became relevant to *this* repository — exactly like DittoETH's `updatedAt` was relied upon to prove a short record was not recently touched before a redemption dispute.

However, Gitaly's object-pool disconnection flow deliberately re-uses objects from an existing pool by hard-linking them into the member repository rather than copying them, as documented in the object pools design doc: [3](#0-2) 

```
Removing a member from an object pool is slightly more involved, as members of
an object pool members will miss objects which are only part of the object pool.
It is thus not as simple as removing `objects/info/alternates`, as that would
leave behind a corrupt repository. Instead, Gitaly hard-links all objects which
are part of the object pool into the dissociating member first and removes the
alternate afterwards.
```

Because a hard link shares the same inode as the source file, the newly-created link in the member repository inherits the **original** file's `mtime` from whenever the object was first written into the pool — which can be arbitrarily old (pools are long-lived, shared across many forks). The object is brand-new to the member repository's object database (it did not exist there before disconnection), yet its timestamp will already appear to satisfy (i.e., predate) the 30-minute "hasn't been accessed" cutoff the moment the hard link is created.

### Impact Explanation
If `PruneUnreachableObjects` (a `MAINTENANCE`-scoped RPC, reachable through normal housekeeping/maintenance scheduling on a repository an ordinary user controls) runs concurrently with, or shortly after, an object-pool disconnect (`DisconnectGitAlternates`) that has just hard-linked objects into the member repo but before those objects are referenced by a committed ref update, the grace-period check will not protect them: their inherited old `mtime` makes them look "stale" immediately, even though they are unreachable-but-about-to-become-reachable objects central to the disconnect operation's correctness guarantee. This mirrors the DittoETH root cause precisely — a state-mutating operation (`decreaseCollateral` / hard-linking objects into a repo) fails to refresh the timestamp field a downstream security/safety check depends on, letting the check be satisfied "for the wrong reason." The result here is potential repository corruption (missing objects, broken refs) for an ordinary repository owner's fork/pool member, not merely a benign race — which is the class of impact the grace period was explicitly designed to prevent.

### Likelihood Explanation
Triggering `DisconnectGitAlternates` requires only ordinary access to a repository that is a pool member (e.g., an owner making a fork private, a routine GitLab operation), and `PruneUnreachableObjects`/`OptimizeRepository` are maintenance RPCs that run automatically via housekeeping scheduling on the same repository. No elevated privileges, malicious peers, or leaked credentials are needed — only a race between two RPCs that Gitaly itself schedules against the same repository, both reachable by a legitimate fork owner. The race window (between hard-linking objects and the connectivity check/alternates removal completing) may be narrow, so likelihood should be assessed as a timing-dependent race rather than deterministic, but the underlying mechanism (stale-looking mtime immediately after hard-linking) is deterministic and always present.

### Recommendation
When hard-linking objects out of an object pool into a dissociating member (`internal/git/objectpool/disconnect.go`), explicitly update (`os.Chtimes`) each newly hard-linked object's `mtime` to "now" so the `PruneUnreachableObjects`/cruft-repack grace-period check reflects the object's true relevance to the member repository, rather than inheriting an arbitrary historical timestamp from the shared pool inode. Alternatively, serialize `PruneUnreachableObjects`/repacking against in-flight `DisconnectGitAlternates` operations on the same repository (e.g., via the existing housekeeping "already executing" lock) so the two can never race.

### Proof of Concept
Conceptual reproduction (not executed, since this is a static/code-review analog finding):
1. Create an object pool and link a member repository to it; ensure the pool has objects that are older than 30 minutes (trivially true for any long-lived pool).
2. Call `DisconnectGitAlternates` on the member; instrument/pause it right after the hard-linking step (`internal/git/objectpool/disconnect.go`) but before the connectivity check/ref update completes.
3. Concurrently invoke `PruneUnreachableObjects` (or `OptimizeRepository` with cruft repacking) against the same member repository.
4. Because the freshly hard-linked objects carry the pool's old `mtime`, they satisfy `expireBefore := time.Now().Add(-30*time.Minute)` immediately and are eligible for pruning/cruft-expiration, potentially removing objects the disconnect operation still needs, leading to a failed or corrupted `fsck` on the member repository.

Note: I was not able to directly inspect the exact internal `git-prune`/`git-repack --cruft-expiration` mtime-comparison implementation invoked by `housekeeping.PruneObjects`/`housekeeping.RepackObjects` within the available index; this analysis relies on Git's well-documented behavior that loose-object expiration is based on the loose object file's `mtime`, combined with the confirmed Gitaly-side facts above (the 30-minute buffer's stated purpose, and the documented hard-link reuse in object-pool disconnection). A full Devin session with repository access would be needed to empirically confirm the race window and mtime propagation.

### Citations

**File:** internal/gitaly/service/repository/prune_unreachable_objects.go (L16-19)
```go
// PruneUnreachableObjects prunes objects which aren't reachable from any of its references. To
// ensure that concurrently running commands do not reference those objects anymore when we execute
// the prune we enforce a grace-period: objects will only be pruned if they haven't been accessed
// for at least 30 minutes.
```

**File:** internal/gitaly/service/repository/prune_unreachable_objects.go (L70-88)
```go
	expireBefore := time.Now().Add(-30 * time.Minute)

	// We need to prune loose unreachable objects that exist in the repository.
	if err := housekeeping.PruneObjects(ctx, repo, housekeeping.PruneObjectsConfig{
		ExpireBefore: expireBefore,
	}); err != nil {
		return nil, structerr.NewInternal("pruning objects: %w", err)
	}

	// But we also have to prune unreachable objects part of cruft packs. The only way to do
	// that is to do a full repack. So unfortunately, this is quite expensive.
	if err := housekeeping.RepackObjects(ctx, repo, housekeepingcfg.RepackObjectsConfig{
		Strategy:            housekeepingcfg.RepackObjectsStrategyFullWithCruft,
		WriteMultiPackIndex: true,
		WriteBitmap:         len(repoInfo.Alternates.ObjectDirectories) == 0,
		CruftExpireBefore:   expireBefore,
	}); err != nil {
		return nil, structerr.NewInternal("repacking objects: %w", err)
	}
```

**File:** doc/object_pools.md (L35-43)
```markdown
Removing a member from an object pool is slightly more involved, as members of
an object pool members will miss objects which are only part of the object pool.
It is thus not as simple as removing `objects/info/alternates`, as that would
leave behind a corrupt repository. Instead, Gitaly hard-links all objects which
are part of the object pool into the dissociating member first and removes the
alternate afterwards. In order to check whether the operation succeeded, Gitaly
now runs `git-fsck(1)` to check for missing objects. If there are none, the
dissociation has succeeded. Otherwise, it will fail and re-add the alternates
file.
```
