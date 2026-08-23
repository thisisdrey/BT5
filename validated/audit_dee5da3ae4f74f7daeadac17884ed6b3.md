## Title
Unbounded growth of `refs/dangling/*` references in a shared object pool allows any pool member to degrade or DoS pool housekeeping (`FetchIntoObjectPool`) for all forks sharing the pool - (File: `internal/git/objectpool/fetch.go`)

### Summary
The Convex report describes an unprivileged/administrative actor being able to make an unbounded, ever-growing collection (`extraRewards`) that a critical operation (`pullFromLocker` → `claimRewards`) is forced to iterate over in full, eventually making that operation prohibitively expensive or impossible, and thereby locking user funds. The analogous mechanism in Gitaly is the object pool's dangling-reference safeguard: ordinary, unprivileged pool members can cause an unbounded number of permanent `refs/dangling/$OID` references to accumulate in the shared object pool, which every future `FetchIntoObjectPool`/repack of that pool must account for, degrading housekeeping for every fork that shares the pool.

### Finding Description
When Gitaly performs `FetchIntoObjectPool`, it fetches `+refs/*:refs/remotes/origin/*` from a pool member (a normal repository, e.g. a fork) into the shared pool [1](#0-0) . Because it must prune stale refs to avoid D/F ref conflicts, any objects that become unreferenced as a result of the member's own force-updates/rewrites become "dangling" in the pool [2](#0-1) .

To avoid ever losing these objects (since any other pool member might still reference them), Gitaly's `rescueDanglingObjects` runs `git fsck --connectivity-only --dangling` and permanently creates one `refs/dangling/$OID` reference per dangling object found, with no cap on the number of refs created [3](#0-2) . These dangling refs are never removed, by design, because "there is no point in time where it's safe to delete objects from the object pool" [4](#0-3) .

The documentation itself acknowledges the resulting operational cost: "For busy repositories, we generate loads of dangling references... Fetches into the object pool and repacking of references can thus become quite expensive," and that the resulting packfiles from `git-repack(1)` become suboptimal because Git treats all dangling refs as reachable [5](#0-4) . Any single pool member can trigger this simply by repeatedly force-pushing/rewriting history (an entirely ordinary, unprivileged git operation) into their own fork that belongs to the pool, causing every subsequent `FetchIntoObjectPool` housekeeping run for the *whole pool* — shared by potentially many unrelated forks — to do increasing amounts of `git fsck`, ref-walking, and `git-repack` work [6](#0-5) .

This mirrors the Convex bug-class exactly: a party that is not expected to be malicious (a restricted/unprivileged actor — here, any fork owner doing routine force-pushes rather than a Convex reward-manager) can unilaterally and unboundedly grow a persistent, never-pruned collection that a shared, safety-critical maintenance routine is forced to fully process, and the operators of the shared resource ("the pool", analogous to Zivoe's locker) have no way to safely clean it up afterward.

### Impact Explanation
Because `FetchIntoObjectPool` (and the repack step it triggers) operates on the shared object pool used by every fork/member linked to it, degradation caused by one member's activity affects housekeeping for all pool members, not just the attacker's own repository. As dangling refs accumulate without bound, `git-pack-refs(1)` and `git-repack(1)` runs on the pool become progressively slower and packfiles become less optimal (because Git must treat all dangling objects as reachable delta candidates) [5](#0-4) . In the worst case this can make pool housekeeping RPCs (`FetchIntoObjectPool`) take excessively long or fail/time out, effectively denying maintenance service to all repositories sharing the pool — a resource-exhaustion/DoS of a Gitaly RPC handler triggered entirely by an ordinary git push from an unprivileged fork owner.

### Likelihood Explanation
This requires no special privilege: any user who can push/force-push to a repository that is a member of an object pool (i.e., a standard fork workflow) can generate dangling objects on every rewrite, and this is entirely legitimate git usage (rebases, force-pushes, branch deletion/recreation) rather than an attack requiring code execution or credential compromise. The only "cost" to the attacker is doing normal force-pushes repeatedly, which is a low-effort, repeatable action reachable purely from ordinary push/fetch/fork flows.

### Recommendation
Introduce a bound or aging/consolidation strategy for `refs/dangling/*` entries in object pools (e.g., periodically consolidating dangling refs into a single "keep-alive" structure, or rate-limiting/monitoring growth per pool), and expose metrics/alerts so operators can detect a pool whose dangling-ref count is growing abnormally fast due to a single member's activity, consistent with the project's own acknowledgment of this cost in `doc/object_pools.md`.

### Proof of Concept
1. Create an object pool and link a fork (`repo`) to it, as in `TestFetchFromOrigin_dangling` [7](#0-6) .
2. As the fork owner (unprivileged), repeatedly write new commits/branches and then force-update/delete them in `repo`, then trigger `FetchIntoObjectPool` (as GitLab's scheduler would after each push). Each cycle leaves newly-unreferenced objects in the pool.
3. Observe that `rescueDanglingObjects` creates a new permanent `refs/dangling/$OID` ref per unreferenced object every cycle [8](#0-7) , and that these refs are never removed.
4. Repeating this at scale (many force-push cycles) grows the pool's ref count without bound, which per the project's own documentation makes future fetches into the pool and `git-repack(1)` "quite expensive" [9](#0-8) , degrading `FetchIntoObjectPool` for every other fork sharing that same pool.

### Citations

**File:** internal/git/objectpool/fetch.go (L59-67)
```go
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

**File:** internal/git/objectpool/fetch.go (L275-336)
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
```

**File:** doc/object_pools.md (L78-96)
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
```

**File:** doc/object_pools.md (L98-112)
```markdown
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

**File:** internal/git/objectpool/fetch_test.go (L33-49)
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
```
