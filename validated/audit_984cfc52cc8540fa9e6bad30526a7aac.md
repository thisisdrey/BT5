Based on my research, the strongest and most concrete analog in this Gitaly codebase to the reported bug class ("silent zero-on-error propagates into a downstream consistency/security decision, hiding an unhealthy state as if it were legitimate") is the `CalculateChecksum` RPC.

### Title
`CalculateChecksum` masks corrupted or partially-scanned repositories as legitimately empty by silently returning the zero checksum - ([File: internal/gitaly/service/repository/calculate_checksum.go])

### Summary
`CalculateChecksum` collapses two semantically distinct states — "a genuinely empty repository" and "a repository whose reference enumeration failed partway through (corrupt/invalid ref)" — into the same sentinel value, `git.ZeroChecksum`. This zero value is then consumed by Praefect's replica-consistency logic (`RepositoryReplicas`, `RecoverPendingTransactions`) to make trust decisions about whether a secondary is up to date, exactly analogous to the reported pattern where a Chainlink oracle failure silently returns `0` and that zero is trusted downstream as if it were a legitimate voting-power value.

### Finding Description
`CalculateChecksum` scans `git show-ref --head` output, hashing valid refs into a running `Checksum`. If the command later fails (`cmd.Wait()` returns an error) — which can happen due to a corrupt/invalid reference such as a malformed `packed-refs` entry or broken reftable data — the function discards all refs accumulated so far and, provided `isValidRepo` (a `rev-parse --is-bare-repository` check) still succeeds, returns `git.ZeroChecksum` as if the repository were simply empty: [1](#0-0) 

This is the same class of "silent failure collapses to a benign-looking sentinel" seen in the report's `ChainlinkPriceFeed.getPriceAt` returning `0` instead of surfacing the failure. The test suite explicitly acknowledges the resulting ambiguity: [2](#0-1) 

This zero checksum is the exact value Praefect relies on to compare a primary's state against replicas in order to decide which replicas are considered "in sync" (and therefore have their generation advanced) versus which need replication: [3](#0-2)  and [4](#0-3) .

### Impact Explanation
Because both "truly empty" and "corrupt-but-still-a-git-repo" states collapse to the identical `ZeroChecksum`, two nodes that have diverged (e.g. one has a corrupt/partial ref set caused by a crafted push, an interrupted transaction, or an on-disk race) can appear to Praefect's checksum-based comparison as consistent. In `recoverPendingTransaction`, a voter whose checksum incidentally comes back as `ZeroChecksum` due to a scan failure would be marked `updated` (matching the primary) rather than `outdated`, causing `IncrementGeneration` to be called including that node and suppressing the replication job that would otherwise repair it. This mirrors the reported bug's core harm: an error condition is silently converted into a "matches expected value" signal, producing an incorrect/unfair consistency (the analog of "voting") outcome without any operator notification.

### Likelihood Explanation
`CalculateChecksum` is invoked automatically as part of Praefect's read-only checksum comparisons and recovery flow. An attacker with ordinary push access can craft references that Git considers borderline-valid enough to pass `isValidRepo`'s `rev-parse --is-bare-repository` check yet cause `git show-ref --head` to fail partway through enumeration (the existing test suite already demonstrates one such construction via a corrupted `packed-refs`/reftable entry). No privileged access is required — this is reachable through a normal git push that leaves the repository in this ambiguous state.

### Recommendation
Do not collapse "scan failed" into the same sentinel as "genuinely empty." Distinguish the two cases explicitly, e.g., by returning a distinct error/status (rather than `ZeroChecksum`) when `cmd.Wait()` fails after some refs were already read, or by not discarding already-accumulated checksum state on a mid-scan failure. Downstream consumers (`RepositoryReplicas`, `recoverPendingTransaction`) should treat an inability to compute a definitive checksum as "unknown/outdated" rather than implicitly trusting a zero value as a match.

### Proof of Concept
The existing `calculate_checksum_test.go` "invalid reference" test case demonstrates the mechanism: writing a corrupted `packed-refs` entry (or, for reftables, patching `HEAD` to `NOPE` in the reftable) causes `git show-ref --head` to fail after already having successfully hashed one legitimate commit's ref, yet the RPC still returns `git.ZeroChecksum` rather than an error or a checksum reflecting the state before failure: [5](#0-4) 
An operator could reproduce the Praefect-level impact by: (1) creating a repository on a secondary in this "corrupt-but-zero-checksum" state via a crafted push, (2) triggering `RecoverPendingTransactions` or `RepositoryReplicas`, and (3) observing that the secondary is classified as consistent/updated despite having a diverged, corrupted state.

### Citations

**File:** internal/gitaly/service/repository/calculate_checksum.go (L38-55)
```go
	var checksum git.Checksum

	scanner := bufio.NewScanner(cmd)
	for scanner.Scan() {
		checksum.AddBytes(scanner.Bytes())
	}

	if err := scanner.Err(); err != nil {
		return nil, structerr.NewInternal("%w", err)
	}

	if err := cmd.Wait(); checksum.IsZero() || err != nil {
		if s.isValidRepo(ctx, repo) {
			return &gitalypb.CalculateChecksumResponse{Checksum: git.ZeroChecksum}, nil
		}

		return nil, structerr.NewDataLoss("not a git repository '%s'", repoPath)
	}
```

**File:** internal/gitaly/service/repository/calculate_checksum_test.go (L164-164)
```go
		{
```

**File:** internal/gitaly/service/repository/calculate_checksum_test.go (L200-219)
```go
					// We write a known-broken reference into the packed-refs file. We expect that this
					// issue should be detected and reported to the caller. The existing behaviour is
					// somewhat weird though as it's impossible for the caller to distinguish an empty
					// repository from a corrupt repository given that both cases return the zero checksum.
					require.NoError(t, os.WriteFile(
						filepath.Join(repoPath, "packed-refs"),
						[]byte(fmt.Sprintf("# pack-refs with: peeled fully-peeled sorted\n%s refs/heads/broken:reference\n", commitID)),
						mode.File,
					))
				}

				return setupData{
					request: &gitalypb.CalculateChecksumRequest{
						Repository: repo,
					},
					expectedResponse: &gitalypb.CalculateChecksumResponse{
						Checksum: git.ZeroChecksum,
					},
				}
			},
```

**File:** internal/praefect/recovery.go (L75-105)
```go
	primaryChecksum, err := repoChecksum(ctx, primaryConn, tx.PrimaryStorage, tx.ReplicaPath)
	if err != nil {
		return fmt.Errorf("checksum from primary %q: %w", tx.PrimaryStorage, err)
	}

	var updated, outdated []string
	for _, voter := range tx.ExpectedVoters {
		if voter == tx.PrimaryStorage {
			continue
		}

		conn, ok := vsConns[voter]
		if !ok {
			logger.WithField("storage", voter).Warn("no connection for expected voter during recovery, treating as outdated")
			outdated = append(outdated, voter)
			continue
		}

		checksum, err := repoChecksum(ctx, conn, voter, tx.ReplicaPath)
		if err != nil {
			logger.WithError(err).WithField("storage", voter).Warn("checksum failed for voter during recovery, treating as outdated")
			outdated = append(outdated, voter)
			continue
		}

		if checksum == primaryChecksum {
			updated = append(updated, voter)
		} else {
			outdated = append(outdated, voter)
		}
	}
```

**File:** internal/praefect/service/info/repositories.go (L56-79)
```go
	var resp gitalypb.RepositoryReplicasResponse

	if resp.Primary, err = s.getRepositoryDetails(ctx, virtualStorage, primary, relativePath, replicaPath); err != nil {
		return nil, structerr.NewInternal("%w", err)
	}

	resp.Replicas = make([]*gitalypb.RepositoryReplicasResponse_RepositoryDetails, len(secondaries))

	g, ctx := errgroup.WithContext(ctx)

	for i, storage := range secondaries {
		g.Go(func() error {
			var err error
			resp.Replicas[i], err = s.getRepositoryDetails(ctx, virtualStorage, storage, relativePath, replicaPath)
			return err
		})
	}

	if err := g.Wait(); err != nil {
		return nil, structerr.NewInternal("%w", err)
	}

	return &resp, nil
}
```
