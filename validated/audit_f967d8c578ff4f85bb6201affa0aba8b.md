### Title
Unbounded `blob_ids` list in `GetLFSPointers` allows unary-response size blow-up - (File: proto/blob.proto)

### Summary
`GetLFSPointers` is defined as a **non-streaming** (unary) RPC whose request accepts an unbounded `repeated string blob_ids` field and whose response accumulates a `repeated LFSPointer lfs_pointers` collection — including each pointer's raw blob bytes — into a **single** protobuf message. [1](#0-0) 

### Finding Description
Gitaly's own RPC design rules require that any `repeated` field which "can grow without limit" or any payload that "could exceed gRPC's message-size limit" must be sent via a `stream`, exactly to avoid the class of bug described in the external report (an unbounded collection forced into one fixed-capacity buffer/message, causing decode failure or excessive cost once the limit is hit). [2](#0-1) [3](#0-2) 

`GetLFSPointers`, however, does not follow this pattern: the request lets a caller supply an arbitrary-length `blob_ids` list ("Must be a non-empty list of blobs IDs to fetch" — no stated upper bound), and the corresponding unary response packs every resolved `LFSPointer` (each carrying its own `data` bytes) into one `GetLFSPointersResponse` message rather than a stream of chunked responses like the sibling RPCs `GetBlob`/`GetBlobs`/`ListBlobs`, which explicitly document chunking when content "exceed[s] the maximum gRPC message size." [4](#0-3) [5](#0-4) [6](#0-5) 

This mirrors the "unbounded array forced into one fixed-capacity storage cell" class from the ink! report: a caller-controlled array size is directly translated into a single response buffer with no independent bound, so once the number of requested `blob_ids` (and thus the number of returned `LFSPointer` entries) is large enough, the resulting message can approach or exceed gRPC's message-size ceiling, causing the RPC to fail with `ResourceExhausted`/serialization errors, or to consume disproportionate CPU/memory building and marshalling the oversized message before that limit is even hit.

Note: I was unable to retrieve the body of `internal/gitaly/service/blob/lfs_pointers.go` (only its function signatures were located) before the tool budget was exhausted, so I could not confirm whether the handler internally enforces any additional cap on `len(blob_ids)` beyond the proto-level "non-empty" requirement. The architectural issue — a unary response with an unbounded repeated field, contrary to Gitaly's own streaming guideline — is confirmed directly from `proto/blob.proto`.

### Impact Explanation
A caller (any client with access to the `BlobService`, e.g. GitLab Rails via a project with many LFS objects, or any RPC-capable actor in deployments where authorization does not restrict blob enumeration) can request LFS pointers for a very large `blob_ids` list. This can produce a single, unstreamed response large enough to exceed gRPC's default/configured max message size, causing the RPC to fail outright (denial of service for the LFS pointer lookup path) or to force expensive full-message buffering/marshalling on the Gitaly node before failing — analogous to the ink! contract's decode failure and high "gas" cost once the single-buffer limit is exceeded.

### Likelihood Explanation
Triggering this requires only crafting a `GetLFSPointersRequest` with a sufficiently long `blob_ids` list — no privileged access, token leakage, or malicious peer is needed, only an ordinary RPC caller supplying a crafted request field, consistent with the validation scope.

### Recommendation
Convert `GetLFSPointers` to a server-streaming RPC (`returns (stream GetLFSPointersResponse)`), following the same chunking pattern already used by `GetBlobs`/`ListBlobs`/`ListAllBlobs`, and enforce an explicit upper bound on the number of `blob_ids` accepted per request, returning `InvalidArgument` if exceeded.

### Proof of Concept
Not independently executed; based on static analysis of the `.proto` contract: send a `GetLFSPointersRequest` with `blob_ids` containing a very large number of entries (e.g., tens of thousands of valid blob OIDs) against a repository with matching LFS pointer blobs, and observe the unary `GetLFSPointersResponse` failing to marshal/send once the aggregated `lfs_pointers` payload exceeds the gRPC message-size limit.

### Citations

**File:** proto/blob.proto (L92-101)
```text
// GetBlobResponse is a response for the GetBlob RPC. Multiple responses will be returned when the blob is large and
// thus doesn't fit into a single response.
message GetBlobResponse {
  // size is the size of the blob. Present only in first response message.
  int64 size = 1;
  // data is a chunk of data.
  bytes data = 2;
  // oid of the actual blob returned. Empty if no blob was found.
  string oid = 3;
}
```

**File:** proto/blob.proto (L123-127)
```text
// GetBlobsResponse is a response for the GetBlobs RPC and identifies a single blob. Multiple responses can be returned
// for the same blob in case its data is longer than the gRPC message limit. Subsequent messages for the same blob will
// only have their data field set. Blobs which cannot be found will only have their path and revision set, but will
// otherwise be empty.
message GetBlobsResponse {
```

**File:** proto/blob.proto (L236-266)
```text
// LFSPointer is a git blob which points to an LFS object.
message LFSPointer {
  // size is the size of the blob. This is not the size of the LFS object
  // pointed to.
  int64 size = 1;
  // data is the bare data of the LFS pointer blob. It contains the pointer to
  // the LFS data in the format specified by the LFS project.
  bytes data = 2;
  // oid is the object ID of the blob.
  string oid = 3;
  // file_size is the size given when parsing the LFS pointer spec.
  int64 file_size = 4;
  // file_oid is the object id given when parsing the LFS pointer spec.
  bytes file_oid = 5;
}

// GetLFSPointersRequest is a request for the GetLFSPointers RPC.
message GetLFSPointersRequest {
  // repository is the repository for which LFS pointers should be retrieved
  // from.
  Repository repository = 1[(target_repository)=true];
  // blob_ids is the list of blobs to retrieve LFS pointers from. Must be a
  // non-empty list of blobs IDs to fetch.
  repeated string blob_ids = 2;
}

// GetLFSPointersResponse is a response for the GetLFSPointers RPC.
message GetLFSPointersResponse {
  // lfs_pointers is the list of LFS pointers which were requested.
  repeated LFSPointer lfs_pointers = 1;
}
```

**File:** doc/protobuf.md (L201-207)
```markdown
### Stream patterns

Protobuf supports streaming RPCs which allow for multiple request or response
messages to be sent in a single RPC call. We use these whenever it is expected
that an RPC may be invoked with lots of input parameters or when it may generate
a lot of data. This is required by limitations in the gRPC framework where
messages should not typically be larger than 1MB.
```

**File:** skills/gitaly-rpc-development/SKILL.md (L42-47)
```markdown
- **Stream unbounded or oversized payloads.** Use a `stream` when the result is an unbounded
  sequence (a `repeated` collection that can grow without limit) or a payload that could exceed
  gRPC's message-size limit, sending it in batches/chunks. For high-volume byte payloads (e.g. Git
  fetch/pack data), Gitaly avoids gRPC messages entirely and uses a **sidechannel**
  ([doc/sidechannel.md](../../doc/sidechannel.md)); the full streaming patterns are in
  [doc/protobuf.md](../../doc/protobuf.md).
```
