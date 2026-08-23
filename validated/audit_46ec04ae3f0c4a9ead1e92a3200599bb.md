### Title
Non-collision-resistant partition key derivation via string concatenation in `NewPartitionKey` - (File: internal/gitaly/storage/raftmgr/replica.go)

### Summary
`NewPartitionKey` computes a `RaftPartitionKey` by hashing the naive string concatenation of `storageName` and `partitionID.String()` rather than a length-prefixed or delimited encoding. This mirrors the reported `abi.encodePacked()` collision class: two distinct `(storageName, partitionID)` pairs can serialize to the identical byte string fed into `sha256.Sum256`, producing the same partition key.

### Finding Description
`NewPartitionKey` is defined as: [1](#0-0) 

`storageName` is an arbitrary configured storage name (string) and `partitionID.String()` is simply the base-10 decimal representation of a `uint64`: [2](#0-1) 

Because the two components are concatenated with no delimiter or length prefix before hashing, a storage name that ends with digits can collide with a different storage name plus a different partition ID whenever the concatenated byte strings are identical, e.g. `storageName="node1"`, `partitionID=23` yields the same input (`"node123"`) as `storageName="node12"`, `partitionID=3`. This key is the sole identifier used to look up and route Raft state:
- The registry indexes running replicas purely by this string value: [3](#0-2) 
- The persistent routing table stores/retrieves cluster membership entries keyed by the same value: [4](#0-3) 
- Incoming `JoinCluster` and inbound Raft message streams (`Receive`) dispatch strictly based on this key to select which replica instance receives the entries: [5](#0-4) 

The proto documents the intended invariant that this value must be "globally-unique": [6](#0-5) , which the concatenation-without-separator construction does not guarantee.

### Impact Explanation
If storage names are configurable (an administrator or automation naming convention could produce names ending in digits, e.g. per-node names like `gitaly-1`, `storage12`), two different `(storageName, partitionID)` pairs could hash to the same `RaftPartitionKey`. Since the registry (`GetReplica`/`RegisterReplica`) and routing table (`GetEntry`/`UpsertEntry`) are keyed solely on this string, a collision would cause:
- Raft messages, log entries, or `JoinCluster` requests intended for one partition to be routed to and processed by the replica of a different, unrelated partition (cross-partition/cross-repository object and state confusion).
- Overwriting of routing table entries (leader/replica membership) between unrelated partitions via `UpsertEntry`, potentially causing loss of cluster consensus state or admitting a node into the wrong replica set.

This is a real (though narrow, configuration-dependent) confidentiality/integrity risk within the Raft-based storage subsystem when it is enabled, distinct from the "malicious peer" exclusion since it is a systemic hash-construction defect reachable by ordinary storage/partition naming, not by an adversarial peer forging messages.

### Likelihood Explanation
Exploitability depends on control over storage names or the ability to influence which storage names get paired with which partition IDs (both are operator/config-plane inputs, not directly attacker-controlled via ordinary git push/fetch RPCs). This lowers likelihood relative to a pure remote-triggerable vulnerability, but the underlying defect (collision-prone key derivation) is concretely present and directly analogous to the reported CREATE2 salt collision issue caused by `abi.encodePacked`. I could not confirm within the available code whether storage names are fully attacker-influenced by an unprivileged git client, so likelihood should be treated as low-to-moderate and tied to deployment/config practices rather than to a purely remote unauthenticated trigger.

### Recommendation
Replace the raw concatenation with a collision-resistant encoding, e.g. hash length-prefixed fields or use a fixed delimiter plus length prefixes:
```go
func NewPartitionKey(storageName string, partitionID storage.PartitionID) *gitalypb.RaftPartitionKey {
    h := sha256.New()
    binary.Write(h, binary.BigEndian, uint64(len(storageName)))
    h.Write([]byte(storageName))
    h.Write(partitionID.MarshalBinary())
    return &gitalypb.RaftPartitionKey{Value: fmt.Sprintf("%x", h.Sum(nil))}
}
```
This eliminates ambiguity between the two input fields, matching the report's recommendation to use `abi.encode()`-equivalent (unambiguous, length-delimited) encoding instead of packed concatenation.

### Proof of Concept
Given `storageName = "node1"`, `partitionID = 23`, the hashed input is `"node1" + "23" = "node123"`.
Given `storageName = "node12"`, `partitionID = 3`, the hashed input is `"node12" + "3" = "node123"`.
Both produce identical `sha256.Sum256([]byte("node123"))`, so `NewPartitionKey("node1", 23)` and `NewPartitionKey("node12", 3)` return the same `RaftPartitionKey.Value`, causing `ReplicaRegistry.GetReplica`/`RegisterReplica` and `kvRoutingTable.GetEntry`/`UpsertEntry` to treat these two distinct partitions as one. [1](#0-0)

### Citations

**File:** internal/gitaly/storage/raftmgr/replica.go (L1260-1266)
```go
// NewPartitionKey creates a partition key for a newly-minted partition. A partition should only
// ever have a single RaftPartitionKey, computed by the replica which first created the partition.
func NewPartitionKey(storageName string, partitionID storage.PartitionID) *gitalypb.RaftPartitionKey {
	return &gitalypb.RaftPartitionKey{
		Value: fmt.Sprintf("%x", sha256.Sum256([]byte(storageName+partitionID.String()))),
	}
}
```

**File:** internal/gitaly/storage/partition_id.go (L23-26)
```go
// String returns a base 10 string representation of the PartitionID.
func (id PartitionID) String() string {
	return strconv.FormatUint(uint64(id), 10)
}
```

**File:** internal/gitaly/storage/raftmgr/replica_registry.go (L32-43)
```go
// GetReplica returns the replica for a given partitionKey.
func (r *raftRegistry) GetReplica(key *gitalypb.RaftPartitionKey) (RaftReplica, error) {
	if mgr, ok := r.replicas.Load(key.GetValue()); ok {
		return mgr.(RaftReplica), nil
	}
	return nil, errNoReplicaFound.WithMetadata("partition_key", key)
}

// RegisterReplica registers a replica for a given partitionKey.
func (r *raftRegistry) RegisterReplica(key *gitalypb.RaftPartitionKey, replica RaftReplica) {
	r.replicas.LoadOrStore(key.GetValue(), replica)
}
```

**File:** internal/gitaly/storage/raftmgr/routing_table.go (L16-18)
```go
func routingKey(partitionKey *gitalypb.RaftPartitionKey) []byte {
	return []byte(fmt.Sprintf("raft/%s", partitionKey.GetValue()))
}
```

**File:** internal/gitaly/storage/raftmgr/grpc_transport.go (L206-216)
```go
// Receive receives a stream of Raft messages and processes them.
func (t *GrpcTransport) Receive(ctx context.Context, partitionKey *gitalypb.RaftPartitionKey, raftMsg raftpb.Message) error {
	// Retrieve the replica from the registry, assumption is that all the messages are from the same partition key.
	var replica RaftReplica
	replica, err := t.registry.GetReplica(partitionKey)
	if err != nil {
		t.logger.WithFields(log.Fields{
			"raft.partition_key": partitionKey.GetValue(),
		}).Error("replica has not been created yet")
		return nil
	}
```

**File:** proto/cluster.proto (L39-47)
```text
// RaftPartitionKey is a globally-unique identifier for a replicated partition.
// The replica which minted the partition is responsible for computing the RaftPartitionKey,
// which is a hash of the storage name and partition ID. The key is then consumed
// as-is by other replicas wishing to store the partition.
message RaftPartitionKey {
  // value is the SHA256 digest of the storage name and partition ID of the
  // newly-minted partition.
  string value = 1;
}
```
