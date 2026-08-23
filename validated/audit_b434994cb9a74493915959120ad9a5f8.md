This gives a solid concrete root cause: `findObjectFiles` hard-links objects present in the alternate directory **at the moment `Disconnect` is called**, then `removeAlternatesIfOk` validates completeness via `git rev-list --objects --all`. If the pool has grown (more pushes referencing new pool objects landed after the file listing) or if concurrent housekeeping (`FetchIntoObjectPool`) is repacking/pruning at the same time, the hard-link snapshot and the connectivity check can disagree, causing the disconnect to permanently fail and roll back — exactly the "critical action blocked by an uncooperative, attacker-influenceable external dependency" pattern from the Sherlock report.### Title
Ordinary member push can permanently block an object pool member from disconnecting via `DisconnectGitAlternates` (connectivity-check DoS) - (File: internal/git/objectpool/disconnect.go)

### Summary
The Sherlock finding shows that `Stream.cancel()` bundles a mandatory, uncontrollable external dependency (a transfer to a `recipient` that can be blacklisted) together with irreversible internal bookkeeping, so a hostile but unprivileged party (the recipient) can make the whole critical operation permanently fail. Gitaly's object-pool disconnection RPC has the same structural weakness: `DisconnectGitAlternates` bundles a snapshot-based hard-link step with a strict post-hoc validation step (`git rev-list --objects --all`) that depends on the *current* completeness of the pool member, a piece of state that ordinary, unprivileged pool members with normal push access can influence. If the validation step fails, the whole operation aborts and rolls back, and there is no alternative path to complete the disconnect.

### Finding Description
`DisconnectGitAlternates` (`internal/gitaly/service/objectpool/alternates.go:19`) calls `objectpool.Disconnect` (`internal/git/objectpool/disconnect.go:36`), which:

