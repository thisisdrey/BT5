### Title
Unbounded accumulation of dangling references in shared object pools enables force-push based DoS of pool housekeeping - ([File: internal/git/objectpool/fetch.go])

### Summary
Any user with force-push access to a repository that is a member of a Gitaly object pool (i.e. any fork in a fork network) can cheaply and repeatedly generate garbage objects that Gitaly is forced to permanently keep alive as `refs/dangling/$OID` references in the shared pool repository. Because Gitaly can never determine that a dangling object is truly unused by any pool member, it never removes these references. Their number grows without bound in proportion to attacker-controlled force-push activity, degrading every subsequent `FetchIntoObjectPool` housekeeping run and repack of the pool for the whole fork network — a DoS on a shared resource caused entirely by ordinary, unprivileged git operations.

### Finding Description
`FetchFromOrigin` (invoked by the `FetchIntoObjectPool` RPC) fetches from a pool member into the pool and then calls `rescueDanglingObjects`, which runs `git fsck --connectivity-only --dangling` and creates a permanent `refs/dangling/<oid>` reference for every dangling object it finds: [1](#0-0) 

The pruning step that runs before the fetch (`pruneReferences`) is forced to enable pruning of the pool's own remote-tracking refs in order to avoid D/F conflicts, which is exactly what produces the dangling objects that then get "rescued": [2](#0-1) 

The design rationale and known consequence are explicitly documented: [3](#0-2) 

The root cause is that Gitaly has no reliable way to prove an object in a pool is unreferenced by every pool member, so it must treat every dangling object as permanently reachable via a dedicated ref. Any ordinary, unprivileged user who can force-push (or otherwise force-update refs) into any pool member repository can trigger this path repeatedly: each force push that rewrites history in a pool member creates new unreferenced objects in the pool once `FetchIntoObjectPool` next runs, and each of these accumulates as a permanent, non-removable reference. There is no limit on the number of dangling refs a member can cause to be created, and there is no cleanup mechanism — the documentation itself states "there is no point in time where it's safe to delete objects from the object pool."

This closely parallels the reported bug class: a cheap, attacker-controlled action creates entries (there: zero-value offers in `s_offersToUpkeep`; here: `refs/dangling/*` entries in the pool) that a maintenance/housekeeping routine (`checkUpkeep`/`performUpkeep` there; `FetchIntoObjectPool`/repack here) must always account for, and that never get removed, degrading the routine's performance and, at scale, its usability for legitimate members of the shared resource.

### Impact Explanation
As dangling refs accumulate:
- `git-for-each-ref`/`git fsck` and `git-pack-refs` operations run during every `FetchIntoObjectPool` housekeeping cycle become more expensive, as documented ("Fetches into the object pool and repacking of references can thus become quite expensive").
- Because the packfile generation uses delta islands limited to `refs/heads`/`refs/tags`, dangling refs still bloat the object graph considered by `git-repack`, degrading repack efficiency/size for the *entire* fork network sharing the pool, not just the attacker's fork.
- Because the pool is shared infrastructure, this is a resource-level DoS affecting all repositories linked to the pool, triggered by any single unprivileged member with force-push rights.

This is a medium-impact availability/performance issue rather than a direct compromise, matching the bug class's characterization as a DoS without direct economic/security benefit to the attacker.

### Likelihood Explanation
Any fork owner already has the ability to force-push and rewrite history in their own fork, which is a completely ordinary, unprivileged git operation. Triggering `FetchIntoObjectPool` repeatedly (this is normally scheduled automatically by GitLab as part of push-triggered housekeeping) requires no special access. Repeating force-pushes with distinct throwaway commits is trivial and cheap to automate, so the likelihood of a motivated pool member (or malicious fork owner) exploiting this is high, though the impact is bounded to performance degradation rather than data loss or corruption.

### Recommendation
- Cap the number of dangling references Gitaly is willing to create per housekeeping cycle/per pool, and rate-limit or reject `FetchIntoObjectPool` runs for members that are perceived to be generating excessive object churn.
- Consider record-keeping (e.g., last-referenced timestamps) that would allow eventually reclaiming dangling objects that have exceeded a grace period across all known pool members, rather than treating "permanently reachable" as the only safe state.
- Emit metrics/alerts on dangling reference counts per pool so operators can detect and mitigate abusive members before repack/housekeeping cost becomes prohibitive.

### Proof of Concept
1. Create an object pool and link a repository `A` as a member (`CreateObjectPool` + `LinkRepositoryToObjectPool`).
2. As the unprivileged owner of `A`, repeatedly: push a new commit to a branch, then force-push a different unrelated commit to the same branch — this is standard git operation available to any writer.
3. Each time `FetchIntoObjectPool` runs afterward (as in `TestFetchFromOrigin_dangling`, which demonstrates the exact mechanic of dangling objects being converted into permanent `refs/dangling/<oid>` refs), the previously-referenced objects from the overwritten branch tip become dangling in the pool and are rescued into permanent refs: [4](#0-3) 
4. Repeating this loop indefinitely grows `refs/dangling/*` in the pool without bound, with no code path ever removing them, degrading `git-repack`/`git-pack-refs` cost for every member of the pool.

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
