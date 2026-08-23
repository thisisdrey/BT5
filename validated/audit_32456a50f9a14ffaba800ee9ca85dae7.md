### Title
Unbounded Growth of `refs/dangling/*` in Shared Object Pools Enables Fork-Network DoS via Uncontrolled Housekeeping Cost - ([File: internal/git/objectpool/fetch.go])

### Summary
Every time `FetchIntoObjectPool` runs against an object pool, any objects that become unreferenced (because a pool member force-updated a branch/tag) are permanently kept alive by creating a `refs/dangling/$OID` reference. These dangling references are **never removed** — only ever added — so the pool's reference set grows monotonically with every force-push cycle performed by *any* member of the fork network, even though the underlying capital (the pushed commit) can be discarded and recreated indefinitely by the attacker at no cost to themselves.

### Finding Description
`ObjectPool.FetchFromOrigin` fetches from a pool member into the shared pool repository and then calls `rescueDanglingObjects`, which scans `git fsck --dangling` output and unconditionally creates a `refs/dangling/<oid>` reference for every dangling object found: [1](#0-0) 

The design doc explicitly documents that this operation is additive with no compaction step: pruning old references is intentionally *not* done for correctness reasons (D/F ref conflicts across pool members), forcing every unreferenced object created by a force-update to be preserved forever via a new dangling reference: [2](#0-1) [3](#0-2) 

Structurally, this mirrors the reported bug class exactly:
- An append-only record (`refs/dangling/*` reference set, analogous to `TrackerAccount.issuances`) is created on every "inbound" event (a fetch that discovers newly-unreferenced objects following a member's force-push).
- The record is never pruned on the corresponding "outbound"/reclaim action (there is no code path that ever deletes a `refs/dangling/*` reference; `git prune` on the pool is explicitly guarded against since pool objects must never be pruned, per `HeuristicalOptimizationStrategy.ShouldPruneObjects` in `internal/git/housekeeping/optimization_strategy.go`).
- The attacker's cost to trigger a new record is trivial and repeatable (each force-push cycle recycles the same branch name/content), while the party who pays the compounding cost — the shared object pool, its housekeeping compute, and its repack/fsck time — is not the attacker.

An unprivileged user who owns any repository that is a member of an object pool (i.e., any fork in GitLab's deduplication network) can force-push the same branch back and forth, replacing the pointed-to commit each time. Each such force-push, once picked up by the next `FetchIntoObjectPool` run (which GitLab schedules as ordinary housekeeping for fork networks, not an admin-only action), causes the previously-referenced commit/tree/blob objects in the pool to become dangling and receive a permanent `refs/dangling/<oid>` reference: [4](#0-3) 

The doc explicitly acknowledges the resulting cost escalation on the shared pool: [5](#0-4) 

Because `logStats` and later repacks/`git-fsck` invocations must enumerate the entire `refs/dangling/` namespace on every future `FetchFromOrigin` call, the CPU/IO cost of every future housekeeping run for the *whole fork network* scales with the cumulative number of force-push cycles ever performed by any single unprivileged fork owner: [6](#0-5) 

### Impact Explanation
This is a resource-exhaustion/DoS vector against a shared resource funded and processed by the platform rather than the attacker:
1. Every pool member's fork network shares one object pool; a single malicious/careless fork owner repeatedly force-pushing to their own fork inflates the *pool's* reference count and loose-object count indefinitely.
2. Because pool repositories are explicitly exempted from object pruning (`ShouldPruneObjects` returns `false` for `IsObjectPool`), there is no self-healing mechanism — growth is permanent.
3. Escalating reference count increases the cost (CPU time, I/O, transaction/vote overhead per reference via `updateref`) of every subsequent `FetchIntoObjectPool`, `git-pack-refs`, `git-repack`, and `git-fsck` invocation on the pool, degrading housekeeping for the entire fork network (all forks share the same pool), and in the worst case can push individual fetch/housekeeping operations toward Gitaly's compute/time limits for that repository, causing a liveness failure for legitimate fork operations (analogous to the destination-identity bridge flow becoming stuck in the original report).
4. Unlike the original report, no rent/lamports are literally spent by a third party, but the compute/storage cost imposed on Gitaly's housekeeping pipeline (and eventually disk usage) plays the same structural role as the relayer's rent in the source report — an externalized, unbounded, unrecoverable cost driven by cheap, repeatable attacker action.

### Likelihood Explanation
Any authenticated user who can push force-updates to a repository that is (or becomes) a pool member — the common case for any GitLab fork — can trigger this at will, with no special privilege beyond ordinary push access to their own fork. The only prerequisite is that periodic `FetchIntoObjectPool` housekeeping runs, which is standard, non-privileged, automatic behavior in the fork/object-pool lifecycle, not an admin-triggered action.

### Recommendation
Bound the growth of `refs/dangling/*`: introduce a periodic reconciliation step that removes dangling references whose referenced object is no longer reachable from *any* live pool member (e.g., tracked via a per-member reachability bitmap/set with a grace period, similar to how `LooseObjectLimit`/stale-object grace periods are used elsewhere in `internal/git/housekeeping/optimization_strategy.go`), or cap/rate-limit the number of dangling references retained per pool and alert/refuse further growth once a threshold is exceeded, consistent with the recommendation in the source report to prune matured/stale entries before appending new ones.

### Proof of Concept
Conceptual reproduction using existing Gitaly test scaffolding (`internal/git/objectpool/fetch_test.go`):
1. Create an object pool and link a member repository to it (`setupObjectPoolWithCfg`).
2. Repeat N times: force-push/overwrite the same branch in the member repository with a new commit (as `TestFetchFromOrigin_refUpdates` already does for a single cycle), then call `pool.FetchFromOrigin`.
3. After each cycle, enumerate `refs/dangling/` in the pool (`git for-each-ref refs/dangling/`) as done in `TestFetchFromOrigin_dangling` and observe the count strictly increasing by one entry per cycle with no removal, mirroring the reported `issuances.length` growth pattern exactly: [7](#0-6) [8](#0-7)

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

**File:** internal/git/objectpool/fetch.go (L275-337)
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
```

**File:** internal/git/objectpool/fetch.go (L357-415)
```go
func (o *ObjectPool) logStats(ctx context.Context, when string) error {
	fields := log.Fields{
		"when": when,
	}

	repoInfo, err := stats.RepositoryInfoForRepository(ctx, o.Repo)
	if err != nil {
		return fmt.Errorf("deriving repository info: %w", err)
	}
	fields["repository_info"] = repoInfo

	forEachRef, err := o.Repo.Exec(ctx, gitcmd.Command{
		Name:  "for-each-ref",
		Flags: []gitcmd.Option{gitcmd.Flag{Name: "--format=%(objecttype)%00%(refname)"}},
		Args:  []string{"refs/"},
	}, gitcmd.WithSetupStdout())
	if err != nil {
		return fmt.Errorf("spawning for-each-ref: %w", err)
	}

	var danglingTypes, normalTypes referencedObjectTypes
	scanner := bufio.NewScanner(forEachRef)
	for scanner.Scan() {
		objectType, refname, found := bytes.Cut(scanner.Bytes(), []byte{0})
		if !found {
			continue
		}

		types := &normalTypes
		if bytes.HasPrefix(refname, []byte(danglingObjectNamespace)) {
			types = &danglingTypes
		}

		switch {
		case bytes.Equal(objectType, []byte("blob")):
			types.Blobs++
		case bytes.Equal(objectType, []byte("commit")):
			types.Commits++
		case bytes.Equal(objectType, []byte("tag")):
			types.Tags++
		case bytes.Equal(objectType, []byte("tree")):
			types.Trees++
		}
	}

	if err := scanner.Err(); err != nil {
		return fmt.Errorf("scanning references: %w", err)
	}
	if err := forEachRef.Wait(); err != nil {
		return fmt.Errorf("waiting for for-each-ref: %w", err)
	}

	fields["references.dangling"] = danglingTypes
	fields["references.normal"] = normalTypes

	o.logger.WithFields(fields).InfoContext(ctx, "pool dangling ref stats")

	return nil
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

**File:** internal/gitaly/service/objectpool/fetch_into_object_pool.go (L16-32)
```go
func (s *server) FetchIntoObjectPool(ctx context.Context, req *gitalypb.FetchIntoObjectPoolRequest) (*gitalypb.FetchIntoObjectPoolResponse, error) {
	if err := validateFetchIntoObjectPoolRequest(req); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	objectPool, err := objectpool.FromProto(ctx, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.housekeepingManager, req.GetObjectPool())
	if err != nil {
		return nil, structerr.NewInvalidArgument("object pool invalid: %w", err)
	}

	origin := s.localRepoFactory.Build(req.GetOrigin())

	if err := objectPool.FetchFromOrigin(ctx, origin, func(repo *gitalypb.Repository) *localrepo.Repo {
		return s.localRepoFactory.Build(repo)
	}); err != nil {
		return nil, structerr.NewInternal("%w", err)
	}
```

**File:** internal/git/objectpool/fetch_test.go (L33-91)
```go
func TestFetchFromOrigin_dangling(t *testing.T) {
	ctx := testhelper.Context(t)
	testWithAndWithoutTransaction(t, ctx, func(t *testing.T, cfg config.Cfg, newLocalRepo LocalRepoFactory) {
		pool, repo := setupObjectPoolWithCfg(t, ctx, cfg)
		poolPath := gittest.RepositoryPath(t, ctx, pool)
		repoPath := gittest.RepositoryPath(t, ctx, repo)

		// Write some reachable objects into the object pool member and fetch them into the pool.
		blobID := gittest.WriteBlob(t, cfg, repoPath, []byte("contents"))
		treeID := gittest.WriteTree(t, cfg, repoPath, []gittest.TreeEntry{
			{Mode: "100644", OID: blobID, Path: "reachable"},
		})
		commitID := gittest.WriteCommit(t, cfg, repoPath,
			gittest.WithTree(treeID),
			gittest.WithBranch("master"),
		)
		require.NoError(t, pool.FetchFromOrigin(ctx, repo, newLocalRepo))

		// We now write a bunch of objects into the object pool that are not referenced by anything.
		// These are thus "dangling".
		unreachableBlob := gittest.WriteBlob(t, cfg, poolPath, []byte("unreachable"))
		unreachableTree := gittest.WriteTree(t, cfg, poolPath, []gittest.TreeEntry{
			{Mode: "100644", OID: blobID, Path: "unreachable"},
		})
		unreachableCommit := gittest.WriteCommit(t, cfg, poolPath,
			gittest.WithMessage("unreachable"),
			gittest.WithTree(treeID),
		)
		unreachableTag := gittest.WriteTag(t, cfg, poolPath, "unreachable", commitID.Revision(), gittest.WriteTagConfig{
			Message: "unreachable",
		})
		// `WriteTag()` automatically creates a reference and thus makes the annotated tag
		// reachable. We thus delete the reference here again.
		gittest.Exec(t, cfg, "-C", poolPath, "update-ref", "-d", "refs/tags/unreachable")

		// git-fsck(1) should report the newly created unreachable objects as dangling.
		fsckBefore := gittest.Exec(t, cfg, "-C", poolPath, "fsck", "--connectivity-only", "--dangling")
		require.ElementsMatch(t, []string{
			fmt.Sprintf("dangling blob %s", unreachableBlob),
			fmt.Sprintf("dangling tag %s", unreachableTag),
			fmt.Sprintf("dangling commit %s", unreachableCommit),
			fmt.Sprintf("dangling tree %s", unreachableTree),
		}, strings.Split(text.ChompBytes(fsckBefore), "\n"))

		// We expect this second run to convert the dangling objects into non-dangling objects.
		require.NoError(t, pool.FetchFromOrigin(ctx, repo, newLocalRepo))

		// Each of the dangling objects should have gotten a new dangling reference.
		danglingRefs := gittest.Exec(t, cfg, "-C", poolPath, "for-each-ref", "--format=%(refname) %(objectname)", "refs/dangling/")
		require.ElementsMatch(t, []string{
			fmt.Sprintf("refs/dangling/%[1]s %[1]s", unreachableBlob),
			fmt.Sprintf("refs/dangling/%[1]s %[1]s", unreachableTree),
			fmt.Sprintf("refs/dangling/%[1]s %[1]s", unreachableTag),
			fmt.Sprintf("refs/dangling/%[1]s %[1]s", unreachableCommit),
		}, strings.Split(text.ChompBytes(danglingRefs), "\n"))
		// And git-fsck(1) shouldn't report the objects as dangling anymore.
		require.Empty(t, gittest.Exec(t, cfg, "-C", poolPath, "fsck", "--connectivity-only", "--dangling"))
	})
}
```

**File:** internal/git/objectpool/fetch_test.go (L170-215)
```go
func TestFetchFromOrigin_refUpdates(t *testing.T) {
	ctx := testhelper.Context(t)
	testWithAndWithoutTransaction(t, ctx, func(t *testing.T, cfg config.Cfg, newLocalRepo LocalRepoFactory) {
		pool, repo := setupObjectPoolWithCfg(t, ctx, cfg)
		repoPath, err := repo.Path(ctx)
		require.NoError(t, err)

		poolPath := gittest.RepositoryPath(t, ctx, pool)

		// Seed the pool member with some preliminary data.
		oldRefs := map[string]git.ObjectID{}
		oldRefs["heads/csv"] = gittest.WriteCommit(t, cfg, repoPath, gittest.WithBranch("csv"), gittest.WithMessage("old"))
		oldRefs["tags/v1.1.0"] = gittest.WriteTag(t, cfg, repoPath, "v1.1.0", oldRefs["heads/csv"].Revision())

		// We now fetch that data into the object pool and verify that it exists as expected.
		require.NoError(t, pool.FetchFromOrigin(ctx, repo, newLocalRepo))
		for ref, oid := range oldRefs {
			require.Equal(t, oid, gittest.ResolveRevision(t, cfg, poolPath, "refs/remotes/origin/"+ref))
		}

		// Next, we force-overwrite both old references with new objects.
		newRefs := map[string]git.ObjectID{}
		newRefs["heads/csv"] = gittest.WriteCommit(t, cfg, repoPath, gittest.WithBranch("csv"), gittest.WithMessage("new"))
		newRefs["tags/v1.1.0"] = gittest.WriteTag(t, cfg, repoPath, "v1.1.0", newRefs["heads/csv"].Revision(), gittest.WriteTagConfig{
			Force: true,
		})

		// Create a bunch of additional references. This is to trigger OptimizeRepository to indeed
		// repack the loose references as we expect it to in this test. It's debatable whether we
		// should test this at all here given that this is business of the housekeeping package. But
		// it's easy enough to do, so it doesn't hurt.
		for i := 0; i < 32; i++ {
			branchName := fmt.Sprintf("branch-%d", i)
			newRefs["heads/"+branchName] = gittest.WriteCommit(t, cfg, repoPath,
				gittest.WithMessage(strconv.Itoa(i)),
				gittest.WithBranch(branchName),
			)
		}

		// Now we fetch again and verify that all references should have been updated accordingly.
		require.NoError(t, pool.FetchFromOrigin(ctx, repo, newLocalRepo))
		for ref, oid := range newRefs {
			require.Equal(t, oid, gittest.ResolveRevision(t, cfg, poolPath, "refs/remotes/origin/"+ref))
		}
	})
}
```
