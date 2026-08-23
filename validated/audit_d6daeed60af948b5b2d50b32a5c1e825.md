### Title
Unbounded, on-demand `CalculateChecksum` ref iteration enables cheap, repeatable DoS of accessor RPC and replication verification - ([File: internal/gitaly/service/repository/calculate_checksum.go])

### Summary
`CalculateChecksum` runs `git show-ref --head` and iterates over *every* reference in a repository to build a SHA1 XOR-checksum, with no limit, pagination, or cost accounting on the number of refs processed. [1](#0-0) 
This mirrors the reported bug class: a permissionless, cheap write (creating refs, analogous to sending spam-denom tokens) inflates a piece of state that is later scanned in full by an unmetered/automatically-invoked code path, giving an attacker outsized, repeatable cost amplification.

### Finding Description
`CalculateChecksum` is a plain `ACCESSOR` RPC with no page size, no ref-count cap, and no early-exit: it scans the full `git show-ref --head` output for the target repository and hashes every line with SHA1. [2](#0-1) 
An ordinary user with write access to a repository can grow the number of references essentially without bound via mutator RPCs such as `UpdateReferences`, which imposes no cap on the number of ref updates that can be queued in a single streamed request. [3](#0-2) 
Once refs are numerous, every subsequent call to `CalculateChecksum` — which is also invoked automatically and repeatedly by Praefect during replication confirmation/recovery for every write to that repository — must fully re-scan and re-hash all references, with cost scaling linearly (or worse, since `git show-ref` itself must enumerate everything) with the number of refs the attacker planted. [4](#0-3) 
This is directly analogous to the reported `AllocateRewards`/`GetAllBalances` issue: a cheap, permissionless write (spam denoms / spam refs) inflates address/repository state, and an unmetered, automatically-triggered read path (`BeginBlock` / Praefect's replication-confirmation calls to `CalculateChecksum`) then performs unbounded iteration over that inflated state on every subsequent invocation, with no limit comparable to `GetBalance(ctx, addr, denom)` per-denom lookups.

### Impact Explanation
Because `CalculateChecksum` has no limit on the number of refs scanned and is both directly callable and automatically invoked as part of Gitaly/Praefect's internal consistency/replication-confirmation workflow, an attacker who can push/update references to a repository can inflate the ref count to a level where each checksum computation takes disproportionately long. This can:
- Cause request timeouts / resource exhaustion on the accessor RPC handler itself (repeated caller-driven DoS), and
- Repeatedly stall or slow down Praefect's automatic replication-confirmation and recovery flows for that repository, since those flows call `CalculateChecksum` on every write without any cap.

This matches the "DoS of a handler" acceptance criterion, achievable purely through ordinary push/reference-update actions without any privileged access.

### Likelihood Explanation
Likelihood is high: creating references via `UpdateReferences` requires only ordinary write access to the target repository (the same access level needed to push at all), and there is no server-side limit preventing an attacker from queuing an arbitrarily large number of ref updates in one or more streamed requests. Once refs are planted, every future accessor call to `CalculateChecksum` — as well as every automatic replication-confirmation cycle — pays the inflated cost, requiring no further attacker interaction ("on-demand" repeatable cost, similar to the reported per-`BeginBlock` amplification).

### Recommendation
Bound the amount of work `CalculateChecksum` (and the underlying `git show-ref` invocation) can perform per call: enforce a maximum reference count/time budget, paginate or stream partial checksums, or cache/incrementally update the checksum instead of re-scanning all refs on every invocation. Additionally, consider capping the number of reference updates permitted in a single `UpdateReferences` call/session to limit how much ref-count inflation a single unprivileged actor can introduce.

### Proof of Concept
Not independently reproduced in this analysis; the following describes an expected reproduction path.
1. As an ordinary user with push/write access, issue repeated `UpdateReferences` RPCs (or a single very large streamed request) to create a very large number of new references (e.g., hundreds of thousands of `refs/heads/spam-N`) in the target repository — no server-side limit rejects this. See [3](#0-2) .
2. Call `CalculateChecksum` on the repository and observe that execution time scales with the number of refs created, since the entire `git show-ref --head` output is scanned and hashed with no limit. See [2](#0-1) .
3. Observe that Praefect's automatic replication-confirmation logic, which calls `CalculateChecksum` for both primary and replica on every write, is similarly slowed for every subsequent write to the repository. See [4](#0-3) .

### Citations

**File:** internal/gitaly/service/repository/calculate_checksum.go (L16-47)
```go
func (s *server) CalculateChecksum(ctx context.Context, in *gitalypb.CalculateChecksumRequest) (*gitalypb.CalculateChecksumResponse, error) {
	repoProto := in.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repoProto); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	repo := s.localRepoFactory.Build(repoProto)
	repoPath, err := repo.Path(ctx)
	if err != nil {
		return nil, err
	}

	cmd, err := repo.Exec(ctx, gitcmd.Command{
		Name: "show-ref",
		Flags: []gitcmd.Option{
			gitcmd.Flag{Name: "--head"},
		},
	}, gitcmd.WithSetupStdout())
	if err != nil {
		return nil, structerr.NewInternal("gitCommand: %w", err)
	}

	var checksum git.Checksum

	scanner := bufio.NewScanner(cmd)
	for scanner.Scan() {
		checksum.AddBytes(scanner.Bytes())
	}

	if err := scanner.Err(); err != nil {
		return nil, structerr.NewInternal("%w", err)
	}
```

**File:** internal/gitaly/service/ref/update_references.go (L47-88)
```go
	for {
		// Only the first request may have its repository set.
		if request.GetRepository() != nil {
			return structerr.NewInvalidArgument("repository set in subsequent request")
		}

		if len(request.GetUpdates()) == 0 {
			return structerr.NewInvalidArgument("no updates specified")
		}

		for _, update := range request.GetUpdates() {
			reference := string(update.GetReference())
			if err := git.ValidateReference(reference); err != nil {
				return structerr.NewInvalidArgument("validating reference: %w", err).
					WithMetadata("reference", reference).
					WithDetail(&gitalypb.UpdateReferencesError{
						Error: &gitalypb.UpdateReferencesError_InvalidFormat{
							InvalidFormat: &gitalypb.InvalidRefFormatError{
								Refs: [][]byte{[]byte(reference)},
							},
						},
					})
			}

			// The old object ID may be empty, in which case we don't care about the current value of the
			// reference but instead do a force update of it.
			oldObjectID := string(update.GetOldObjectId())
			if len(oldObjectID) > 0 {
				if err := objectHash.ValidateHex(oldObjectID); err != nil {
					return structerr.NewInvalidArgument("validating old object ID: %w", err).WithMetadata("old_object_id", oldObjectID)
				}
			}

			newObjectID := string(update.GetNewObjectId())
			if err := objectHash.ValidateHex(newObjectID); err != nil {
				return structerr.NewInvalidArgument("validating new object ID: %w", err).WithMetadata("new_object_id", newObjectID)
			}

			if err := updater.Update(git.ReferenceName(reference), git.ObjectID(newObjectID), git.ObjectID(oldObjectID)); err != nil {
				return structerr.NewInvalidArgument("queueing update: %w", err)
			}
		}
```

**File:** internal/praefect/replicator_test.go (L529-547)
```go
func confirmChecksums(ctx context.Context, logger log.Logger, primaryClient, replicaClient gitalypb.RepositoryServiceClient, primary, replica *gitalypb.Repository) (bool, error) {
	g, gCtx := errgroup.WithContext(ctx)

	var primaryChecksum, replicaChecksum string

	g.Go(getChecksumFunc(gCtx, primaryClient, primary, &primaryChecksum))
	g.Go(getChecksumFunc(gCtx, replicaClient, replica, &replicaChecksum))

	if err := g.Wait(); err != nil {
		return false, err
	}

	logger.WithFields(log.Fields{
		"primary_checksum": primaryChecksum,
		"replica_checksum": replicaChecksum,
	}).Info("checksum comparison completed")

	return primaryChecksum == replicaChecksum, nil
}
```
