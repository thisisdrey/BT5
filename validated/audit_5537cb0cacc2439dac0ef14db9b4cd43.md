### Title
Unsigned integer underflow in `RepositoryInfo`'s object-size accounting can misreport repository size - ([File: internal/gitaly/service/repository/repository_info.go])

### Summary
`convertRepositoryInfo()` computes "recent" object sizes by subtracting stale/cruft sizes from total sizes using unsigned 64-bit integers, without ever validating that the totals are actually greater than or equal to the subtrahends, mirroring the exact bug-class in the external report (an unchecked `a - b` that assumes `a >= b`).

### Finding Description
In `convertRepositoryInfo()`, the size of "recent" loose objects and packfiles is derived by direct subtraction: [1](#0-0) 

```go
recentLooseObjectsSize := repoInfo.LooseObjects.Size - repoInfo.LooseObjects.StaleSize
recentPackfilesSize := repoInfo.Packfiles.Size - repoInfo.Packfiles.CruftSize
```

Both `Size`/`StaleSize` and `Size`/`CruftSize` fields are `uint64` [2](#0-1) . `LooseObjectsInfoForRepository` walks the on-disk `objects/` shards and accumulates `Size` and `StaleSize` together per file, incrementing both counters from the same `entryInfo.Size()` read [3](#0-2) , and `PackfilesInfoForRepository` similarly derives `CruftSize` as a subset of `Size` by walking on-disk pack files. Because these values are computed via two independent, non-atomic filesystem walks per object/shard, and the repository size is calculated from a live directory walk (`dirSizeInBytes`) that can race with concurrent Git operations (a `receive-pack`/push, background housekeeping, or a concurrent `RepositoryInfo` call performing repacks and pruning objects in the pool that is merged in via `mergePoolInfo`), it is not proven that `Size` is always `>= StaleSize` (or `>= CruftSize`) at the moment `convertRepositoryInfo` runs. If this invariant ever breaks — e.g., a filesystem read races with a repack/prune/gc that moves objects from loose to stale/cruft state, or `mergePoolInfo` merges pool stats gathered at a different point in time than the member stats [4](#0-3)  — the unsigned subtraction wraps around to a near-`math.MaxUint64` value instead of erroring, silently corrupting the RPC response instead of reverting/erroring like the analogous Solidity bug.

This is directly reachable by any client calling the unprivileged `RepositoryInfo` RPC on a repository they can access, satisfying the "ordinary user... crafted RPC field" requirement, since no special privilege is needed to invoke this RPC.

### Impact Explanation
A wrapped (near-max) `RecentSize`/`stale_size` value returned by `RepositoryInfo` corrupts a metric that GitLab's housekeeping/repository-size-limit logic consumes to decide whether a repository has exceeded storage quotas or requires optimization. An artificially huge reported size could cause downstream systems (e.g., repository size limits, housekeeping heuristics) to incorrectly block legitimate operations (false quota denial) — a data-integrity/DoS-adjacent condition on a resource-accounting RPC, analogous to the "variable participants losing access to expected values" impact in the source report, though here the effect is corrupted output rather than a revert.

### Likelihood Explanation
Likelihood is low-to-medium and unproven: I could not find code that guarantees the loose-objects/packfiles pass and the "recent" computation observe a single consistent snapshot, nor could I confirm within this exercise that a real race window exists in production (e.g., whether `RepositoryInfo` runs under a repository-wide lock or transactional snapshot that would prevent concurrent repacking from altering `Size` vs `StaleSize`/`CruftSize` relationships mid-computation). This uncertainty means the underflow is a real code smell (unchecked unsigned subtraction on externally-influenced accounting data) rather than a demonstrated, reliably-triggerable vulnerability.

### Recommendation
Guard the subtractions the same way the external report recommends: clamp to zero (or use `saturating` subtraction) instead of relying on the invariant `Size >= StaleSize` / `Size >= CruftSize`, e.g.:
```go
recentLooseObjectsSize := uint64(0)
if repoInfo.LooseObjects.Size > repoInfo.LooseObjects.StaleSize {
    recentLooseObjectsSize = repoInfo.LooseObjects.Size - repoInfo.LooseObjects.StaleSize
}
```
and equivalently for `recentPackfilesSize`. Additionally, verify whether `LooseObjectsInfoForRepository`/`PackfilesInfoForRepository`/`dirSizeInBytes` for a repository (and its linked pool) are computed against a single consistent snapshot; if not, consider snapshotting before computing these paired statistics.

### Proof of Concept
Not independently reproduced — a full PoC would require demonstrating a concrete race between the loose-object/packfile scan and a concurrent housekeeping operation (or non-atomic pool merge) that causes `StaleSize > Size` or `CruftSize > Size` at the moment `convertRepositoryInfo` executes. This was not verified within the scope of this analysis; existing tests (`TestConvertRepositoryInfo`, `TestRepositoryInfo`) only exercise the non-underflowing case where `Size >= StaleSize`/`CruftSize` [5](#0-4) .

### Citations

**File:** internal/gitaly/service/repository/repository_info.go (L79-83)
```go
		repoInfo = mergePoolInfo(repoInfo, stats.RepositoryInfo{
			LooseObjects: poolLooseObjects,
			Packfiles:    poolPackfiles,
		})
	}
```

**File:** internal/gitaly/service/repository/repository_info.go (L115-121)
```go
func convertRepositoryInfo(repoSize uint64, repoInfo stats.RepositoryInfo) (*gitalypb.RepositoryInfoResponse, error) {
	// The loose objects size includes objects which are older than the grace period and thus
	// stale, so we need to subtract the size of stale objects from the overall size.
	recentLooseObjectsSize := repoInfo.LooseObjects.Size - repoInfo.LooseObjects.StaleSize
	// The packfiles size includes the size of cruft packs that contain unreachable objects, so
	// we need to subtract the size of cruft packs from the overall size.
	recentPackfilesSize := repoInfo.Packfiles.Size - repoInfo.Packfiles.CruftSize
```

**File:** internal/git/stats/repository_info.go (L317-333)
```go
// LooseObjectsInfo contains information about loose objects.
type LooseObjectsInfo struct {
	// Count is the number of loose objects.
	Count uint64 `json:"count"`
	// Size is the total size of all loose objects in bytes.
	Size uint64 `json:"size"`
	// StaleCount is the number of stale loose objects when taking into account the specified cutoff
	// date.
	StaleCount uint64 `json:"stale_count"`
	// StaleSize is the total size of stale loose objects when taking into account the specified
	// cutoff date.
	StaleSize uint64 `json:"stale_size"`
	// GarbageCount is the number of garbage files in the loose-objects shards.
	GarbageCount uint64 `json:"garbage_count"`
	// GarbageSize is the total size of garbage in the loose-objects shards.
	GarbageSize uint64 `json:"garbage_size"`
}
```

**File:** internal/git/stats/repository_info.go (L371-380)
```go
			// Note: we don't `continue` here as we count stale objects into the total
			// number of objects.
			if entryInfo.ModTime().Before(cutoffDate) {
				info.StaleCount++
				info.StaleSize += uint64(entryInfo.Size())
			}

			info.Count++
			info.Size += uint64(entryInfo.Size())
		}
```

**File:** internal/gitaly/service/repository/repository_info_test.go (L881-909)
```go
		},
		{
			desc: "loose objects with counts",
			repoInfo: stats.RepositoryInfo{
				LooseObjects: stats.LooseObjectsInfo{
					Size:       123,
					StaleSize:  3,
					Count:      50,
					StaleCount: 5,
				},
				References: stats.ReferencesInfo{
					ReferenceBackendName: gittest.DefaultReferenceBackend.Name,
				},
			},
			expectedResponse: &gitalypb.RepositoryInfoResponse{
				References: &gitalypb.RepositoryInfoResponse_ReferencesInfo{
					ReferenceBackend: gittest.FilesOrReftables(
						gitalypb.RepositoryInfoResponse_ReferencesInfo_REFERENCE_BACKEND_FILES,
						gitalypb.RepositoryInfoResponse_ReferencesInfo_REFERENCE_BACKEND_REFTABLE,
					),
				},
				Objects: &gitalypb.RepositoryInfoResponse_ObjectsInfo{
					Size:                   123,
					RecentSize:             120,
					StaleSize:              3,
					LooseObjectsCount:      50,
					StaleLooseObjectsCount: 5,
				},
			},
```
