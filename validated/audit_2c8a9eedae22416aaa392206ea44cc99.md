### Title
Unauthenticated write of forged Atlas attachments due to missing hash field in `GetAttachmentResponse` and no post-decode equality check - (File: stackslib/src/net/atlas/download.rs, stackslib/src/net/api/getattachment.rs)

### Summary
`GetAttachmentResponse` carries only `{ attachment: Attachment }` with no claimed/requested hash field, so `decode_atlas_get_attachment` cannot reject mismatched content at deserialization time. The only consumer, `AttachmentsBatchStateContext::extend_with_attachments`, does not compare `response.attachment.hash()` to the original `AttachmentRequest.content_hash` either, so arbitrary attacker-supplied bytes for any requested hash are inserted straight into the AtlasDB.

### Finding Description
The broken equality is: `attachment.hash() == request.content_hash` is never checked anywhere in the download call chain.

- Request side: `AttachmentsBatchStateContext::get_prioritized_attachments_requests` builds an `AttachmentRequest { content_hash, .. }` [1](#0-0)  and dispatches it over HTTP via `PeerNetwork::begin_request`.
- Response side: the handler `RPCGetAttachmentRequestHandler::try_parse_response` just wraps the JSON body into a `GetAttachmentResponse` with `parse_json` [2](#0-1) , and `decode_atlas_get_attachment` does the same, deserializing straight into the struct with no hash field to check against [3](#0-2) .
- Consumption: `extend_with_attachments` takes each `(request, response)` pair — so it *does* have access to `request.get_url()`/`request` (which carries `content_hash`) — but only uses it to look up the peer's `ReliabilityReport`; the attachment itself is inserted into `self.attachments` (`HashSet<Attachment>`) with **no comparison to `request.content_hash`** whatsoever [4](#0-3) .
- Final write: in `AttachmentsDownloader::run`, every attachment collected in `context.attachments` is committed unconditionally: `network.atlasdb.insert_instantiated_attachment(&attachment)` [5](#0-4) . `Attachment::hash()` (`Hash160::from_data(&self.content)`) is used here only to look up existing `attachment_instances` and to resolve the batch — never to assert equality against the originally requested hash.

Attacker exploit: a peer serving `/v2/attachments/{hash}` on the P2P/RPC HTTP surface (an unprivileged, reachable outbound sync peer) can return `{"attachment": {"content": "<arbitrary bytes>", ...}}` for *any* requested hash. Since no code path — not the wire struct, not `extend_with_attachments`, not the final DB insert — checks `Hash160::from_data(content) == requested content_hash`, the arbitrary bytes are stored as if they were the legitimately-committed attachment for that hash and later served to other RPC callers as canonical attachment data for that `content_hash`.

### Impact Explanation
This is an unauthenticated write to node state: the AtlasDB attachment table is poisoned with attacker-chosen bytes keyed under a hash that does not match. Any subsequent local RPC lookup via `/v2/attachments/{hash}` (`RPCGetAttachmentRequestHandler::try_handle_request`) would serve the poisoned/mismatched content as canonical, and any `attachment_instance` resolution built on top of it (e.g., BNS-related zonefile references bound on-chain to that hash) would be served with mismatched content — a High/Critical-adjacent Atlas/BNS integrity break, repeatable per attachment sync cycle from any peer the node happens to sync attachments from.

### Likelihood Explanation
Preconditions: the victim node must be actively running the Atlas attachment downloader and have queued an `AttachmentInstance` referencing some `content_hash` (this happens automatically as part of normal BNS/Atlas sync), and the attacker just needs to be one of the outbound sync peers the node polls (`network.get_outbound_sync_peers()`), which requires no privileged role — any reachable HTTP peer serving the `/v2/attachments/:hash` endpoint qualifies. Attacker cost is a single crafted HTTP 200 response; it is fully repeatable for every attachment the victim requests.

### Recommendation
Add an explicit hash check in `extend_with_attachments` (or in `decode_atlas_get_attachment`) comparing `response.attachment.hash()` against `request.content_hash` before inserting into `self.attachments`; reject and treat as a faulty-peer/failed-request response on mismatch. Optionally add a `requested_content_hash` field to `GetAttachmentResponse` for defense-in-depth wire-level verification, but the mandatory fix is the equality check before `insert_instantiated_attachment`.

### Proof of Concept
Rust test plan in `stackslib/src/net/atlas/download.rs` (or an integration test extending the existing `getattachment.rs` tests):
1. Construct an `AttachmentRequest` with `content_hash = Hash160::from_data(b"legit")`.
2. Simulate a peer HTTP response body `{"attachment":{"content":"<hex of b\"evil\">"}}` via `decode_atlas_get_attachment`.
3. Call `AttachmentsBatchStateContext::extend_with_attachments` with a `BatchedRequestsResult` mapping that request to the crafted response.
4. Assert that `context.attachments` contains an `Attachment` whose `.hash()` != the original `content_hash` — proving no equality check exists (`assert_ne!(attachment.hash(), original_content_hash)` should pass, demonstrating the bug — a fixed version would instead drop the mismatched entry).
5. Follow through `AttachmentsDownloader::run` to confirm `atlasdb.insert_instantiated_attachment` is called with this mismatched attachment, completing the poisoning.

### Citations

**File:** stackslib/src/net/atlas/download.rs (L152-169)
```rust
        match progress {
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

**File:** stackslib/src/net/atlas/download.rs (L467-474)
```rust
                let request = AttachmentRequest {
                    sources,
                    content_hash: content_hash.clone(),
                    stacks_block_height: self.attachments_batch.stacks_block_height,
                    canonical_stacks_tip_height: self.attachments_batch.canonical_stacks_tip_height,
                };
                enqueued.insert(content_hash);
                queue.push(request);
```

**File:** stackslib/src/net/atlas/download.rs (L530-558)
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
        let mut events_ids = results.faulty_peers.keys().copied().collect::<Vec<usize>>();
        self.events_to_deregister.append(&mut events_ids);

        self
    }
```

**File:** stackslib/src/net/api/getattachment.rs (L134-143)
```rust
impl HttpResponse for RPCGetAttachmentRequestHandler {
    fn try_parse_response(
        &self,
        preamble: &HttpResponsePreamble,
        body: &[u8],
    ) -> Result<HttpResponsePayload, Error> {
        let pages: GetAttachmentResponse = parse_json(preamble, body)?;
        Ok(HttpResponsePayload::try_from_json(pages)?)
    }
}
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
