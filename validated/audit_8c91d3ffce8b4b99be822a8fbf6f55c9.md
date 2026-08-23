Confirmed: `GitObjectDirectory` and `GitAlternateObjectDirectories` are ordinary fields on the `Repository` protobuf message [1](#0-0) , settable by any RPC client sending accessor requests (this is exactly how Rails' access-check callback loop is designed to work, per `beginTransactionForRepository`) [2](#0-1) . This maps closely to the reported bug class.

### Title
Non-repo-specific "generic quarantine" name check in `ObjectDirectoryPath` allows cross-repository object-directory acceptance - (File: `internal/git/localrepo/paths.go`)

### Summary
`Repo.ObjectDirectoryPath()` validates a client-supplied `GitObjectDirectory` path using two different tests: a strict, repository-specific check based on a SHA1 hash of the repo's relative path (`storage.QuarantineDirectoryPrefix`), and a second, much weaker check (`isTransactionQuarantineDir`) that accepts *any* path whose base directory is simply named `quarantine` (or `tmp_objdir*` inside a `quarantine` parent), with no verification that this particular quarantine directory actually belongs to the requesting repository. This mirrors the reported bridge bug where an "open"/network-wide discriminator (`allowedDiscriminator1`) was accepted instead of requiring the contract-specific one (`allowedDiscriminator2`).

### Finding Description
`ObjectDirectoryPath` is: [3](#0-2) 

The function first computes `relativeObjectDirectoryPath` and classifies it as a "transaction quarantine dir" purely by name shape — `baseDir == "quarantine"` or `parentDir == "quarantine" && strings.HasPrefix(baseDir, "tmp_objdir")` — with **no check that the specific quarantine directory belongs to this repo/transaction**. This is the "open" (any-bridge / any-repo) branch, analogous to `allowedDiscriminator1`.

Only in the `else` branch (when the path is *not* recognized as a generic quarantine dir) does the code fall back to the strict, repository-scoped check using `storage.QuarantineDirectoryPrefix(repo)`, which hashes the repo's relative path to build a repo-unique prefix [4](#0-3) . This is the "specific" (per-bridge / per-repo) branch, analogous to `allowedDiscriminator2`.

Because the *generic* "named `quarantine`" branch is checked first and short-circuits the specific check, any `GitObjectDirectory` value whose path, once joined and validated to stay inside the storage root (`storage.ValidateRelativePath`), simply ends in a directory literally called `quarantine` (or `.../quarantine/tmp_objdir-*`) is accepted for *any* target repository — without proving that quarantine directory was created for, or belongs to, that repository's own transaction. The storage-root validation (`storage.ValidateRelativePath`, `internal/gitaly/storage/locator.go:157-164`) only prevents escaping the storage's root, not crossing between sibling repositories/partitions within the same storage — e.g., paths like `<repo-relative-path>/../other-repo/quarantine` or a staging/partition-relative path that happens to end in `.../quarantine` created for a different repository's write transaction, would satisfy `isTransactionQuarantineDir` and bypass the repo-specific hash check entirely.

The `GitObjectDirectory`/`GitAlternateObjectDirectories` fields are attacker/client-reachable: they exist directly on the public `Repository` proto message and are explicitly expected to arrive on caller-supplied Accessor RPCs as part of the documented Rails access-check quarantine loop-back flow [5](#0-4) , and is exercised in tests that directly set `repo.GitObjectDirectory` from client code [6](#0-5) .

### Impact Explanation
If an accessor RPC (any RPC not classified `OpMutator`, e.g. `ObjectFormat`, `GetCustomHooks`, `IsAncestor`, blob/commit readers, etc.) can be made to target repository A while setting `GitObjectDirectory` to a path that resolves (after `filepath.Join(repoPath, objectDirectoryPath)`) into a `quarantine`-named directory that actually belongs to another repository B's in-flight write transaction (e.g. during a push/mass operation touching many repos under the same storage/partition), the generic name-based branch will accept it without validating that the directory is scoped to repository A. This is exactly the double-use pattern in the report: the "open"/generic acceptance path bypasses per-entity (per-repo/per-bridge) scoping, enabling `git` to be launched against another repository's staged/quarantined objects — i.e. cross-repository object access, and potentially reading not-yet-committed or rejected objects belonging to a different repository across the same storage/partition.

### Likelihood Explanation
Reaching this code requires: (1) an ordinary but not-fully-privileged caller able to set `Repository.GitObjectDirectory` on an accessor RPC (documented, supported behavior for the Rails access-check loop) [7](#0-6) , and (2) predicting/controlling a path segment ending literally in `quarantine` or `quarantine/tmp_objdir-*` under the storage root that coincides with another repository's active quarantine (created under a shared per-storage staging directory as seen in `TransactionManager.Begin`, `filepath.Join(txn.stagingDirectory, "quarantine")`) [8](#0-7) . This requires timing/knowledge of concurrent transactions' staging paths, which lowers likelihood somewhat, but the code comment itself acknowledges the property is easy to violate ("This allows for circumventing the transaction management by configuring either of the object directories... We'll leave this unaddressed for now") [9](#0-8) .

### Recommendation
Remove or tighten the name-based `isTransactionQuarantineDir` shortcut in `ObjectDirectoryPath` so that it, too, is verified against a repository/transaction-specific identifier (e.g. requiring the quarantine directory to be nested under the transaction's own `stagingDirectory`/`quarantineDirectory`, or checked via `storage.QuarantineDirectoryPrefix(repo)` unconditionally) rather than accepting any path merely because its base directory is literally named `quarantine`. This mirrors the report's own recommendation: avoid the "open"/generic acceptance branch and always require the repo-specific discriminator/prefix check.

### Proof of Concept
1. Two repositories, A and B, exist on the same storage/partition.
2. A push to repository B is in flight, causing `TransactionManager.Begin` to create `stagingDirectory/quarantine` for B's write transaction [8](#0-7) .
3. An attacker issues an Accessor RPC (e.g. `ObjectFormat`/`IsAncestor`) targeting repository A, setting `Repository.GitObjectDirectory` to a relative path that resolves under A's repo path to a directory whose base name is `quarantine` (constructed via `..` segments plus knowledge/enumeration of the storage's staging layout).
4. `ObjectDirectoryPath` classifies this as `isTransactionQuarantineDir == true` purely from the name shape and skips the `QuarantineDirectoryPrefix(repo)`-based ownership check, returning the path as valid.
5. Gitaly spawns `git` with `GIT_OBJECT_DIRECTORY` pointed at B's quarantine contents while operating "as repository A," disclosing objects that belong to repository B's not-yet-committed push.

### Citations

**File:** proto/shared.proto (L1-1)
```text
syntax = "proto3";
```

**File:** internal/gitaly/storage/storagemgr/middleware.go (L271-297)
```go
	if targetRepo.GetGitObjectDirectory() != "" || len(targetRepo.GetGitAlternateObjectDirectories()) > 0 {
		// The object directories should only be configured on a repository coming from a request that
		// was already configured with a quarantine directory and is being looped back to Gitaly from Rails'
		// authorization checks. If that's the case, the request should already be running in scope of a
		// transaction and the repository rewritten to point to the snapshot repository. We thus don't start
		// a new transaction if we encounter this.
		//
		// This property is violated in tests which manually configure the object directory or the alternate
		// object directory. This allows for circumventing the transaction management by configuring the either
		// of the object directories. We'll leave this unaddressed for now and later address this by removing
		// the options to configure object directories and alternates in a request.

		if methodInfo.Operation == protoregistry.OpMutator {
			// Accessor requests may come with quarantine configured from Rails' access checks. Since the
			// RPC that triggered these access checks would already run in a transaction and target a
			// snapshot, we won't start another one. Mutators however are rejected to prevent writes
			// unintentionally targeting the main repository.
			return transactionalizedRequest{}, ErrQuarantineConfiguredOnMutator
		}

		rewrittenReq, err := restoreSnapshotRelativePath(ctx, methodInfo, req)
		if err != nil {
			return transactionalizedRequest{}, fmt.Errorf("restore snapshot relative path: %w", err)
		}

		return nonTransactionalRequest(ctx, rewrittenReq), nil
	}
```

**File:** internal/git/localrepo/paths.go (L43-75)
```go
	parentDir := filepath.Base(filepath.Dir(relativeObjectDirectoryPath))
	baseDir := filepath.Base(relativeObjectDirectoryPath)
	isTransactionQuarantineDir := (baseDir == "quarantine") || ((parentDir == "quarantine") && strings.HasPrefix(baseDir, "tmp_objdir"))

	// Transactions quarantine a repository by pointing the object directory to a 'quarantine' named
	// directory in the transaction's temporary directory. If the base directory is `quarantine`,
	// Git push may apply an additional layer of quarantine such as `/quarantine/tmp_objdir-incoming-Gbc29N`
	// so we don't assert the `/quarantine` being the last element of the path. We thus also check for
	// whether the parent directory is in `quarantine` and whether the base directory has the expected
	// `tmp_objdir` suffix.
	if !isTransactionQuarantineDir {
		// We need to check whether the relative object directory as given by the repository is
		// a valid path. This may either be a path in the Git repository itself, where it may either
		// point to the main object directory storage or to an object quarantine directory as
		// created by git-receive-pack(1). Alternatively, if that is not the case, then it may be a
		// manual object quarantine directory located in the storage's temporary directory. These
		// have a repository-specific prefix which we must check in order to determine whether the
		// quarantine directory does in fact belong to the repo at hand.
		if _, origError := storage.ValidateRelativePath(repoPath, objectDirectoryPath); origError != nil {
			tempDir, err := repo.locator.TempDir(repo.GetStorageName())
			if err != nil {
				return "", structerr.NewInvalidArgument("getting storage's temporary directory: %w", err)
			}

			expectedQuarantinePrefix := filepath.Join(tempDir, storage.QuarantineDirectoryPrefix(repo))
			absoluteObjectDirectoryPath := filepath.Join(repoPath, objectDirectoryPath)

			// The relative path is outside of the repository
			if !strings.HasPrefix(absoluteObjectDirectoryPath, expectedQuarantinePrefix) {
				return "", structerr.NewInvalidArgument("not a valid relative path: %w", origError)
			}
		}
	}
```

**File:** internal/gitaly/storage/locator.go (L201-212)
```go
// QuarantineDirectoryPrefix returns a prefix for use in the temporary directory. The prefix is
// based on the relative repository path and will stay stable for any given repository. This allows
// us to verify that a given quarantine object directory indeed belongs to the repository at hand.
// Ideally, this function would directly be located in the quarantine module, but this is not
// possible due to cyclic dependencies.
func QuarantineDirectoryPrefix(repo Repository) string {
	hash := [20]byte{}
	if repo != nil {
		hash = sha1.Sum([]byte(repo.GetRelativePath()))
	}
	return fmt.Sprintf("quarantine-%x-", hash[:8])
}
```

**File:** internal/gitaly/storage/storagemgr/middleware_test.go (L319-329)
```go
		{
			desc: "mutator with object directory configured",
			performRequest: func(t *testing.T, ctx context.Context, cc *grpc.ClientConn) {
				repo := validRepository()
				repo.GitObjectDirectory = "non-default"

				resp, err := gitalypb.NewRepositoryServiceClient(cc).RemoveRepository(ctx, &gitalypb.RemoveRepositoryRequest{Repository: repo})
				testhelper.RequireGrpcError(t, structerr.NewInternal("%w", storagemgr.ErrQuarantineConfiguredOnMutator), err)
				require.Nil(t, resp)
			},
		},
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L424-428)
```go
				if txn.repositoryExists {
					txn.quarantineDirectory = filepath.Join(txn.stagingDirectory, "quarantine")
					if err := os.MkdirAll(filepath.Join(txn.quarantineDirectory, "pack"), mode.Directory); err != nil {
						return nil, fmt.Errorf("create quarantine directory: %w", err)
					}
```
