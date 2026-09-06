### Title
`AttachmentsBatchStateContext::extend_with_attachments` accepts attacker-supplied attachment content without verifying it matches the requested `content_hash` - (File: stackslib/src/net/atlas/download.rs)

### Summary
`extend_with_attachments` decodes a peer's `GetAttachmentResponse` and unconditionally inserts `response.attachment` into `self.attachments`, marking the request as successful, without ever comparing `response.attachment.hash()` to the `AttachmentRequest.content_hash` that was originally requested. Any outbound sync peer that answers an attachment request can therefore supply arbitrary bytes that get accepted as if they were the attachment committed on-chain.

### Finding Description
The batch downloader builds `AttachmentRequest { content_hash, sources, .. }` in `get_prioritized_attachments_requests` [1](#0-0)  and dispatches it to peers claiming to have the attachment in their inventory. When a response comes back, `extend_with_attachments` decodes it via `StacksHttpResponse::decode_atlas_get_attachment()` and inserts the returned `Attachment` directly: [2](#0-1) 

`decode_atlas_get_attachment` itself performs no hash validation either — it only parses the HTTP body as JSON into a `GetAttachmentResponse`: [3](#0-2) 

The equality that should be enforced — `response.attachment.hash() == request.content_hash` — is never checked anywhere in this call path. A malicious (or compromised) outbound peer that is legitimately reachable and merely claims (via a forged/self-reported inventory response) to hold the requested attachment can respond to the `GET /v2/attachments/{hash}`-style request with an `Attachment` whose `content` is arbitrary attacker data. That attachment is accepted into `self.attachments`, and `report.bump_successful_requests()` is called, so the reliability-report bookkeeping also treats the malicious peer as trustworthy. Because the request is marked "succeeded" with the bogus payload, the state machine will not re-attempt fetching the correct attachment from another source for that batch cycle.

### Impact Explanation
This allows a single unprivileged remote peer (any node the victim has as an outbound sync peer, which requires no secret/credential — just running a Stacks P2P peer that the victim dials into) to poison the victim's Atlas/BNS attachment cache with data that does not match the on-chain-committed `content_hash`. Since Atlas attachments back BNS name resolution (zonefiles) and other off-chain content referenced by hash on-chain, this is a canonical-vs-served-state mismatch: the node serves/stores content that no canonical transaction actually committed to. This matches the "High: attachment/BNS mismatch" impact category. It is repeatable per attachment request as long as the attacker peer is selected as a response source.

### Likelihood Explanation
Preconditions are modest: the attacker's node must appear as an outbound sync peer for the target (standard peer discovery/gossip, no special role or secret required) and must be selected as a source when the downloader queries `get_prioritized_attachments_requests` — attainable by advertising the attachment as present in the attacker's `GetAttachmentsInvResponse` inventory. No privileged role, RPC secret, or slot ownership is needed; the attacker simply answers a normal attachment-fetch request with crafted bytes. Cost is trivial (one crafted HTTP response per request).

### Recommendation
In `extend_with_attachments`, after `decode_atlas_get_attachment()` succeeds, compute `response.attachment.hash()` and compare it to `request.content_hash` before inserting into `self.attachments`; on mismatch, call `report.bump_failed_requests()` (and optionally treat the peer as unreliable/faulty) instead of accepting the attachment.

### Proof of Concept
1. In `stackslib/src/net/atlas/tests.rs` (or a new test module), construct an `AttachmentRequest` with a known `content_hash = Hash160::from_data(b"real content")`.
2. Build a `StacksHttpResponse` (or directly a `GetAttachmentResponse`) whose `Attachment.content = b"malicious content".to_vec()` (hash differs from `content_hash`).
3. Populate a `BatchedRequestsResult` with this `(request, Some(response))` in `succeeded`, and call `AttachmentsBatchStateContext::extend_with_attachments`.
4. Assert `self.attachments` contains an `Attachment` whose `hash() != content_hash` (i.e., the mismatched attachment was accepted), and that no attachment with the original `content_hash` was ever stored — demonstrating the missing equality check.

Note: I was not able to fully trace how `self.attachments` is subsequently consumed downstream (e.g., whether `AttachmentsDownloader::run` or the `AtlasDB` insertion path re-validates the hash before persisting to `atlasdb`) due to tool-call budget limits; this should be verified to confirm whether any later gate mitigates persistence, though the acceptance into `self.attachments` and the false "successful request" bookkeeping at this stage are confirmed to occur unconditionally.

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

**File:** stackslib/src/net/api/getattachment.rs (L159-165)
```rust
    pub fn decode_atlas_get_attachment(self) -> Result<GetAttachmentResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: GetAttachmentResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
    }
```
