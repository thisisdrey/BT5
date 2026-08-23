### Title
Unvalidated push objects are permanently written to the WAL/object store without validity checks - ([File: internal/gitaly/storage/storagemgr/partition/transaction_manager.go])

### Summary
The Sherlock finding describes a class of bug where a proposer can bloat permanent storage by getting invalid/unvalidated content admitted into a persistent log because the validation layer (mempool) is a no-op. Gitaly's transactional storage backend has a directly analogous gap: `TransactionManager.packObjects` explicitly packs and commits every object reachable from a pushed reference update into the repository's write-ahead log (WAL) and on-disk object store **without checking object validity**, only checking that dependency OIDs exist. This is a documented, acknowledged gap in the code itself.

### Finding Description
When a client (an ordinary user with push access) sends a `git-receive-pack` request, Gitaly's transactional partition backend quarantines the pushed pack, walks reachable objects from the new reference tips, and re-packs them via `packObjects`: [1](#0-0) 

The function's own doc comment states the objects are not validated: [2](#0-1) 

At commit time, the only integrity check performed is `verifyObjectsExist`, which merely confirms that dependency OIDs are *present* — it does not check that the objects are well-formed, that types match, or that the packfile passes `git-fsck`: [3](#0-2) 

The `objectDependencies` field comment confirms this is intended only to guard against missing objects due to concurrent pruning, not to validate object correctness: [4](#0-3) 

Once `verifyObjectsExist` and reference-conflict checks pass, the packed objects are written into the WAL log entry directory and later applied to the repository's permanent object store — there is no fsck-equivalent gate comparable to Git's own `receive.fsckObjects` on this transactional commit path. This mirrors the reported bug class: a validation-bypass path (`NoOpMempool`/here, "objects not yet checked for validity") that lets attacker-controlled data reach a durable, append-only store (CometBFT's Blockstore / Gitaly's WAL and object directory) that is not pruned automatically.

### Impact Explanation
An ordinary user with push access to a repository (no elevated privilege) can repeatedly push reference updates carrying quarantined objects that are syntactically acceptable to be packed (i.e., reachable and hash-verifiable) but are otherwise not fsck-clean or are deliberately bulky/garbage blobs. Each such push is durably packed into `objects.pack`/`.idx`/`.rev` files referenced by a committed WAL log entry: [5](#0-4) 

This causes permanent growth of the on-disk object store and WAL, consuming disk space and inflating housekeeping/repack costs, without any object-quality gate rejecting the push before it is committed — directly analogous to the reported "no mempool to bloat the block store" issue: unvalidated attacker data lands in a permanent store.

### Likelihood Explanation
Likelihood is moderate-to-high for any Gitaly deployment using the transactional storage backend, since `packObjects` runs on every write transaction targeting a repository and the lack-of-validation is unconditional (not gated behind a flag), and reaching it only requires normal push permission — the standard entry path for any repository writer.

### Recommendation
Add an explicit object-validity check (equivalent to `git fsck` or `git index-pack --strict`) to the `packObjects`/commit path in `transaction_manager.go` before objects are staged into the WAL, so malformed or otherwise invalid objects are rejected prior to being made durable, closing the gap tracked internally in gitlab-org/gitaly#5779 referenced directly in the code comment.

### Proof of Concept
1. As a user with push access, craft a push whose packfile contains reachable objects that are technically walkable/hashable (so `verifyObjectsExist`/object-walk succeeds) but would fail `git fsck --strict` (e.g., malformed tree entries, oversized/garbage blobs).
2. Push repeatedly; each push is repacked via `packObjects` and committed to the WAL/object directory without an fsck-equivalent check, as shown at `internal/gitaly/storage/storagemgr/partition/transaction_manager.go:1317-1319` and `internal/gitaly/storage/storagemgr/partition/transaction_manager.go:2030-2060`.
3. Repository storage and WAL grow with no rejection point, demonstrating the analogous "no validation before permanent storage" bloat condition described in the source report.

### Citations

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L204-209)
```go
	// objectDependencies are the object IDs this transaction depends on in
	// the repository. The dependencies are used to guard against invalid packs
	// being committed which don't contain all necessary objects. The write could
	// either be missing objects, or a concurrent prune could have removed the
	// dependencies.
	objectDependencies map[git.ObjectID]struct{}
```

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

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L1421-1450)
```go
		group.Go(func() (returnedErr error) {
			defer packReader.CloseWithError(returnedErr)

			// index-pack places the pack, index, and reverse index into the transaction's staging directory.
			var stdout, stderr bytes.Buffer
			if err := quarantineOnlySnapshotRepository.ExecAndWait(ctx, gitcmd.Command{
				Name:  "index-pack",
				Flags: []gitcmd.Option{gitcmd.Flag{Name: "--stdin"}, gitcmd.Flag{Name: "--rev-index"}},
				Args:  []string{filepath.Join(transaction.stagingDirectory, "objects.pack")},
			}, gitcmd.WithStdin(packReader), gitcmd.WithStdout(&stdout), gitcmd.WithStderr(&stderr)); err != nil {
				return structerr.New("index pack: %w", err).WithMetadata("stderr", stderr.String())
			}

			matches := packPrefixRegexp.FindStringSubmatch(stdout.String())
			if len(matches) != 2 {
				return structerr.New("unexpected index-pack output").WithMetadata("stdout", stdout.String())
			}

			packPrefix := fmt.Sprintf("pack-%s", matches[1])

			// Log the freshly created packfile and the associated files.
			packDir := filepath.Join(transaction.relativePath, "objects", "pack")
			for _, fileExtension := range []string{".pack", ".idx", ".rev"} {
				if err := transaction.walEntry.CreateFile(
					filepath.Join(transaction.stagingDirectory, "objects"+fileExtension),
					filepath.Join(packDir, packPrefix+fileExtension),
				); err != nil {
					return fmt.Errorf("record file creation: %w", err)
				}
			}
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L2030-2060)
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
}
```
