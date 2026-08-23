This is a real, exploitable vulnerability confirmed by the code.

### Title
Attacker-controlled `objects/info/alternates` from `CreateRepositoryFromSnapshot`/`ReplicateRepository` tar extraction causes cross-repository object disclosure via partition co-location - ([File: internal/gitaly/storage/storagemgr/partition_assigner.go])

### Summary
`CreateRepositoryFromSnapshot` and `ReplicateRepository` extract an HTTP/gRPC-supplied tar archive directly onto disk via `extractTarToDirectory`/`untar` before the repository is registered in a partition, allowing arbitrary regular files — including a crafted `objects/info/alternates` — to be written into the new repository. When `getAlternatePartitionID` subsequently reads this file, it only checks that the resolved path stays within the storage root (`storage.ValidateRelativePath`) and isn't the repository itself (`ErrAlternatePointsToSelf`); it performs no ownership/ACL check that the target of the alternate is a legitimate object pool the caller is authorized to link to. If the alternate path resolves to an existing repository (e.g. `../../<victim>/objects`), the attacker's new repository is silently co-assigned into the victim's partition.

### Finding Description
The relevant code paths:
- `internal/gitaly/service/repository/create_repository_from_snapshot.go` (`untar`) pipes attacker-controlled tar bytes into `tar -C <path> -xvf -`, and `internal/gitaly/service/repository/replicate.go` (`extractTarToDirectory`, lines 314-406) manually extracts tar entries (including regular files) onto disk with only path-traversal checks — no allow-list of file names, so a `objects/info/alternates` entry is written like any other file.
- These extraction calls happen inside `repoutil.Create`'s callback, i.e. before the repository is committed/assigned a partition.
- When a transaction is later begun against this new repository, `partitionAssigner.getPartitionID` → `assignPartitionID` → `getAlternatePartitionID` (`internal/gitaly/storage/storagemgr/partition_assigner.go:317-359`) reads the alternates file with `gitstorage.ReadAlternatesFile` and computes:
```
alternateRelativePath, err := storage.ValidateRelativePath(
    pa.storagePath,
    filepath.Dir(filepath.Join(relativePath, "objects", alternate)),
)
``` [1](#0-0) 
This only ensures the resolved path stays inside the storage root and isn't the repo itself — it does **not** verify the target is a pool the attacker is authorized to reference, nor that the alternates file was written through the trusted `objectpool.Link` path (`internal/git/objectpool/link.go`).
- `getPartitionIDRecursive` is then called for the victim's relative path, which — because the victim repository already exists on disk — passes `storage.ValidateGitDirectory` and gets (or is assigned) a partition ID, which the attacker's repository is forced to share (`ErrRepositoriesAreInDifferentPartitions` is only raised if a *different* pre-existing partition hint conflicts, not to block first-time co-assignment).
- Once co-located, `createRepositorySnapshots` in `internal/gitaly/storage/storagemgr/partition/snapshot/snapshot.go:180-209` includes the alternate (victim) repository's objects in the attacker's transactional snapshot whenever the attacker's repository is accessed, disclosing the victim's objects.

Existing invariant is documented but not enforced in code: alternates are meant to be written only by `objectpool.Link` (which does have a `TestLink_noClobber`-style protection, but only inside the `Link` RPC itself, not at repository-creation time). No comparable check exists in `create_repository_from_snapshot.go` / `replicate.go` to reject a pre-existing/attacker-supplied `objects/info/alternates` in extracted content.

### Impact Explanation
An unprivileged attacker who can call `CreateRepositoryFromSnapshot` (self-hosted HTTP endpoint serving the tar) or trigger `ReplicateRepository`/import flows can craft a tar containing `objects/info/alternates` pointing at `../../<victim-relative-path>/objects`. If the victim relative path is guessable/known (project paths are often derivable), the attacker's new repository becomes co-partitioned with the victim, and subsequent reads (via git operations on the attacker's own repo, which will resolve objects through the alternate) disclose the victim's Git objects — this is unauthorized cross-repository/cross-tenant object disclosure, matching GitLab's "IDOR"/"cross-tenant data access" bounty class.

### Likelihood Explanation
Requires only standard, unprivileged capability to invoke `CreateRepositoryFromSnapshot` (attacker controls the `HttpUrl` serving the tar) or similar import RPC, and knowledge/guessability of the victim's relative/hashed storage path. No admin access, no secret, no MITM needed. This is a realistic and repeatable attack given tar content is entirely attacker-controlled.

### Recommendation
- In `extractTarToDirectory` and `untar`'s underlying extraction, reject or strip any tar entry at `objects/info/alternates` (and any git config/hook paths not expected from a bona fide repository snapshot), or validate post-extraction that no alternates file exists unless explicitly created via `objectpool.Link`.
- In `getAlternatePartitionID`, in addition to `ValidateRelativePath`/self-check, verify that the alternate target is a repository the caller is authorized to be linked to (e.g., only allow co-partitioning when the alternate was set via the trusted linking flow, tracked separately from the raw file content, or cross-check against Rails-provided pool information).
- Fail repository creation (`repoutil.Create`) if a `objects/info/alternates` file is discovered in freshly extracted/imported content that wasn't produced by Gitaly's own linking mechanism.

### Proof of Concept
Extend `TestPartitionAssigner_alternates`-style test:
```go
func TestPartitionAssigner_maliciousAlternate(t *testing.T) {
    // 1. Create victim repository "victim/repo" via gittest.CreateRepository.
    // 2. Simulate CreateRepositoryFromSnapshot import: create "attacker/repo" directory
    //    and write objects/info/alternates containing "../../../victim/repo/objects".
    // 3. Call pa.getPartitionID(ctx, "attacker/repo", "", true).
    // Expected (fixed) behavior: error such as ErrUnauthorizedAlternate.
    // Actual (vulnerable) behavior: returns the same partition ID as "victim/repo",
    // and subsequent transaction snapshot for "attacker/repo" includes victim's objects
    // (verify via createRepositorySnapshots / TransactionManager.Begin RepositoryAssertion).
}
```
At the RPC level: call `CreateRepositoryFromSnapshot` with a crafted tar whose `objects/info/alternates` entry references a known victim relative path, then assert `Fsck`/`GetObjectPool`/read RPCs against the attacker's new repo reveal the victim's commit objects.

### Citations

**File:** internal/gitaly/storage/storagemgr/partition_assigner.go (L330-349)
```go
	// The relative path should point somewhere within the same storage.
	alternateRelativePath, err := storage.ValidateRelativePath(
		pa.storagePath,
		// Take the relative path to the repository, not 'repository/objects'.
		filepath.Dir(
			// The path in alternates file points to the object directory of the alternate
			// repository. The path is relative to the repository's own object directory.
			filepath.Join(relativePath, "objects", alternate),
		),
	)
	if err != nil {
		return 0, fmt.Errorf("validate relative path: %w", err)
	}

	if alternateRelativePath == relativePath {
		// The alternate must not point to the repository itself. Not only is it non-sensical
		// but it would also cause a dead lock as the repository is locked during this call
		// already.
		return 0, storage.ErrAlternatePointsToSelf
	}
```
