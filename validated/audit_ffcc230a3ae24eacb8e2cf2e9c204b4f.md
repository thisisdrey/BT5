### Title
Unverified attachment content hash allows malicious peer to poison AttachmentsDownloader storage - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` accepts any `Attachment` returned by a peer for an `AttachmentRequest` without checking that the returned attachment's content hashes to the originally requested `content_hash`. `AttachmentsDownloader::run` then unconditionally stores it via `insert_instantiated_attachment` and resolves the batch using the attacker-controlled `attachment.hash()` rather than the pending request's `content_hash`.

### Finding Description
The request is built with an explicit `content_hash` in `get_prioritized_attachments_requests` [1](#0-0) . When the HTTP response comes back, `extend_with_attachments` only checks that JSON decoding succeeded (`decode_atlas_get_attachment`) and inserts `response.attachment` into `self.attachments` — it never compares `response.attachment.hash()` against `request.content_hash` [2](#0-1) . `StacksHttpResponse::decode_atlas_get_attachment` itself performs no hash validation either, it merely parses JSON into a `GetAttachmentResponse` [3](#0-2) .

Then in `AttachmentsDownloader::run`, for every attachment drained from `context.attachments`, the code calls `network.atlasdb.insert_instantiated_attachment(&attachment)` unconditionally, and resolves the pending batch state via `context.attachments_batch.resolve_attachment(&attachment.hash())` — i.e., keyed by the attacker-returned attachment's own recomputed hash, not cross-checked against the originally requested `content_hash` [4](#0-3) . Since `HashSet<Attachment>` in `AttachmentsBatchStateContext` (`self.attachments.insert(response.attachment)`) is not keyed by request identity, any peer selected as a `source` for a pending `AttachmentRequest` can respond with unrelated content, and this content will be inserted into the atlas DB as an "instantiated" attachment and used to try to resolve whatever attachment instance is pending in that batch.

### Impact Explanation
A malicious peer chosen from `sources` for an `AttachmentRequest` can return arbitrary bytes as the `Attachment.content`. The downloader stores this content in `AtlasDB` (`insert_instantiated_attachment`) without validating it matches the requested `content_hash`, and can attempt to resolve pending attachment instances using the attacker's self-declared hash. This is a High-severity attachment/BNS integrity issue: an unrelated/attacker-chosen blob can be persisted and later served through the `/v2/attachments/:hash` RPC endpoint (`getattachment.rs`, which just does `find_attachment(&attachment_hash)`) as if it were legitimate content for that hash, and it also allows unlimited unrelated content to be inboxed into local storage, repeatable per request/peer selection.

### Likelihood Explanation
The attacker only needs to be a normal, unprivileged peer that the node's Atlas downloader selects as an inventory/attachment source (i.e., it merely needs to advertise the hash in its `GetAttachmentsInvResponse` inventory and be chosen by `get_prioritized_attachments_requests`). No secret, admin role, or privileged position is required — any node that can serve HTTP responses on its data URL and gets picked as a peer can trigger this on every `AttachmentRequest` it services.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (stackslib/src/net/atlas/download.rs), after decoding the response, compute `response.attachment.hash()` and compare it against `request.content_hash`; if they don't match, treat the response as failed (`report.bump_failed_requests()`) and do not insert into `self.attachments`. Additionally, in `AttachmentsDownloader::run`, avoid resolving/storing based solely on the returned attachment's self-reported hash — cross-check against the pending instance's `content_hash` before calling `insert_instantiated_attachment`.

### Proof of Concept
Add a test in `stackslib/src/net/atlas/tests.rs` (or a new test module) that:
1. Constructs an `AttachmentsBatchStateContext` with a pending `AttachmentRequest{content_hash: H, sources: {peer_url}, ...}`.
2. Mocks a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map contains that request mapped to `Some(StacksHttpResponse)` encoding `GetAttachmentResponse{attachment: Attachment{content: b"unrelated".to_vec()}}`, where `Attachment{content: b"unrelated"}.hash() != H`.
3. Calls `context.extend_with_attachments(&mut results)` and asserts that `context.attachments` contains an attachment whose `.hash() != H` (demonstrating no cross-check occurred).
4. Drives it through `AttachmentsDownloader::run`/the `Done` branch and asserts `network.atlasdb.find_attachment(&H)` returns `Ok(None)` while `network.atlasdb.find_attachment(&unrelated_hash)` returns `Ok(Some(_))` — showing the mismatch is stored under the attacker's hash rather than being rejected as not matching the originally committed `content_hash`.

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
