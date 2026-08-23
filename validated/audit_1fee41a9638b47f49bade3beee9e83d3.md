## Title
Object pool link silently dropped during replication when pool and member are partitioned differently, leaving forked repositories permanently missing deduplicated objects - (File: internal/praefect/replicator.go)

### Summary
This is a structurally identical bug class to the reported Turnstile issue: an operation that appears to succeed at the "application" layer (the `ReplicateRepository` RPC returns no error) silently fails to establish a required linkage at the "storage" layer (the object-pool alternate link), because a stricter invariant enforced deeper in the stack (partition co-location) rejects it. The caller gets no error and has no way to detect or repair the inconsistency afterward.

### Finding Description
When Praefect replicates a repository from a source node to a target node, `defaultReplicator.Replicate` reconciles the object-pool state between source and target by calling `LinkRepositoryToObjectPool` on the target. [1](#0-0) 

If the pool and the pool member end up in different partitions on the target node, the transactional partition manager rejects the link with `ErrRepositoriesAreInDifferentPartitions`. Instead of surfacing this as an error to the replication job (which would normally be retried or alerted on), the code deliberately swallows it and moves on, leaving the repository unlinked from its pool: [2](#0-1) 

The replication job then proceeds to record the generation as successfully replicated: [3](#0-2) 

This mirrors the external report's root cause exactly: a stricter, lower-layer check (partition co-location, analogous to the "account must exist" check on the consensus layer) silently defeats an operation that the higher layer (the RPC / replication job) reports as successful, with no revert and no mechanism for the caller to detect or retry the failure. Just like the Turnstile NFT recipient could never be fixed after the fact (`onlyUnregistered`), a forked repository's alternates link, once skipped this way, has no automatic remediation path — `LinkRepositoryToObjectPool` is effectively idempotent per repository state and Praefect has no separate reconciliation loop that revisits this specific failure mode.

The partition co-location requirement itself is enforced in `partition_assigner.go` and is reachable via ordinary user-driven forking (`CreateFork`), since forks and their object pools are supposed to be assigned to the same partition, but replication of an already-existing repository to a node whose partition assignment differs (e.g., due to replay order, prior state, or partition reassignment) can hit this branch.

### Impact Explanation
A pool member repository (typically a fork) that is supposed to be deduplicated against a shared object pool ends up on a replica without the corresponding `objects/info/alternates` link. Because the pool holds objects that are not duplicated into the member's own object database, the affected replica is missing objects that its references point to. Read RPCs served from that replica (e.g. reading commits, blobs, trees) will fail with "object not found"-style errors, and any promotion of that replica to primary would present users with an apparently corrupted repository — all without any operator-visible failure at replication time, since the replication job is marked successful.

### Likelihood Explanation
This path triggers purely from an ordinary user's fork/replication flow — no privileged actor, leaked token, or malicious peer is required. It depends on Praefect scheduling a replication job for a fork whose pool ends up assigned to a different partition than the member on the target node, which the code comment acknowledges is an anticipated (if considered "difficult to support") scenario rather than a hypothetical one.

### Recommendation
Do not silently discard `ErrRepositoriesAreInDifferentPartitions`. Either:
- Fail the replication job so it surfaces as a persistent, retryable/alertable error (mirroring the report's suggestion to return an error from `PostTxProcessing` rather than swallowing it), or
- Record the degraded/unlinked state explicitly (e.g., in the repository store) so that operators and future reconciliation logic can detect and repair repositories that are missing their expected pool linkage, rather than treating the replication as fully successful.

### Proof of Concept
1. Create a source repository and an object pool, then fork a member repository into the pool on the primary/source node so that the member and pool are co-located in the same partition there.
2. Arrange (via normal repository/partition assignment ordering across nodes, e.g. the member repository already exists in a different partition on the target node before the pool relationship is known) for the target node's partition assignment of the member to differ from the pool's partition, as covered by `TestPartitionAssigner_alternates`/`partition_assigner.go` invariants. [4](#0-3) 
3. Trigger replication of the member repository to that target node. `Replicate` calls `GetObjectPool` on both sides, sees a mismatch, and calls `LinkRepositoryToObjectPool` on the target. [5](#0-4) 
4. `LinkRepositoryToObjectPool` fails with `ErrRepositoriesAreInDifferentPartitions`; the replicator matches this error string and swallows it, then proceeds to mark generation as replicated with no alternates link ever created on the target. [6](#0-5) 
5. Reads against objects that exist only in the pool will fail on the target replica, while replication metadata reports success.

### Citations

**File:** internal/praefect/replicator.go (L98-160)
```go
	sourceResp, err := sourceObjectPoolClient.GetObjectPool(ctx, &gitalypb.GetObjectPoolRequest{
		Repository: sourceRepository,
	})
	if err != nil {
		return err
	}

	targetResp, err := targetObjectPoolClient.GetObjectPool(ctx, &gitalypb.GetObjectPoolRequest{
		Repository: targetRepository,
	})
	if err != nil {
		return err
	}

	sourcePool := sourceResp.GetObjectPool()
	targetPool := targetResp.GetObjectPool()

	switch {
	// If the source and target object pool state already match, there is nothing to sync.
	case sourcePool.GetRepository().GetRelativePath() == targetPool.GetRepository().GetRelativePath():
	// If the target repository is linked to a non-matching object pool it must be disconnected.
	case targetPool != nil:
		if _, err := targetObjectPoolClient.DisconnectGitAlternates(ctx, &gitalypb.DisconnectGitAlternatesRequest{
			Repository: targetRepository,
		}); err != nil {
			return err
		}

		// If the source repository is not linked to an object pool, the target repository does not
		// need to be linked to a new object pool. Otherwise, continue to object pool linking.
		if sourcePool == nil {
			break
		}
		fallthrough
	// If the source pool is linked to a repository, link the target repository to the matching
	// target object pool.
	case targetPool == nil:
		targetObjectPool := proto.Clone(sourcePool).(*gitalypb.ObjectPool)
		targetObjectPool.GetRepository().StorageName = targetRepository.GetStorageName()
		if _, err := targetObjectPoolClient.LinkRepositoryToObjectPool(ctx, &gitalypb.LinkRepositoryToObjectPoolRequest{
			ObjectPool: targetObjectPool,
			Repository: targetRepository,
		}); err != nil {
			if !strings.Contains(err.Error(), storagemgr.ErrRepositoriesAreInDifferentPartitions.Error()) {
				return err
			}

			// The pool and the member repository were not in the same partition and thus failed to be linked.
			// When transactions are enabled, the pool and the member repository must be in the same partition
			// are only serialized within partitions. Moving a repository into a different partition is difficult
			// as one would have to create a new repository in the same partition as the pool, and delete the old
			// one. We don't intend to support moving repositories between partitions with Praefect. If we hit this
			// error, we'll leave the repository without the alternate link.
		}
	}

	if generation != datastore.GenerationUnknown {
		return dr.rs.SetGeneration(ctx,
			event.Job.RepositoryID,
			event.Job.TargetNodeStorage,
			event.Job.RelativePath,
			generation,
		)
```

**File:** internal/gitaly/storage/storagemgr/partition_assigner_test.go (L389-480)
```go
	} {
		t.Run(tc.desc, func(t *testing.T) {
			t.Parallel()

			db, err := keyvalue.NewBadgerStore(testhelper.SharedLogger(t), t.TempDir())
			require.NoError(t, err)
			defer testhelper.MustClose(t, db)

			cfg := testcfg.Build(t)

			pa, err := newPartitionAssigner(db, cfg.Storages[0].Path)
			require.NoError(t, err)
			defer testhelper.MustClose(t, pa)

			// Access 10 repositories concurrently.
			repositoryCount := 10
			// Access each repository from 10 goroutines concurrently.
			goroutineCount := 10

			collectedIDs := make([][]storage.PartitionID, repositoryCount)
			ctx := testhelper.Context(t)
			wg := sync.WaitGroup{}
			start := make(chan struct{})

			pool, poolPath := gittest.CreateRepository(t, ctx, cfg, gittest.CreateRepositoryConfig{
				SkipCreationViaService: true,
			})

			for i := 0; i < repositoryCount; i++ {
				collectedIDs[i] = make([]storage.PartitionID, goroutineCount)

				repo, repoPath := gittest.CreateRepository(t, ctx, cfg, gittest.CreateRepositoryConfig{
					SkipCreationViaService: true,
				})

				if tc.withAlternate {
					// Link the repositories to the pool.
					alternateRelativePath, err := filepath.Rel(
						filepath.Join(repoPath, "objects"),
						filepath.Join(poolPath, "objects"),
					)
					require.NoError(t, err)
					require.NoError(t, os.WriteFile(filepath.Join(repoPath, "objects", "info", "alternates"), []byte(alternateRelativePath), fs.ModePerm))

					wg.Add(1)
					go func() {
						defer wg.Done()
						<-start
						_, err := pa.getPartitionID(ctx, repo.GetRelativePath(), "", false)
						assert.NoError(t, err)
					}()
				}

				for j := 0; j < goroutineCount; j++ {
					wg.Add(1)
					go func() {
						defer wg.Done()
						<-start
						ptnID, err := pa.getPartitionID(ctx, repo.GetRelativePath(), "", false)
						assert.NoError(t, err)
						collectedIDs[i][j] = ptnID
					}()
				}
			}

			close(start)
			wg.Wait()

			var partitionIDs []storage.PartitionID
			for _, ids := range collectedIDs {
				partitionIDs = append(partitionIDs, ids[0])
				for i := range ids {
					// We expect all goroutines accessing a given repository to get the
					// same partition ID for it.
					require.Equal(t, ids[0], ids[i], ids)
				}
			}

			if tc.withAlternate {
				// We expect all repositories to have been assigned to the same partition as they are all linked to the same pool.
				require.Equal(t, []storage.PartitionID{2, 2, 2, 2, 2, 2, 2, 2, 2, 2}, partitionIDs)
				ptnID, err := pa.getPartitionID(ctx, pool.GetRelativePath(), "", false)
				require.NoError(t, err)
				require.Equal(t, storage.PartitionID(2), ptnID, "pool should have been assigned into the same partition as the linked repositories")
				return
			}

			// We expect to have 10 unique partition IDs as there are 10 repositories being accessed.
			require.ElementsMatch(t, []storage.PartitionID{2, 3, 4, 5, 6, 7, 8, 9, 10, 11}, partitionIDs)
		})
	}
}
```
