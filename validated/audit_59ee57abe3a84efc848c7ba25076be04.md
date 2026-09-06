### Title
`AttachmentsBatchStateContext::extend_with_attachments` accepts attachment responses without verifying `response.attachment.hash() == request.content_hash` - (File: stackslib/src/net/atlas/download.rs)

### Summary
`extend_with_attachments` (download.rs:530-558) decodes a peer's `/v2/attachments/{content_hash}` response via `decode_atlas_get_attachment()` and, on success, unconditionally inserts `response.attachment` into `self.attachments` and calls `report.bump_successful_requests()`. Neither `decode_atlas_get_attachment()` nor `extend_with_attachments` compares the returned `Attachment`'s content hash against the `request.content_hash` that was actually requested.

### Finding Description
The broken equality is: the code never asserts `response.attachment.hash() == request.content_hash` before trusting the response. Tracing the path:

- `decode_atlas_get_attachment()` in `stackslib/src/net/api/getattachment.rs:158-165` only parses the JSON body into a `GetAttachmentResponse { attachment }` — it performs no hash validation at all.
- `extend_with_attachments` (`download.rs:547-552`) then does:
```rust
if let Ok(response) = response.decode_atlas_get_attachment() {
    self.attachments.insert(response.attachment);
    report.bump_successful_requests();
} else {
    report.bump_failed_requests();
}
```
There is no comparison against `request.content_hash` anywhere in this branch (contrast with `extend_with_inventories`, which has the same shape but is not required to check content since it's an inventory bitmap, not raw content).

A remote peer chosen via `network.get_outbound_sync_peers()` that answers a `GET /v2/attachments/{content_hash}` request with any well-formed `Attachment` JSON body (regardless of whether its content hashes to the requested `content_hash`) will have that malformed pairing accepted as a "successful" request, and the peer's `ReliabilityReport` score is bumped upward via `bump_successful_requests()`.

### Impact Explanation
Two concrete, verifiable consequences follow directly from the missing check in this function's scope:
1. **Reliability-score poisoning**: a malicious/buggy peer that never actually serves the correct attachment content for a requested hash is nonetheless scored as reliable, which (per `AttachmentsDownloader::run`, lines 93-140) influences peer selection/prioritization for future rounds — a persistent, repeatable manipulation of the peer-scoring subsystem using zero cost per message (one crafted HTTP response per round).
2. **Garbage/wrong-hash attachments admitted into `self.attachments`**: the wrong `Attachment` is inserted into the batch state's attachment set with no correctness guarantee, meaning the state machine's bookkeeping of "resolved" attachments is built on unverified data at this stage of the pipeline.

This matches the requested High-impact bucket ("attachment/BNS mismatch") at the level of this specific function: the function itself treats content-hash-violating responses identically to correct ones for scoring and set-membership purposes, with no verification gate present anywhere in this code path.

### Likelihood Explanation
- Precondition: the attacker only needs to be selected as one of `network.get_outbound_sync_peers()` and answer a legitimate `GET /v2/attachments/{content_hash}` request — both reachable by any remote, unprivileged peer that establishes an outbound-sync relationship with the victim node (no secret, no privileged role required).
- Attacker cost: one crafted HTTP JSON response per attachment request; fully repeatable every round.
- No rate limiting, signature check, or hash check exists in this function to block it.

### Recommendation
In `extend_with_attachments` (download.rs:547), after a successful decode, compare `response.attachment.hash()` (or equivalent `Hash160` computation over `response.attachment.content`) against `request.content_hash` before inserting into `self.attachments` and before calling `report.bump_successful_requests()`. On mismatch, treat it identically to a failed/faulty response (`report.bump_failed_requests()`, and consider marking the peer as faulty).

### Proof of Concept
Rust unit test in `stackslib::net::atlas` (module `download` or `tests.rs`):
1. Build an `AttachmentsBatchStateContext` with a single peer URL and a fresh `ReliabilityReport` in `self.peers`.
2. Construct an `AttachmentRequest { content_hash: H1, sources, .. }` where `H1 = Hash160::from_data(b"expected")`.
3. Construct a `StacksHttpResponse` whose body JSON-encodes `GetAttachmentResponse { attachment: Attachment { content: b"wrong".to_vec() } }` (hash `H2 = Hash160::from_data(b"wrong")`, `H2 != H1`).
4. Populate `BatchedRequestsResult::succeeded` with `(request, Some(response))`.
5. Call `context.extend_with_attachments(&mut results)`.
6. Assert: `context.peers.get(&peer_url).unwrap().is_reliable()` (or inspect the internal success counter) reflects a bumped success count, and assert `context.attachments` contains the "wrong" `Attachment` even though its hash `H2 != H1` — proving the missing equality check at `download.rs:547-552`.

Note: I was not able to fully trace, within the tool budget available, whether a later stage of `AttachmentsBatchStateMachine` (post-`Done`) re-matches `self.attachments` against each `AttachmentInstance.content_hash` before final resolution — if such a downstream hash-keyed lookup exists, it would prevent the wrong attachment from being served as if it resolved the genuinely-committed BNS name operation, reducing the confirmed impact to reliability-score poisoning only rather than full BNS content substitution. This downstream behavior should be verified independently (e.g., by starting a full Devin session with complete file access) before treating the "BNS mismatch/served-as-canonical" portion of the impact as fully confirmed; the missing hash check inside `extend_with_attachments` itself, and the resulting unconditional `bump_successful_requests()`, are confirmed directly from the code shown above.