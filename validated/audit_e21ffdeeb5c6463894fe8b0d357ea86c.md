### Title
Atlas attachment content is never validated against the requested content hash, allowing forged attachment content to be stored - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` takes the decoded `GetAttachmentResponse` from any peer that answered a pending `AttachmentRequest` and inserts `response.attachment` into `self.attachments` with no comparison between `response.attachment.hash()` and the `request.content_hash` that was actually requested. A malicious peer can therefore return arbitrary attachment bytes for any hash it was asked to serve.

### Finding Description
The download flow works as follows: `AttachmentsDownloader` builds `AttachmentRequest`s keyed by `content_hash` [1](#0-0) , issues them over HTTP via `AttachmentRequest::make_request_type` / `StacksHttpRequest::new_getattachment` [2](#0-1) , and the peer's HTTP response is decoded by `StacksHttpResponse::decode_atlas_get_attachment`, which simply JSON-deserializes the body into a `GetAttachmentResponse { attachment }` with no cryptographic check tying it to the requested hash [3](#0-2) .

The result is consumed in `extend_with_attachments`:
```rust
if let Ok(response) = response.decode_atlas_get_attachment() {
    self.attachments.insert(response.attachment);
    report.bump_successful_requests();
}
``` [4](#0-3) 

There is no `response.attachment.hash() == request.content_hash` check anywhere in this function (lines 530-558) before the attachment is inserted into `self.attachments`, unlike what a content-addressed store must enforce. Any peer that is a source for a given `content_hash` (added to `sources` purely because its previously-gossiped inventory claimed to have that hash, see lines 454-458) can, when asked for that hash, return any byte content it wants and it will be accepted into the batch context.

### Impact Explanation
A remote, unprivileged peer that only needs to (a) be listed as a source for a `content_hash` (via its own advertised/gossiped attachment inventory) and (b) respond to the resulting `GET /v2/attachments/{content_hash}` request, can supply attachment content that does not hash to the requested/committed value. This is accepted by `extend_with_attachments` and merged into the node's attachment set, which is subsequently used to resolve BNS name attachments. This causes the node to treat forged/unrelated bytes as the resolved attachment for that hash, and the legitimately committed attachment is never fetched for that request cycle, matching the "High: attachment/BNS mismatch" impact category. This is repeatable per content-hash the attacker is queried for.

### Likelihood Explanation
Preconditions are minimal: the attacker just needs the node to consider them a valid source for the hash in question (attainable by advertising it in inventory gossip that the node already trusts enough to add as a source) and then answer the RPC GET request truthfully in transport terms but with wrong content. No secret, no privileged role, and no local access is required — this is exploitable over the public RPC surface exposed to any P2P-connected/attachment-syncing peer.

### Recommendation
In `extend_with_attachments`, after decoding the response, verify `response.attachment.hash() == request.content_hash` (using `Attachment::hash` / `Hash160::from_data`) before inserting into `self.attachments`; on mismatch, treat it as a failed request (`report.bump_failed_requests()`) and optionally penalize/deregister the offending peer, mirroring the intended "served bytes must match committed hash" invariant.

### Proof of Concept
Rust test in `stackslib::net::atlas::download` (or `tests.rs` in that module):
1. Construct an `AttachmentsBatchStateContext` with a `peers` reliability map containing a fake peer URL.
2. Build a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map has one entry: `AttachmentRequest { content_hash: H, sources: {peer_url}, .. } -> Some(response)`, where `response` is a `StacksHttpResponse` (or a stub implementing `decode_atlas_get_attachment`) that decodes to `GetAttachmentResponse { attachment: Attachment { content: b"forged bytes" } }` whose `Attachment::hash()` equals `H' != H`.
3. Call `context.extend_with_attachments(&mut results)`.
4. Assert that `context.attachments` contains the attachment with hash `H'` (i.e., `context.attachments.iter().any(|a| a.hash() != H)` is true), proving that content not matching the originally requested `content_hash` was accepted with no rejection and no peer penalty distinguishing this case from a valid response.

### Citations

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

**File:** stackslib/src/net/atlas/download.rs (L547-552)
```rust
            if let Ok(response) = response.decode_atlas_get_attachment() {
                self.attachments.insert(response.attachment);
                report.bump_successful_requests();
            } else {
                report.bump_failed_requests();
            }
```

**File:** stackslib/src/net/api/getattachment.rs (L145-156)
```rust
impl StacksHttpRequest {
    /// Make a new request for an attachment
    pub fn new_getattachment(host: PeerHost, attachment_id: Hash160) -> StacksHttpRequest {
        StacksHttpRequest::new_for_peer(
            host,
            "GET".into(),
            format!("/v2/attachments/{}", &attachment_id),
            HttpRequestContents::new(),
        )
        .expect("FATAL: failed to construct request from infallible data")
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
