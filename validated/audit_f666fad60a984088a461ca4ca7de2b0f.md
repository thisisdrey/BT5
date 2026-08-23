### Title
Quarantine directory ownership check bypassed by basename-only match - ([File: internal/git/localrepo/paths.go])

### Summary
`Repo.ObjectDirectoryPath` in `internal/git/localrepo/paths.go` decides whether a repository's configured `GitObjectDirectory` is legitimate using two structurally different code paths: one for "manually" quarantined directories (which is bound to the specific repository via a per-repository hash prefix) and one for "transaction" quarantine directories (which is recognized purely by directory basename). Like the reported fixed-point multiply/divide bug — where the result depended on only one operand's sign flag instead of combining both operands correctly — this code only checks one superficial property (the last path component's name) and skips the actual identity/ownership check that the sibling branch performs, for what should be an equivalent security decision.

### Finding Description
`ObjectDirectoryPath` computes the object directory candidate path and classifies it: [1](#0-0) 

```go
parentDir := filepath.Base(filepath.Dir(relativeObjectDirectoryPath))
baseDir := filepath.Base(relativeObjectDirectoryPath)
isTransactionQuarantineDir := (baseDir == "quarantine") || ((parentDir == "quarantine") && strings.HasPrefix(baseDir, "tmp_objdir"))
...
if !isTransactionQuarantineDir {
    // We need to check whether the relative object directory ... 
    if _, origError := storage.ValidateRelativePath(repoPath, objectDirectoryPath); origError != nil {
        ...
        expectedQuarantinePrefix := filepath.Join(tempDir, storage.QuarantineDirectoryPrefix(repo))
        ...
        if !strings.HasPrefix(absoluteObjectDirectoryPath, expectedQuarantinePrefix) {
            return "", structerr.NewInvalidArgument("not a valid relative path: %w", origError)
        }
    }
}
```

For directories outside the repository's own `objects/info/alternates` chain, Gitaly is only supposed to accept an object directory that is provably tied to the requesting repository. The "manual" branch enforces this by comparing the path against `storage.QuarantineDirectoryPrefix(repo)`, a SHA1-derived prefix computed from the repository's own `RelativePath`: [2](#0-1) 

However, `isTransactionQuarantineDir` short-circuits this entire ownership check whenever the *basename* of the path is literally `"quarantine"` (or its parent is `"quarantine"` and the leaf starts with `"tmp_objdir"`). This condition carries no information about *which* repository or transaction the quarantine directory belongs to — it is satisfied by matching a fixed literal string, not by verifying a relationship between the requested directory and `repo`. The test fixture confirms transaction quarantine directories are simply named `.../quarantine` inside a transaction-scoped temp directory: [3](#0-2) 

Because `storage.ValidateRelativePath` (called earlier at line 38) only guarantees the path resolves *somewhere inside the storage root*, and the `isTransactionQuarantineDir` branch performs no additional check binding the directory to the calling repository or an authorized transaction, any `GitObjectDirectory` value whose final path segment is `quarantine` (or `tmp_objdir*` under a `quarantine` parent) anywhere within the storage tree is accepted for **any** repository's `Repository` message, bypassing the repo-specific prefix check that the sibling ("manual quarantine") branch performs for every other case.

### Impact Explanation
`GitObjectDirectory`/`GitAlternateObjectDirectories` are attacker-influenceable fields on the `Repository` protobuf message used across many RPCs (e.g. `GetObjectDirectorySize`, catfile/access-check loop-back calls, and any RPC that resolves object directories through `ObjectDirectoryPath`). The existing test suite explicitly demonstrates that Gitaly is expected to reject cross-repository quarantine confusion for the manual-prefix case: [4](#0-3) 

but no equivalent test exists for the "transaction quarantine directory" branch, because that branch never performs the ownership comparison in the first place. A malicious or buggy caller (or a Praefect/Rails "loop-back" access-check request with a manipulated relative path) could set `GitObjectDirectory` to point at a `.../quarantine` (or `tmp_objdir-*`) directory belonging to a *different, concurrently running* transaction/push in the same storage, causing Gitaly to treat objects staged by another repository's in-flight push as valid, readable object storage for the attacker's own repository handle. This is a cross-repository object disclosure / quarantine-isolation bypass — one of the explicitly accepted impact categories (cross-repository object access, quarantine bypass).

### Likelihood Explanation
Exploitability requires the attacker to (a) control or influence the `GitObjectDirectory` field of a `Repository` message sent to an RPC that resolves paths via `ObjectDirectoryPath`, and (b) know or guess the relative path of another transaction's temporary `quarantine` directory within the same storage (transaction/temp directory names are generated server-side and not necessarily secret to a co-located tenant on the same storage). Because the check is purely syntactic (basename match) rather than cryptographically/prefix bound like the manual case, no secret needs to be forged — only the directory name/location needs to be known, which significantly lowers the bar compared to the manual-quarantine path that requires knowledge of a repo-specific SHA1 prefix.

### Recommendation
Apply the same ownership verification to the "transaction quarantine directory" branch as is applied to the manual quarantine directory branch: bind the accepted `quarantine`/`tmp_objdir*` directory to the specific transaction or repository it was created for (e.g., by checking it resides under a path segment derived from the transaction ID or the repository's own hash), rather than accepting any path whose basename happens to be `quarantine`. At minimum, verify the directory was actually issued for the current request's transaction context before treating it as `isTransactionQuarantineDir`.

### Proof of Concept
1. Start two concurrent operations touching two different repositories, `repoA` (attacker-controlled) and `repoB` (victim), on the same Gitaly storage, such that `repoB`'s push/transaction quarantines its incoming objects into `<storage>/<tx-tmp-B>/quarantine/...`.
2. As the attacker, issue an RPC against `repoA` (any RPC that accepts a `Repository` message with a settable `GitObjectDirectory`, resolved through `Repo.ObjectDirectoryPath`) with:
   `GitObjectDirectory = <relative path from repoA's repo dir to <storage>/<tx-tmp-B>/quarantine>`
3. `storage.ValidateRelativePath` succeeds because the path is inside the storage root.
4. `isTransactionQuarantineDir` evaluates true because `filepath.Base(...) == "quarantine"`, skipping the `QuarantineDirectoryPrefix(repo)` ownership check.
5. Gitaly returns `repoB`'s in-flight quarantine directory as a valid object directory for `repoA`'s request, allowing the attacker to read objects that belong to `repoB`'s unmerged/unauthorized push.

### Citations

**File:** internal/git/localrepo/paths.go (L43-53)
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

**File:** internal/git/localrepo/paths_test.go (L80-91)
```go

	// Transactions store their set a quarantine directory in the transaction's temporary
	// directory with a path ending in `quarantine` directory. Emulate that by creating
	// such a directory in the root of the storage.
	transactionStateDir := filepath.Join(cfg.Storages[0].Path, "tx-tmp")
	transactionQuarantineDir := filepath.Join(transactionStateDir, "quarantine")
	transactionQuarantineDirWithGitPush := filepath.Join(transactionQuarantineDir, "tmp_objdir-incoming-Gbc29N")
	require.NoError(t, os.MkdirAll(transactionQuarantineDirWithGitPush, mode.Directory))
	transactionQuarantineDirRelativePath, err := filepath.Rel(repoPath, transactionQuarantineDir)
	require.NoError(t, err)
	transactionQuarantineDirWithGitPushRelativePath, err := filepath.Rel(repoPath, transactionQuarantineDirWithGitPush)
	require.NoError(t, err)
```

**File:** internal/gitaly/service/repository/size_test.go (L289-316)
```go
		// We swap out the the object directories of both quarantines. So while both are
		// valid, we still expect that this RPC call fails because we detect that the
		// swapped-in quarantine directory does not belong to our repository.
		repo := proto.Clone(quarantine1.QuarantinedRepo()).(*gitalypb.Repository)
		repo.GitObjectDirectory = quarantine2.QuarantinedRepo().GetGitObjectDirectory()
		// quarantine.New in Gitaly would receive an already rewritten repository. Gitaly would then calculate
		// the quarantine directories based on the rewritten relative path. That quarantine would then be looped
		// through Rails, which would then send a request with the quarantine object directories set based on the
		// rewritten relative path but with the original relative path of the repository. Since we're using the production
		// helpers here, we need to manually substitute the rewritten relative path with the original one when sending
		// it back through the API.
		repo.RelativePath = repo1.GetRelativePath()

		// Rails sends the repository's relative path from the access checks as provided by Gitaly. If transactions are enabled,
		// this is the snapshot's relative path. Include the metadata in the test as well as we're testing requests with quarantine
		// as if they were coming from access checks. The RPC is also a special case as it only works with a quarantine set.
		ctx := metadata.AppendToOutgoingContext(ctx, storagemgr.MetadataKeySnapshotRelativePath,
			// Gitaly sends the snapshot's relative path to Rails from `pre-receive` and Rails
			// sends it back to Gitaly when it performs requests in the access checks. The repository
			// would have already been rewritten by Praefect, so we have to adjust for that as well.
			gittest.RewrittenRepository(t, ctx, cfg, repo).GetRelativePath(),
		)

		response, err := client.GetObjectDirectorySize(ctx, &gitalypb.GetObjectDirectorySizeRequest{
			Repository: repo,
		})
		require.Error(t, err, "rpc error: code = InvalidArgument desc = GetObjectDirectoryPath: relative path escapes root directory")
		require.Nil(t, response)
```
