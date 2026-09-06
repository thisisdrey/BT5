Confirmed: `try_parse_response` for `RPCGetAttachmentRequestHandler` (server side) and `decode_atlas_get_attachment` (client side) never check the returned `Attachment`'s hash against the requested `attachment_hash`/`content_hash` at all — the response is just JSON-deserialized. The check-against-request logic lives solely in `extend_with_attachments`, which does not perform it either.

### Title
Atlas attachment downloader trusts and rewards peers serving mismatched attachment content without verifying against the requested content hash - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` decodes a peer's `GetAttachmentResponse` and unconditionally inserts `response.attachment` into `self.attachments` and calls `report.bump_successful_requests()` whenever the response deserializes successfully, without ever comparing `response.attachment.hash()` to the `request.content_hash` that was actually requested. A malicious outbound-sync peer can therefore serve arbitrary self-consistent (but unrelated) attachment bytes for any `/v2/attachments/<content_hash>` request and be rewarded with a positive reliability bump.

### Finding Description
The broken equality: the code never asserts `response.attachment.hash() == request.content_hash`. In `extend_with_attachments` [1](#0-0) , for each `(request, response)` pair in `results.succeeded`, the code decodes the response via `response.decode_atlas_get_attachment()` and, on success, does `self.attachments.insert(response.attachment)` followed by `report.bump_successful_requests()` — with no reference to `request.content_hash` anywhere in the function body.

Tracing further: `AttachmentRequest` carries `content_hash: Hash160` set from the pending `AttachmentInstance.content_hash` in `get_prioritized_attachments_requests` [2](#0-1) . The HTTP client-side decode path, `StacksHttpResponse::decode_atlas_get_attachment`, purely JSON-deserializes the body into `GetAttachmentResponse` with no hash validation [3](#0-2) . The server-side handler `RPCGetAttachmentRequestHandler::try_handle_request` looks up by hash and returns a self-consistent `Attachment` for whatever hash is in its own `atlasdb` [4](#0-3) , but a malicious node's RPC handler does not have to be the stock implementation — an attacker's own peer can return any bytes it wants for the `attachment_hash` path parameter, since nothing on the requester's side checks the returned content against what was asked for.

Downstream in `AttachmentsDownloader::run`'s `Done` arm, attachments drained from `context.attachments` are keyed by their own (self-consistent) `attachment.hash()` when calling `find_all_attachment_instances` and `insert_instantiated_attachment` [5](#0-4) , so no *instance* gets falsely resolved to attacker data (the DB lookup by hash is self-consistent). However, the `ReliabilityReport` update — the actual damage — happens unconditionally inside `extend_with_attachments` before any of that reconciliation, purely based on "did the response decode as JSON," not "did it answer the actual question."

### Impact Explanation
A malicious peer serving arbitrary attachment bytes unrelated to what was requested is scored as a "successful" responder via `bump_successful_requests()`, inflating its `ReliabilityReport`. This report directly feeds `get_prioritized_attachments_inventory_requests` and `get_prioritized_attachments_requests`, which propagate `reliability_report.clone()` into the priority queue used to prioritize which peer's inventory/attachment claims are trusted for future requests [6](#0-5)  and [7](#0-6) . This steers future rarest-first BNS attachment fetch preference toward a peer that has demonstrated it serves wrong data, rather than being downweighted/penalized as a faulty responder. This matches "attachment/BNS mismatch" high-impact category — no attachment instance is falsely resolved (that part is guarded by hash-keyed lookups), but the trust/reliability signal used to pick sync peers is corrupted by unverified data.

### Likelihood Explanation
Precondition: attacker runs an outbound-sync peer that the node has selected (via `get_outbound_sync_peers`), and previously gossiped/served an inventory page claiming to have the requested attachment so it gets included as a `source` in `get_prioritized_attachments_requests`. This is achievable by any unprivileged remote peer that can establish a P2P connection and exchange inventories — no secret or privileged role is required. The attacker's cost is just running a peer and answering `/v2/attachments/<hash>` with arbitrary self-consistent JSON; this is repeatable on every attachment request cycle.

### Recommendation
In `extend_with_attachments`, after `decode_atlas_get_attachment()` succeeds, compare `response.attachment.hash()` against `request.content_hash` before inserting into `self.attachments` and before calling `report.bump_successful_requests()`. On mismatch, treat it the same as a failed/faulty response (`report.bump_failed_requests()`, and consider adding the peer to `faulty_peers`/deregistering the event) rather than rewarding it.

### Proof of Concept
Add a test in `stackslib/src/net/atlas/tests.rs` (or a new test in `download.rs`'s module) that:
1. Constructs an `AttachmentsBatchStateContext` with one peer and a `ReliabilityReport::empty()`.
2. Builds an `AttachmentRequest { content_hash: H1, .. }` and inserts it as the key in a `BatchedRequestsResult::succeeded` map, paired with a `Some(StacksHttpResponse)` whose JSON body is a `GetAttachmentResponse { attachment: Attachment { content: b"unrelated bytes".to_vec() } }`, whose `.hash()` computes to `H2 != H1`.
3. Calls `context.extend_with_attachments(&mut results)`.
4. Asserts that `context.peers.get(&peer_url).unwrap()` shows a bumped `request_count`/success ratio consistent with `bump_successful_requests()` having been called, and that `context.attachments` contains the attacker's `Attachment` (hash `H2`) even though it was requested under `content_hash = H1` — demonstrating the missing equality check `response.attachment.hash() == request.content_hash`.

### Citations

**File:** stackslib/src/net/atlas/download.rs (L153-169)
```rust
            AttachmentsBatchStateMachine::Done(ref mut context) => {
                for attachment in context.attachments.drain() {
                    let attachments_instances = network
                        .atlasdb
                        .find_all_attachment_instances(&attachment.hash())
                        .map_err(net_error::DBError)?;
                    network
                        .atlasdb
                        .insert_instantiated_attachment(&attachment)
                        .map_err(net_error::DBError)?;
                    for attachment_instance in attachments_instances.into_iter() {
                        resolved_attachments.push((attachment_instance, attachment.clone()));
                    }
                    context
                        .attachments_batch
                        .resolve_attachment(&attachment.hash())
                }
```

**File:** stackslib/src/net/atlas/download.rs (L384-398)
```rust
            for (peer_url, reliability_report) in self.peers.iter() {
                for pages in pages_batches.iter() {
                    let request = AttachmentsInventoryRequest {
                        url: peer_url.clone(),
                        reliability_report: reliability_report.clone(),
                        contract_id: contract_id.clone(),
                        pages: pages.clone(),
                        stacks_block_height: self.attachments_batch.stacks_block_height,
                        index_block_hash: self.attachments_batch.index_block_hash.clone(),
                        canonical_stacks_tip_height: self
                            .attachments_batch
                            .canonical_stacks_tip_height,
                    };
                    queue.push(request);
                }
```

**File:** stackslib/src/net/atlas/download.rs (L454-459)
```rust
                    let report = self
                        .peers
                        .get(peer_url)
                        .expect("Atlas: unable to retrieve reliability report for peer");
                    sources.insert(peer_url.clone(), report.clone());
                }
```

**File:** stackslib/src/net/atlas/download.rs (L466-474)
```rust
                // Success, we found at least one inventory including the attachment we're looking for.
                let request = AttachmentRequest {
                    sources,
                    content_hash: content_hash.clone(),
                    stacks_block_height: self.attachments_batch.stacks_block_height,
                    canonical_stacks_tip_height: self.attachments_batch.canonical_stacks_tip_height,
                };
                enqueued.insert(content_hash);
                queue.push(request);
```

**File:** stackslib/src/net/atlas/download.rs (L530-553)
```rust
    pub fn extend_with_attachments(
        mut self,
        results: &mut BatchedRequestsResult<AttachmentRequest>,
    ) -> AttachmentsBatchStateContext {
        for (request, response) in results.succeeded.drain() {
            let report = self
                .peers
                .get_mut(request.get_url())
                .expect("Atlas: unable to retrieve reliability report for peer");

            let response = if let Some(r) = response {
                r
            } else {
                report.bump_failed_requests();
                continue;
            };

            if let Ok(response) = response.decode_atlas_get_attachment() {
                self.attachments.insert(response.attachment);
                report.bump_successful_requests();
            } else {
                report.bump_failed_requests();
            }
        }
```

**File:** stackslib/src/net/api/getattachment.rs (L93-119)
```rust
    fn try_handle_request(
        &mut self,
        preamble: HttpRequestPreamble,
        _contents: HttpRequestContents,
        node: &mut StacksNodeState,
    ) -> Result<(HttpResponsePreamble, HttpResponseContents), NetError> {
        let attachment_hash = self
            .attachment_hash
            .take()
            .ok_or(NetError::SendError("Missing `attachment_hash`".into()))?;

        let attachment_res = node.with_node_state(
            |network, _sortdb, _chainstate, _mempool, _rpc_args| match network
                .get_atlasdb()
                .find_attachment(&attachment_hash)
            {
                Ok(Some(attachment)) => Ok(GetAttachmentResponse { attachment }),
                _ => {
                    let msg = "Unable to find attachment".to_string();
                    warn!("{msg}");
                    Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpNotFound::new(msg),
                    ))
                }
            },
        );
```

**File:** stackslib/src/net/api/getattachment.rs (L158-165)
```rust
impl StacksHttpResponse {
    pub fn decode_atlas_get_attachment(self) -> Result<GetAttachmentResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: GetAttachmentResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
    }
```
