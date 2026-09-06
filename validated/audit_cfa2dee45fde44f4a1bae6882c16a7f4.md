### Title
Malicious peer can serve attachment content with mismatched hash, permanently leaving the real on-chain attachment commitment unresolved - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` accepts a `GetAttachmentResponse` from a `GET /v2/attachments/{content_hash}` request and stores `response.attachment` into `self.attachments: HashSet<Attachment>` without ever checking that `response.attachment.hash() == request.content_hash`. In the `AttachmentsDownloader::run` `Done` branch, resolution/lookup then keys off the attacker-controlled `attachment.hash()` instead of the originally requested `content_hash`, so a peer can serve arbitrary bytes whose hash differs from what was requested and the real content hash is never resolved.

### Finding Description
The equality the system relies on — `served_attachment.hash() == AttachmentRequest.content_hash` — is never checked.

- `AttachmentRequest` is built from `content_hash` derived from an on-chain commitment (`stackslib/src/net/atlas/download.rs:404-478`, field at line 1067).
- The request is sent via `AttachmentRequest::make_request_type`, which issues `GET /v2/attachments/{content_hash}` (`stackslib/src/net/atlas/download.rs:1110-1118`).
- The response is decoded purely by JSON parsing with `StacksHttpResponse::decode_atlas_get_attachment`, which does no hash validation whatsoever: [1](#0-0) 
- `AttachmentsBatchStateContext::extend_with_attachments` then inserts the decoded `response.attachment` straight into `self.attachments: HashSet<Attachment>`, again with no hash check against `request.content_hash`: [2](#0-1) 
- In `AttachmentsDownloader::run`'s `Done` branch, resolution is keyed on `attachment.hash()` (a hash the attacker fully controls by choosing what bytes to serve), not on the original `content_hash` that was requested and tracked in `AttachmentsBatch.attachments_instances`: [3](#0-2) 
- `AttachmentsBatch::resolve_attachment` only removes entries whose tracked `content_hash` equals the passed hash: [4](#0-3) 

Because the served attachment's hash (call it `X`) differs from the genuinely requested/committed `content_hash` (call it `Y`), `find_all_attachment_instances(&X)` finds zero matching instances, `insert_instantiated_attachment(&X-content)` stores unrelated/garbage content in the atlasdb, and `resolve_attachment(&X)` does not remove the `Y` entry from `attachments_instances`. The batch is therefore re-queued (`bump_retry_count`) and will re-request the same `content_hash` `Y` from peers up to `max_attachment_retry_count` times; if the malicious peer (or any peer in `sources`) keeps responding the same way, the entry is retried and eventually dropped once retries are exhausted, permanently leaving that on-chain attachment commitment unresolved in `AttachmentsBatch.attachments_instances`.

### Impact Explanation
This is a High-severity "attachment/BNS mismatch" style issue: a remote, unprivileged outbound-sync peer that the node fetches attachments from can cause the victim node to never resolve a specific BNS name's zonefile/attachment, even though it is being legitimately served by other honest peers, by winning the race to answer the `AttachmentRequest` with mismatched content. It also pollutes the local `atlasdb` `attachments` table with content keyed under the wrong hash (`X`) that nothing else will ever ask for, since it isn't tied to any instance. No crash or state-consensus violation occurs, but it durably degrades BNS resolution for the targeted node without any indication that data was rejected — the request was answered with HTTP 200, yet nothing validates it before storage.

### Likelihood Explanation
The attacker only needs to be an outbound-sync peer (or be reachable/selected as a data source in `network.get_outbound_sync_peers()`/`get_data_url`) for the node performing Atlas sync, and to be listed as a source for the specific `content_hash` (i.e., their inventory bit for the relevant page/index is `1`, which they can freely set/lie about since inventory bits aren't verified against actual possession before serving). This requires no secret, no privileged role, and just a normal RPC server responding to `/v2/attachments/{hash}` with attacker-chosen JSON content. It is fully repeatable across every retry of the batch.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (stackslib/src/net/atlas/download.rs), after `decode_atlas_get_attachment`, compute `response.attachment.hash()` and compare it against `request.content_hash`; if they don't match, treat it as a failed request (`report.bump_failed_requests()`) and do not insert into `self.attachments`. This restores the equality the design already assumes and prevents unrelated/garbage content from being accepted or persisted.

### Proof of Concept
Add a unit test in `stackslib/src/net/atlas/tests.rs`:
1. Build an `AttachmentsBatch` tracking one `AttachmentInstance` with `content_hash = Y` (e.g. via `new_attachment_instance_from`).
2. Build the corresponding `AttachmentsBatchStateContext`, call `get_prioritized_attachments_requests()` to get the `AttachmentRequest` for `Y`.
3. Construct a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map pairs that request with a `StacksHttpResponse`/decoded `GetAttachmentResponse` wrapping an `Attachment` whose content hashes to `X != Y` (e.g. `Attachment::new(b"malicious-bytes".to_vec())`).
4. Call `context.extend_with_attachments(&mut result)` and assert `context.attachments` contains the attachment with hash `X` (i.e., it was accepted despite mismatch) — demonstrating the missing check.
5. Simulate the `Done` branch logic (or call into `AttachmentsDownloader::run` with a mocked network/atlasdb) and assert `context.attachments_batch.attachments_instances` still contains the entry for `Y` after processing (i.e., `AttachmentsBatch::resolve_attachment` was never effectively called for `Y`), proving the instance remains permanently unresolved.

### Citations

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

**File:** stackslib/src/net/atlas/download.rs (L1227-1239)
```rust
    pub fn resolve_attachment(&mut self, content_hash: &Hash160) {
        for missing_attachments in self.attachments_instances.values_mut() {
            let mut keys = vec![];
            for (k, hash) in missing_attachments.iter() {
                if hash == content_hash {
                    keys.push(*k);
                }
            }
            for key in keys {
                missing_attachments.remove(&key);
            }
        }
    }
```
