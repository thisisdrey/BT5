### Title
Unbounded-size attachment content accepted and stored into AtlasDB with no Atlas-specific size cap - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` accepts a `GetAttachmentResponse` from any peer it queried and inserts the returned `Attachment` into `self.attachments` without checking its size or verifying it corresponds to the requested `content_hash`. `AttachmentsDownloader::run` then unconditionally calls `network.atlasdb.insert_instantiated_attachment(&attachment)` for every attachment received, even when `find_all_attachment_instances(&attachment.hash())` returns no matching instance, meaning arbitrary attacker-chosen content can be committed to local storage.

### Finding Description
The claimed equality is: *bytes received == bytes authorized by an on-chain commitment*. The commitment (`AttachmentInstance.content_hash`) only commits to a `Hash160` of the content, never to a size. In `extend_with_attachments` (`stackslib/src/net/atlas/download.rs:530-558`), the response is decoded via `decode_atlas_get_attachment` (`stackslib/src/net/api/getattachment.rs:159-165`), which merely does `serde_json::from_value` into `GetAttachmentResponse { attachment: Attachment }` with no bound on `attachment.content.len()`. The decoded attachment is inserted into a `HashSet<Attachment>` with no comparison against `request.content_hash` at this call site.

In `AttachmentsDownloader::run` (`stackslib/src/net/atlas/download.rs:152-169`), every attachment drained from `context.attachments` is processed by computing `attachment.hash()` locally and looking up `find_all_attachment_instances(&attachment.hash())`. Critically, `insert_instantiated_attachment(&attachment)` is called *unconditionally*, regardless of whether any matching instance was found: [1](#0-0) 

This means a queried peer can return any content blob of any size (bounded only by the generic HTTP response body/payload limit enforced by the HTTP layer, not by any Atlas-specific per-attachment cap) and have it written into the local `AtlasDB` "instantiated attachments" table, since the size is never checked before storage.

### Impact Explanation
A single malicious response to an outbound `AttachmentRequest` causes the requesting node to write an oversized, unauthenticated blob into its own `AtlasDB`. Because the write happens per successfully-parsed response and is bounded only by the generic HTTP body cap (not a BNS/Atlas-specific size class), a peer could repeatedly inflate the victim's Atlas storage disproportionately to legitimate zonefile/profile attachment sizes. This is a local, unauthenticated write to persistent state triggered by remote, untrusted input.

### Likelihood Explanation
Preconditions: the attacking node must be selected as one of the victim's `outbound_sync_peers` and must be queried for at least one `AttachmentRequest` (i.e., the victim must already have an outstanding `AttachmentInstance` it's trying to resolve, which happens routinely during normal BNS/Atlas sync). No secret, admin role, or privileged key is required — any peer that participates in the P2P network as an outbound sync source can trigger this on any request it receives. This makes it a low-cost, repeatable action for any unprivileged peer capable of connecting to the node's P2P/RPC and being selected as a data source.

### Recommendation
In `extend_with_attachments`, verify `Attachment::hash() == request.content_hash` before inserting into `self.attachments`, and reject/log-and-drop mismatches. Additionally, enforce an explicit Atlas-specific maximum content length (matching the legitimate BNS zonefile/attachment size class) both in `decode_atlas_get_attachment`/`GetAttachmentResponse` deserialization and before calling `insert_instantiated_attachment`, so that unmatched or oversized attachments are never persisted regardless of the generic HTTP body limit.

### Proof of Concept
Rust net test plan:
1. Construct an `AttachmentsBatchStateContext` with a pending `AttachmentRequest` for a known `content_hash` H.
2. Simulate a `StacksHttpResponse` whose JSON body is `{"attachment":{"content":"<hex of several MB of arbitrary bytes>"}}` (content that does NOT hash to H).
3. Feed this response through `BatchedRequestsResult::succeeded` into `context.extend_with_attachments(&mut results)`.
4. Assert the oversized/mismatched `Attachment` lands in `context.attachments`.
5. Call the equivalent of `AttachmentsDownloader::run`'s `Done` branch logic and assert `atlasdb.insert_instantiated_attachment(&attachment)` succeeds despite `find_all_attachment_instances(&attachment.hash())` returning empty — i.e., no ceiling check or hash-match check rejects the oversized/unrequested blob before it is persisted. [2](#0-1) [3](#0-2) 

**Note on uncertainty:** I was unable to inspect the body of `AtlasDB::insert_instantiated_attachment` in `stackslib/src/net/atlas/db.rs` (only its signature/call sites were located within the available tool budget) to confirm there is no size check enforced at the SQL/storage layer itself. If such a check exists there, it would mitigate this finding; based on the traced call path in `download.rs`, no such check is applied before the call.

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
