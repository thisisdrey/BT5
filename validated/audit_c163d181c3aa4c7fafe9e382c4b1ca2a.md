### Title
Slice out-of-range panic from unvalidated `DefaultReplicationFactor` when assigning repository-creation secondaries - ([File: internal/praefect/router_per_repository.go])

### Summary
`PerRepositoryRouter.assignRepositoryToNodes` uses a Praefect-node's `DefaultReplicationFactor` to slice the list of candidate secondary storages to `replicationFactor-1` entries without checking that the slice actually has that many elements. If a repository-creation RPC is routed on a virtual storage whose set of *currently connected* storages is smaller than the configured `DefaultReplicationFactor` (e.g., a storage node config entry was removed, a virtual storage was reconfigured with fewer nodes than the previously-set default replication factor, or the value was set via `SetReplicationFactor` before storages were later reduced), the slice expression panics with an index-out-of-range error, analogous to the reported "prerequisite not validated before use" division-by-zero bug in `CollateralSettlerERC20`.

### Finding Description
`assignRepositoryToNodes` in `internal/praefect/router_per_repository.go` builds `secondaryNodes` from `r.conns[virtualStorage]` (the storages currently configured/connected for that virtual storage), excluding the primary: [1](#0-0) 

It then trims this slice using the configured replication factor: [2](#0-1) 

The comment states "replicationFactor being zero indicates it has not been configured," acknowledging that the value is an operator-set precondition, similar to `proportionalRatioGovLP`/`proportionalRatioGovUser` in the reported bug. However, there is no check that `len(secondaryNodes) >= replicationFactor-1` before the slice operation `secondaryNodes[:replicationFactor-1]`. `DefaultReplicationFactor` is validated only loosely at config-parsing time and can be set independently via the `set-replication-factor` administrative path against a virtual storage's node *count* at that time: [3](#0-2) 

If the number of connections for a virtual storage (`r.conns[virtualStorage]`) is later smaller than `replicationFactor` — for example because Praefect's `[[virtual_storage.node]]` list was reduced in a config reload/restart while `default_replication_factor` was left at its old (higher) value, or because of any mismatch between `conns` and the configured factor — `len(secondaryNodes)` can be less than `replicationFactor-1`, causing `secondaryNodes[:replicationFactor-1]` to panic.

This is reachable by an ordinary user simply issuing a repository-creation RPC (e.g., `CreateRepository`, `CreateFork`) that Praefect routes through `RouteRepositoryCreation` → `assignRepositoryToNodes`, without any privileged action needed once the misconfiguration exists.

### Impact Explanation
A panic in a gRPC handler goroutine, if not recovered by a top-level `recover()` in the gRPC server, crashes the Praefect process handling the request, causing a denial of service for all repositories routed through that Praefect instance — impacting every client, not just the one who issued the triggering RPC. Even if gRPC's panic-recovery middleware catches it and returns an `Internal` error instead of crashing the process, it still causes hard failures for legitimate repository-creation attempts and requires an operator restart/reconfiguration to build a set of `secondaryNodes` large enough, with no in-request remedy — mirroring the "no remedy after commit" characteristic of the analog bug.

### Likelihood Explanation
This is a real, but narrower, edge case: it requires `DefaultReplicationFactor` to exceed the actual number of connected secondary storages for a virtual storage at request time. This is plausible after a Praefect configuration change (removing/rebalancing a virtual storage's nodes) that isn't matched by lowering `default_replication_factor`, since the two settings are configured independently and there's no runtime cross-check linking `conns` size to the persisted/configured replication factor at the moment of slicing. Given operators are explicitly warned in code comments to keep the factor `≤` the number of storages only at initial configuration time, drift between config generations is a realistic operational scenario.

### Recommendation
Before slicing, clamp or validate: `if replicationFactor-1 > len(secondaryNodes) { replicationFactor = len(secondaryNodes) + 1 }` or return a structured error (e.g., `structerr.NewFailedPrecondition`) when the configured replication factor exceeds the number of storages currently known/connected for the virtual storage, instead of trusting the value blindly the way `triggerSettlement()` trusted `proportionalRatioGovLP`/`proportionalRatioGovUser` without an explicit precondition check.

### Proof of Concept
1. Configure a Praefect virtual storage `vs1` with 4 nodes (primary + 3 secondaries) and set `default_replication_factor = 4` (valid at config time, `4 <= 4` nodes).
2. Reconfigure/restart Praefect with `vs1` now having only 2 nodes (primary + 1 secondary) while `default_replication_factor` remains `4` (no cross-validation ties the two together at reload).
3. An ordinary user issues `CreateRepository` (or `CreateFork`) targeting `vs1`.
4. `assignRepositoryToNodes` builds `secondaryNodes` with length 1 (only one non-primary connection) and executes `secondaryNodes[:replicationFactor-1]` = `secondaryNodes[:3]`, which panics with "slice bounds out of range" since `len(secondaryNodes) == 1 < 3`. [2](#0-1)

### Citations

**File:** internal/praefect/router_per_repository.go (L393-409)
```go
		primary, err := r.pickRandom(healthyNodes)
		if err != nil {
			return assignedNodes{}, err
		}

		var secondaryNodes []RouterNode
		for storage, conn := range r.conns[virtualStorage] {
			if storage == primary.Storage {
				continue
			}

			secondaryNodes = append(secondaryNodes, RouterNode{
				Storage:    storage,
				Connection: conn,
			})
		}

```

**File:** internal/praefect/router_per_repository.go (L410-420)
```go
		// replicationFactor being zero indicates it has not been configured. If so, we
		// fallback to the behavior of no assignments and replicate everywhere. Otherwise,
		// if we have a positive replication factor, we pick a random set of secondaries.
		if replicationFactor > 0 {
			// Select random secondaries according to the default replication factor.
			r.rand.Shuffle(len(secondaryNodes), func(i, j int) {
				secondaryNodes[i], secondaryNodes[j] = secondaryNodes[j], secondaryNodes[i]
			})

			secondaryNodes = secondaryNodes[:replicationFactor-1]
		}
```

**File:** internal/praefect/config/config.go (L285-295)
```go
// VirtualStorage represents a set of nodes for a storage
type VirtualStorage struct {
	Name  string  `json:"name" toml:"name,omitempty"`
	Nodes []*Node `json:"node" toml:"node,omitempty"`
	// DefaultReplicationFactor is the replication factor set for new repositories.
	// A valid value is inclusive between 1 and the number of configured storages in the
	// virtual storage. Setting the value to 0 or below causes Praefect to not store any
	// host assignments, falling back to the behavior of replicating to every configured
	// storage
	DefaultReplicationFactor int `json:"default_replication_factor" toml:"default_replication_factor,omitempty"`
}
```
