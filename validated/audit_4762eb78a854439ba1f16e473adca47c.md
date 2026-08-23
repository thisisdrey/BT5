### Title
`RouteRepositoryMaintenance` bypasses the read-only/consistency gate enforced by `RouteRepositoryMutator`, allowing maintenance RPCs to run against inconsistent replicas during a failover - ([File: internal/praefect/router_per_repository.go])

### Summary
In the MagicSea report, `addToPosition()` was the one position-mutating function that forgot to apply the same "no activity during emergency unlock" gate that `renewLock()`, `extendLock()`, and the lock logic in `_lockPosition()` enforced, letting privileged state (voting power) be built up while the system believed it was frozen. The Gitaly analog is `PerRepositoryRouter`: ordinary mutator RPCs are gated by `RouteRepositoryMutator`, which refuses to route to a primary that is not in the repository's `consistentStorages` set (returning `ErrRepositoryReadOnly`, `internal/praefect/router_per_repository.go:254-256`). This is the mechanism that is supposed to prevent further divergence of an out-of-sync/failed-over repository. `RouteRepositoryMaintenance`, however, is a separate routing path used for maintenance-class RPCs and performs **no such consistency or primary-health check at all**.

### Finding Description
`RouteRepositoryMutator` (`internal/praefect/router_per_repository.go:219-295`) is the canonical write-routing function. Before it returns a route it verifies:
```go
if !consistentStorages.HasValue(primary) {
    return RepositoryMutatorRoute{}, ErrRepositoryReadOnly
}
``` [1](#0-0) 
This check exists specifically to stop mutating RPCs from running against a primary whose on-disk generation has fallen behind (e.g., after a failover promotes a node that missed writes), which `sqlElector`/read-only handling documents as the reason a virtual storage is "marked read-only until writes are manually enabled again" [2](#0-1) 
and matches `ErrRepositoryReadOnly`'s stated purpose "returned when the repository is in read-only mode... if the primary does not have the latest changes" [3](#0-2) 

`RouteRepositoryMaintenance`, by contrast, only filters by node health and by whether a replica is `Assigned` and has been created (`Generation >= 0`); it never inspects `consistentStorages` or compares generations to the primary, and never returns `ErrRepositoryReadOnly`:
```go
nodes := make([]RouterNode, 0, len(metadata.Replicas))
for _, replica := range metadata.Replicas {
    node, ok := healthyNodesByStorage[replica.Storage]
    if !ok { continue }
    if !replica.Assigned { continue }
    if replica.Generation < 0 { continue }
    nodes = append(nodes, node)
}
``` [4](#0-3) 
This is functionally identical to the MagicSea bug class: every other write-path (`RouteRepositoryMutator`, `RouteStorageMutator`) is "straight away disallowed" or gated when the system is in the equivalent of "emergency unlock" (read-only mode after a failover), but this one operation was left unrestricted.

The routing feeds RPCs that are documented as being routed "in a best-effort strategy" to *all* healthy, assigned replicas regardless of consistency state [5](#0-4) 
Maintenance-class RPCs (e.g. `OptimizeRepository`, which is explicitly treated as read-only for transactional purposes even though it triggers real on-disk repack/gc/pack-refs work via the housekeeping manager, `internal/gitaly/storage/storagemgr/middleware.go:83-88`) are dispatched to every replica this way, including nodes that are behind and would otherwise be blocked from any write by `ErrRepositoryReadOnly`.

### Impact Explanation
Running housekeeping/maintenance operations (repack, prune, pack-refs, commit-graph writes) against a stale/inconsistent replica during a read-only window can rewrite or garbage-collect the replica's on-disk object/ref state independently of the primary. If the replica is later reconciled or promoted, this out-of-band mutation can produce divergent packfiles/ref layouts, corrupt data expected to be reconciled by `ReplicateRepository`, or cause the replica to garbage collect objects still referenced only via metadata Praefect believes are consistent — undermining exactly the invariant the read-only gate exists to protect. This is analogous to the "monopoly over votes" impact in the source report: an operation type was left un-gated while the surrounding system assumed the state was frozen, letting it silently affect the eventual consistent outcome.

### Likelihood Explanation
Any Gitaly maintenance RPC that goes through `RouteRepositoryMaintenance` reaches this code path unconditionally — there is no privileged actor requirement; it happens automatically whenever housekeeping is scheduled or manually triggered on a repository that currently has inconsistent replicas (a state that legitimately occurs after any failover, which is a routine, not attacker-controlled, occurrence). This makes the trigger condition (a failover leaving some replicas behind) common rather than requiring active exploitation, though the report only reaches "medium" severity in its source analog because the direct damage window is narrow and bounded by later reconciliation.

### Recommendation
Apply the same consistency/read-only gate used in `RouteRepositoryMutator` to `RouteRepositoryMaintenance`: skip or reject replicas not present in `consistentStorages` (or otherwise behind the primary's generation) before including them in the maintenance route, so maintenance operations cannot run against a repository copy the system otherwise considers read-only/inconsistent.

### Proof of Concept
1. Set up a Praefect-managed virtual storage with 3 replicas; let a primary failover occur such that one secondary is left with an older generation and is excluded from `consistentStorages` (this is exactly the state that makes `RouteRepositoryMutator` return `ErrRepositoryReadOnly` for that repository).
2. Trigger a maintenance RPC (e.g. `OptimizeRepository`) against the same repository.
3. Observe that `RouteRepositoryMaintenance` (`internal/praefect/router_per_repository.go:505-552`) includes the stale, inconsistent replica in its `Nodes` list purely because it is healthy, assigned, and has `Generation >= 0` — with no check against `consistentStorages` — whereas the same replica would be rejected by `RouteRepositoryMutator` with `ErrRepositoryReadOnly`.
4. The maintenance RPC executes repack/gc against the stale replica's on-disk state, mutating it independently while the repository is nominally read-only for that node.

### Citations

**File:** internal/praefect/router_per_repository.go (L254-256)
```go
	if !consistentStorages.HasValue(primary) {
		return RepositoryMutatorRoute{}, ErrRepositoryReadOnly
	}
```

**File:** internal/praefect/router_per_repository.go (L502-504)
```go
// RouteRepositoryMaintenance will route the maintenance call to all healthy nodes in a best-effort
// strategy. We do not raise an error in case the primary node is unhealthy, but will in case all
// nodes are unhealthy.
```

**File:** internal/praefect/router_per_repository.go (L521-542)
```go
	nodes := make([]RouterNode, 0, len(metadata.Replicas))
	for _, replica := range metadata.Replicas {
		node, ok := healthyNodesByStorage[replica.Storage]
		if !ok {
			continue
		}

		// If the is not assigned to the replica it either hasn't yet been created
		// or it will eventually get deleted. In neither case does it make sense to
		// maintain it, so we skip such nodes.
		if !replica.Assigned {
			continue
		}

		// If the repository doesn't exist on the replica there is no need to perform any
		// maintenance tasks at all.
		if replica.Generation < 0 {
			continue
		}

		nodes = append(nodes, node)
	}
```

**File:** internal/praefect/nodes/sql_elector.go (L70-78)
```go
// Otherwise, if there is no primary or it is unhealthy, any Praefect node
// can elect a new primary by choosing candidate from the healthy node
// list. If there are no candidate nodes, the primary is demoted by setting the `demoted` flag
// in `shard_primaries`.
//
// In case of a failover, the virtual storage is marked as read-only until writes are manually enabled
// again. This status is stored in the `shard_primaries` table's `read_only` column. If `read_only` is
// set, mutator RPCs against the storage shard should be blocked in order to prevent new primary from
// diverging from the previous primary before data recovery attempts have been made.
```

**File:** internal/praefect/coordinator.go (L38-40)
```go
// ErrRepositoryReadOnly is returned when the repository is in read-only mode. This happens
// if the primary does not have the latest changes.
var ErrRepositoryReadOnly = structerr.NewFailedPrecondition("repository is in read-only mode")
```