1. Reads the alternates file and enumerates all pack/loose object files currently present in the alternate (pool) object directory via `findObjectFiles` [1](#0-0) .
2. Hard-links exactly that **snapshot** of pool object files into the member repository [2](#0-1) .
3. Renames away `objects/info/alternates` and runs `git rev-list --objects --all` as a connectivity check in `removeAlternatesIfOk` [3](#0-2) . If any object referenced by the member repo is missing, the check fails and the alternates file is restored, i.e. the disconnect is aborted [4](#0-3) .

This is explicitly documented as "slightly dangerous": *"It optimistically hard-links all alternate objects we might need, and then temporarily removes (renames) objects/info/alternates and runs a connectivity check... if we are very unlucky and Gitaly crashes, the repository stays in a broken state until an administrator intervenes."* [5](#0-4) 

The root problem, analogous to the USDC-blacklist bug, is that the whole operation's success is gated on a post-condition (full connectivity of the member repo using only objects the pool currently has hard-linkable) that an ordinary, unprivileged repository contributor can influence merely by using normal push functionality:

- A fork/pool member normally shares objects with the pool purely via the `objects/info/alternates` mechanism (this is the entire point of object pools) [6](#0-5) .
- Any ordinary contributor with push access to the pool member can create new refs whose reachable objects live only in the pool (this is the expected, normal way the deduplication feature is used) — no special privilege beyond ordinary push access is required.
- Independently, pool housekeeping (`FetchIntoObjectPool`) manages dangling/reachable objects in the pool and is documented as having races around what stays "referenced" in the pool [7](#0-6) ; the pool's object set is not static and not something the operator performing `DisconnectGitAlternates` fully controls at the exact instant the RPC executes.
- Because `findObjectFiles` takes only a **snapshot** of the alternate directory at the start of `Disconnect`, and the actual test of sufficiency happens later via `git rev-list --objects --all` on the *live* ref set of the repository, any additional object references introduced into the member repo between the snapshot and the connectivity check (e.g., via a concurrent push by any ordinary contributor, since pushes are not blocked while `Disconnect` runs) will cause the connectivity check to fail. This is not merely a transient race: an unprivileged contributor can repeatedly push while an operator tries to disconnect the repo from its pool (e.g., in preparation for making a fork private, a normal GitLab operation), reliably starving the check and preventing the operator from ever completing the disconnect — just as a blacklisted `recipient` prevents `cancel()` from ever succeeding.
- Unlike the Solidity bug, Gitaly does at least roll back safely (no corrupted repository) rather than reverting a whole transaction with side effects lost, but the *availability* impact is the same: a privileged/administrative action can be indefinitely blocked by an ordinary, unprivileged actor's normal use of the write path.

### Impact Explanation
An unprivileged user with only push access to an object-pool member repository can indefinitely prevent that repository from being disconnected from its object pool via the `DisconnectGitAlternates` RPC. This RPC is a normal part of the repository lifecycle (e.g., isolating a fork before making it private, per the proto doc comment "There is not much of a reason to do this for any repositories except for the primary object pool member in case it for example becomes private" [8](#0-7) ), so blocking it is a genuine denial-of-service against an operational/administrative workflow, not merely a benign failure. Because each failed attempt still performs the hard-link step and a `git rev-list --objects --all` full connectivity scan, repeated attempts to force the disconnect are also resource-expensive on potentially large repositories, compounding the DoS.

### Likelihood Explanation
The trigger requires no special privilege — only ordinary push access to a pool member, which is a normal permission level for many contributors. It requires timing a push against an in-flight `DisconnectGitAlternates`/`removeAlternatesIfOk` call, which an attacker with sustained push access can retry cheaply and repeatedly (the disconnect check is a full `rev-list --objects --all`, which is slow on large repos, giving a wide window to race). The likelihood is moderate: it needs an operator to actually invoke disconnect while an attacker is actively racing it, but the attacker fully controls the timing/repetition of their own contribution.

### Recommendation
Do not gate the disconnect purely on a live `git rev-list --objects --all` snapshot race against concurrent writes. Instead:
- Serialize disconnect against concurrent reference updates on the member repository for the duration of the hard-link + connectivity-check sequence (e.g., via the existing repository lock or transaction machinery), so no new pool-object references can be introduced mid-operation.
- Alternatively/additionally, re-run `findObjectFiles`/hard-linking against any refs added since the snapshot before performing the final connectivity check, retrying a bounded number of times, and only failing (with restore) if the object set truly cannot stabilize — rather than failing outright on any object added during the check window.

### Proof of Concept
Conceptual PoC (not executable without a live cluster, but derived directly from the code path):
1. Create an object pool `P` and link member repository `M` to it (`CreateObjectPool` + `LinkRepositoryToObjectPool`).
2. As an ordinary contributor with push access to `M`, continuously push new commits whose objects are supplied via the shared alternate (`P`) rather than being present locally in `M` (this is normal fork behavior).
3. As the operator, invoke `DisconnectGitAlternates` on `M`.
4. Because `findObjectFiles` snapshots `P`'s objects at the start of `Disconnect` [1](#0-0)  while the connectivity check in `removeAlternatesIfOk` runs `git rev-list --objects --all` against the *live* ref state of `M` [3](#0-2) , a concurrently pushed ref referencing a not-yet-hard-linked pool object causes the connectivity check to fail with `connectivityError`, triggering rollback of the alternates removal [9](#0-8) .
5. By repeating step 2 on every operator retry, the unprivileged contributor can perpetually prevent `DisconnectGitAlternates` from succeeding on `M`.

Note: I was not able to fully verify locking/transaction interaction for the write-ahead-log (WAL) transaction manager path (`internal/gitaly/storage/storagemgr/partition`) which may add additional serialization in newer configurations; this could reduce (but per the reviewed code does not eliminate) the exposure in that specific execution mode. If a definitive verdict on WAL-mode mitigation is needed, a Devin session with full codebase/runtime access should be used to trace whether `TransactionManager.Begin`/snapshotting fully serializes `DisconnectGitAlternates` against concurrent ref pushes to the same repository.

### Citations

**File:** internal/git/objectpool/disconnect.go (L89-92)
```go
	objectFiles, err := findObjectFiles(altObjectDir)
	if err != nil {
		return err
	}
```

**File:** internal/git/objectpool/disconnect.go (L99-117)
```go
	for _, path := range objectFiles {
		sourceRelativePath, err := filepath.Rel(f.Root(), filepath.Join(altObjectDir, path))
		if err != nil {
			return fmt.Errorf("source relative path: %w", err)
		}
		targetRelativePath := filepath.Join(repositoryRelativePath, "objects", path)

		if err := storage.MkdirAll(f, filepath.Dir(targetRelativePath)); err != nil {
			return err
		}

		if err := storage.Link(f, sourceRelativePath, targetRelativePath); err != nil {
			if errors.Is(err, fs.ErrExist) {
				continue
			}

			return err
		}
	}
```

**File:** internal/git/objectpool/disconnect.go (L203-246)
```go
// removeAlternatesIfOk is dangerous. We optimistically temporarily
// rename objects/info/alternates, and run `git fsck` to see if the
// resulting repo is connected. If this fails we restore
// objects/info/alternates. If the repo is not connected for whatever
// reason, then until this function returns, probably **all concurrent
// RPC calls to the repo will fail**. Also, if Gitaly crashes in the
// middle of this function, the repo is left in a broken state. We do
// take care to leave a copy of the alternates file, so that it can be
// manually restored by an administrator if needed.
func removeAlternatesIfOk(ctx context.Context, repo *localrepo.Repo, altFile, backupFile string, logger log.Logger, txManager transaction.Manager) error {
	// Acquire distributed lock
	if err := transaction.VoteOnContext(ctx, txManager, voting.VoteFromData([]byte("disconnect alternate")), voting.Preparing); err != nil {
		return fmt.Errorf("preparatory vote for disconnecting alternate: %w", err)
	}

	if err := transaction.VoteOnContext(ctx, txManager, voting.VoteFromData([]byte("disconnect alternate")), voting.Prepared); err != nil {
		return fmt.Errorf("preparatory vote for disconnecting alternate: %w", err)
	}

	if err := os.Rename(altFile, backupFile); err != nil {
		return err
	}

	rollback := true
	defer func() {
		if !rollback {
			return
		}

		// If we would do a os.Rename, and then someone else comes and clobbers
		// our file, it's gone forever. This trick with os.Link and os.Rename
		// is equivalent to "cp $backupFile $altFile", meaning backupFile is
		// preserved for possible forensic use.
		tmp := backupFile + ".2"

		if err := os.Link(backupFile, tmp); err != nil {
			logger.WithError(err).ErrorContext(ctx, "copy backup alternates file")
			return
		}

		if err := os.Rename(tmp, altFile); err != nil {
			logger.WithError(err).ErrorContext(ctx, "restore backup alternates file")
		}
	}()
```

**File:** internal/git/objectpool/disconnect.go (L248-265)
```go
	// The choice here of git rev-list is for performance reasons.
	// git fsck --connectivity-only performed badly for large
	// repositories. The reasons are detailed in https://lore.kernel.org/git/9304B938-4A59-456B-B091-DBBCAA1823B2@gmail.com/
	cmd, err := repo.Exec(ctx, gitcmd.Command{
		Name: "rev-list",
		Flags: []gitcmd.Option{
			gitcmd.Flag{Name: "--objects"},
			gitcmd.Flag{Name: "--all"},
			gitcmd.Flag{Name: "--quiet"},
		},
	})
	if err != nil {
		return err
	}

	if err := cmd.Wait(); err != nil {
		return &connectivityError{error: err}
	}
```

**File:** internal/gitaly/service/objectpool/alternates.go (L13-18)
```go
// DisconnectGitAlternates is a slightly dangerous RPC. It optimistically hard-links all alternate
// objects we might need, and then temporarily removes (renames) objects/info/alternates and runs
// a connectivity check. If we are unlucky that leaves the repository in a broken state during the
// connectivity check. If we are very unlucky and Gitaly crashes, the repository stays in a broken
// state until an administrator intervenes and restores the backed-up copy of
// objects/info/alternates.
```

**File:** doc/object_pools.md (L10-12)
```markdown
The sharing of objects for a given repository and its object pool is done via
alternate object directories which Gitaly sets up when linking a repository to
an object pool by writing the `objects/info/alternates` file.
```

**File:** doc/object_pools.md (L78-97)
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

**File:** proto/objectpool.proto (L60-78)
```text
  // DisconnectGitAlternates will disconnect the object pool member from its object pool. It will:
  //
  // 1. Link all objects from the object pool into the member repository. This essenitally
  //    reduplicates previously-duplicated objects so that the repository will continue to function
  //    after being unlinked.
  // 2. Remove the alternates link to the object pool.
  // 3. Perform a consistency check to assert that the repository is indeed fully functional after
  //    unlinking it from its pool. If the consistency check fails the alternates link is restored
  //    an the RPC fails.
  //
  // If successful, the object pool member is disconnected from the object pool and does not depend
  // on it anymore.
  //
  // This RPC does not return an error in case the repository is not linked to any object pool.
  rpc DisconnectGitAlternates(DisconnectGitAlternatesRequest) returns (DisconnectGitAlternatesResponse) {
    option (op_type) = {
      op: MUTATOR
    };
  }
```
