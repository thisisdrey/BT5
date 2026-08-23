### Title
Quarantine directory ownership is verified only by a truncated 64-bit hash of the repository's `RelativePath` and a prefix match, not a strong binding, allowing weak/spoofable quarantine ownership proofs - (File: internal/gitaly/storage/locator.go)

### Summary
Gitaly derives its "proof" that a candidate object-directory belongs to a given repository purely from `sha1(RelativePath)`, truncated to 8 bytes, embedded as a string prefix (`quarantine-<16 hex chars>-`) of a temp directory name. The verification in `ObjectDirectoryPath` treats any directory matching that prefix under the storage temp root as belonging to the repository named in the very same client-supplied `Repository` message, without any cryptographically strong or context-bound identity check. This mirrors the CREATE3 bug pattern: a deterministic, attacker-computable derivation from public/repeatable input (`RelativePath`) is used as an implicit authorization/ownership proof, rather than requiring an explicit, unforgeable capability that is properly scoped to the operation's actual trust context.

### Finding Description
`QuarantineDirectoryPrefix` computes a "verification" value from nothing but the repository's `RelativePath`: [1](#0-0) 

This value is only 64 bits of a SHA1 digest, and it is used purely as a string *prefix*, not as an exact-match token: [2](#0-1) 

The `RelativePath` used to compute this prefix comes directly from the client-supplied `*gitalypb.Repository` message on the incoming request — the exact same message whose `GitObjectDirectory`/`GitAlternateObjectDirectories` fields are also attacker/client-controlled and are echoed back and forth between Gitaly and GitLab Rails during access checks (as the tests explicitly document, including the "quarantine repo with different relative path" swap test): [3](#0-2) 

Because the ownership proof is (a) a truncated hash rather than the full 160-bit SHA1 or a random capability, and (b) checked with `strings.HasPrefix` rather than exact equality, the design assumes the *only* difficulty in forging a match is guessing the temp-dir's random suffix from `os.MkdirTemp`. But the "authorization" component — the hash prefix — is entirely deterministic and reproducible by anyone who knows (or can predict) the target `RelativePath`, exactly like the CREATE3 deployer address being reproducible by anyone holding the deployer key/salt. This is the same class of bug: a value meant to signal legitimate provenance is actually just a deterministic function of public/repeatable data, and the code never asks "was this specific quarantine directory actually created by us for this exact request," only "does the path start with the expected hash of the path string."

### Impact Explanation
If any code path allows the temp-directory prefix or its random suffix to become predictable, guessable, or reusable across repositories/tenants (e.g., low entropy from `os.MkdirTemp`, directory listing exposure, or path confusion during Praefect relative-path rewriting), the prefix-based gate provides materially weaker assurance than intended: it authenticates by matching a short, deterministic hash string embedded in a path rather than verifying that the directory was genuinely produced for the specific request in the specific trust context. This weakens the quarantine isolation guarantee that "an object directory pointed to by the request truly belongs to the repository at hand," the same isolation invariant the CREATE3 bug broke for "this sender is truly the current InterchainTokenService."

### Likelihood Explanation
Directly forging a full quarantine path requires also guessing the OS-generated random suffix from `os.MkdirTemp`, which provides the dominant remaining entropy today. The likelihood of practical exploitation is therefore currently low, but the design flaw — using a short, deterministic, attacker-computable hash as an ownership/authorization proof instead of a full cryptographic binding or randomly generated capability scoped per-request — is a latent structural weakness that could become exploitable if the random-suffix entropy is ever reduced, if directory names leak, or if `RelativePath` rewriting (as already exercised by the Praefect snapshot-relative-path substitution logic in the tests) causes prefix reuse across differently-owned repositories.

### Recommendation
Replace the truncated, deterministic hash-prefix scheme with either (a) the full un-truncated hash plus exact-path equality checking (not prefix matching) bound to the specific quarantine directory actually created for the request, or (b) a per-request randomly generated, unpredictable token stored server-side and validated against the exact directory, rather than deriving "proof of ownership" solely from data (`RelativePath`) that is repeatable/computable by any client issuing a request for that repository.

### Proof of Concept
Not concretely demonstrated with a full exploit chain: `TestQuarantineDirectoryPrefix` in `internal/gitaly/storage/locator_test.go` shows the deterministic derivation (`sha1("foobar")[:8]` → `quarantine-8843d7f92416211d-`), and `TestGetObjectDirectorySize_quarantine`'s "quarantined repo with different relative path" subtest in `internal/gitaly/service/repository/size_test.go` demonstrates that swapping quarantine directories between two different repositories is exactly the scenario this prefix check is meant to catch — confirming the check's role as the sole ownership gate, and that its strength rests entirely on the (unverified in this codebase) entropy of `os.MkdirTemp`'s random suffix rather than a properly scoped authorization mechanism.

### Citations

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

**File:** internal/git/localrepo/paths.go (L60-74)
```go
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
```

**File:** internal/gitaly/service/repository/size_test.go (L278-317)
```go
	t.Run("quarantined repo with different relative path", func(t *testing.T) {
		repo1, _ := gittest.CreateRepository(t, ctx, cfg)
		quarantine1, cleanup1, err := quarantine.New(ctx, gittest.RewrittenRepository(t, ctx, cfg, repo1), logger, locator)
		require.NoError(t, err)
		t.Cleanup(cleanup1)

		repo2, _ := gittest.CreateRepository(t, ctx, cfg)
		quarantine2, cleanup2, err := quarantine.New(ctx, gittest.RewrittenRepository(t, ctx, cfg, repo2), logger, locator)
		require.NoError(t, err)
		t.Cleanup(cleanup2)

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
	})
```
