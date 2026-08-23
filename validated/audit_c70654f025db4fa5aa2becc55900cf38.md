### Title
Unbounded client-controlled `limit` in `ListPartitions` RPC enables unary-response DoS via unbounded partition-store iteration - ([File: internal/gitaly/service/partition/list_partitions.go])

### Summary
`ListPartitions` accepts a client-supplied `PaginationParameter.Limit` (an `int32`) and uses it directly as the loop-termination bound (`pageLimit`) for iterating the storage partition keyspace, with no upper bound (`MaxLimit`) enforced anywhere in the code path.

### Finding Description
The handler reads the pagination limit straight from the request and uses it unclamped: [1](#0-0) 

```go
pageLimit := 100
if paginationParams != nil {
    pageLimit = int(paginationParams.GetLimit())
    ...
}
```

The iteration loop then only stops when either the storage's partition iterator is exhausted or `len(partitions) >= pageLimit`: [2](#0-1) 

Because `PaginationParameter.Limit` is a plain `int32` field with no validation, a caller can set it to `math.MaxInt32` (or any very large positive value), causing `pageLimit` to effectively become unbounded relative to the actual size of the partition keyspace: [3](#0-2) 

Unlike the streaming, chunked RPCs in this codebase (`ListRefs`, `ListCommits`, `FindLocalBranches`) which send results incrementally via `stream.Send` as soon as a chunk is ready, `ListPartitions` is a **unary** RPC: it accumulates the entire `[]*gitalypb.Partition` slice in memory (`internal/gitaly/service/partition/list_partitions.go:46-54`) before returning a single response. This is analogous to vector 1 (`MaxLimit` too large) in the referenced sei-cosmos advisory — a client-supplied limit field bounds a KV-store scan with no upstream cap, and here the lack of a cap additionally forces unbounded in-memory buffering rather than bounded streaming.

There is also no cap analogous to `MaxOffset`/`MaxScanLimit`: nothing prevents a request from continuing to call `it.Next()` across an arbitrarily large number of partitions before the server-side limit check triggers, since the check only fires after appending to the in-memory slice.

### Impact Explanation
A caller with access to the `PartitionService` (any client with a valid Gitaly RPC/auth token, without special repository-level privileges — this is a storage/node-level RPC available to any authenticated caller, not conditioned on repository ownership) can request `Limit: 2147483647` and force the server to iterate the entire partition keyspace of a storage in one call, allocating and marshaling a proportionally large in-memory slice and gRPC response. On a storage with a large number of partitions (each repository and its forks/pools occupy a partition), this can consume significant CPU and memory in a single unary call, causing resource exhaustion or request timeouts that degrade or deny service to the node — a DoS impact of the same class described in the reference advisory.

### Likelihood Explanation
Likelihood is moderate: the request requires only a syntactically valid gRPC call with an oversized `limit` field — no privileged access, malicious peer, or token leakage is required beyond ordinary RPC authorization to the Gitaly node hosting `PartitionService`. The absence of any `MaxLimit` constant or validation in `ListPartitions` (contrast with the `VerifyPaginationOffset`/`MaxScanLimit` guards recommended in the reference fix) means the attack requires no crafted edge case, just a large integer.

### Recommendation
- Introduce a `MaxLimit` constant for `ListPartitions` (and other unary paginated RPCs) and clamp `pageLimit` to it, rejecting or normalizing values above the cap, mirroring the referenced fix's `MaxLimit = 1_000` approach.
- Consider converting `ListPartitions` to a streaming RPC (as `ListRefs`/`ListCommits` already are) so that large-but-legitimate result sets do not require full in-memory buffering.
- Validate `PaginationParameter.Limit` centrally (e.g., in `buildPaginationOpts` or a shared validator) so all paginated RPCs share a consistent upper bound rather than relying on each handler to self-limit.

### Proof of Concept
```go
resp, err := ptnClient.ListPartitions(ctx, &gitalypb.ListPartitionsRequest{
    StorageName: "default",
    PaginationParams: &gitalypb.PaginationParameter{
        Limit: math.MaxInt32,
    },
})
```
Against a storage with a very large number of partitions, this single unary call forces the server to iterate the full partition keyspace and buffer all resulting `Partition` entries in memory before returning, with no server-side limit reducing the work performed. This can be repeated concurrently by any authenticated client to amplify resource consumption on the target Gitaly node.

### Citations

**File:** internal/gitaly/service/partition/list_partitions.go (L23-33)
```go
	paginationParams := in.GetPaginationParams()
	startPartitionID := invalidPartitionID
	pageLimit := 100
	var err error
	if paginationParams != nil {
		pageLimit = int(paginationParams.GetLimit())
		startPartitionID, err = decodePageToken(paginationParams)
		if err != nil {
			return nil, structerr.NewInvalidArgument("invalid page token: %w", err)
		}
	}
```

**File:** internal/gitaly/service/partition/list_partitions.go (L46-59)
```go
	var partitions []*gitalypb.Partition
	for it.Next() {
		partitions = append(partitions, &gitalypb.Partition{
			Id: it.GetPartitionID().String(),
		})

		if len(partitions) >= pageLimit {
			break
		}
	}

	if err := it.Err(); err != nil {
		return nil, structerr.NewInternal("list partitions: %w", err)
	}
```

**File:** proto/go/gitalypb/shared.pb.go (L1045-1052)
```go
	// limit is the maximum number of objects the client will receive. When fully consuming
	// the response the client will receive _at most_ `limit` number of resulting objects.
	// Note that the number of response messages might be much lower, as some response
	// messages already send multiple objects per message.
	// When the limit is smaller than 0, it will be normalized to 2147483647
	// on the server side. When limit is not set, it defaults to 0, and no
	// results are send in the response.
	Limit         int32 `protobuf:"varint,2,opt,name=limit,proto3" json:"limit,omitempty"`
```
