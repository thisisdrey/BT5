### Title
Missing `attachments_max_size` enforcement on attachment bytes fetched from remote peers - ([File: stackslib/src/net/atlas/mod.rs] / [File: stackslib/src/net/atlas/download.rs] / [File: stackslib/src/net/api/getattachment.rs])

### Summary
`AtlasConfig::attachments_max_size` is only used to validate the configured value at startup and is never checked against the actual byte length of attachment content received from a remote peer in response to an `AttachmentRequest`. A malicious peer can respond to a `GET /v2/attachments/{content_hash}` request with an arbitrarily large body, and that content will be hashed, matched, and persisted into the Atlas DB with no size cap.

### Finding Description
The relevant decode/store path is:
1. `StacksHttpResponse::decode_atlas_get_attachment` (`stackslib/src/net/api/getattachment.rs:159-165`) parses the HTTP JSON body via `parse_json`/`try_into()` into a `GetAttachmentResponse`.
2. `GetAttachmentResponse`'s `Deserialize` impl (`stackslib/src/net/atlas/mod.rs:69-77`) takes the hex string payload, decodes it with `hex_bytes`, and calls `Attachment::new(bytes)` — `Attachment::new` (`stackslib/src/net/atlas/mod.rs:154-156`) stores `content` with no length check at all.
3. `AttachmentsBatchStateContext::extend_with_attachments` (`stackslib/src/net/atlas/download.rs:530-558`) calls `response.decode_atlas_get_attachment()` and inserts the resulting `Attachment` into `self.attachments` — again, no size check.
4. `AttachmentsDownloader::run` (`stackslib/src/net/atlas/download.rs:152-169`) drains `context.attachments` and calls `network.atlasdb.insert_instantiated_attachment(&attachment)` directly — no comparison against `attachments_max_size` anywhere in this call chain.

Grep across the repo confirms `attachments_max_size` is referenced only in `AtlasConfig::new`/`AtlasConfig::validate` (which only enforces a *minimum* configured value, not a per-attachment enforcement) and in config loading/tests — it is never read on the attachment-download/ingestion path in `download.rs` or `mod.rs`'s `Attachment`/`GetAttachmentResponse` types. The hash check (`Hash160::from_data(&content)`, used implicitly via `AttachmentInstance.content_hash` matching, since the request itself is keyed by `content_hash`) will still correctly validate that the content matches the hash the attacker chose to serve, but nothing bounds how large that content can be before it's persisted.

### Impact Explanation
Any remote peer that a node syncs Atlas attachments from can, for any legitimate small-sized `AttachmentInstance` commitment it knows about (or even one it manufactures a `content_hash` for that no local peer holds yet, causing repeated fetch attempts), respond with a multi-hundred-megabyte (or larger) blob when asked for that attachment. This content is durably written into the Atlas SQLite database via `insert_instantiated_attachment`, with no relation to `attachments_max_size`. Repeated across many distinct attachment instances/content hashes, this allows unauthenticated remote peers to inflate a victim node's on-disk Atlas storage without bound, i.e., attachment storage exhaustion decoupled from the configured cap — matching the "attachment/BNS mismatch"-class High severity impact (data accepted/stored that violates the node's own committed size policy).

### Likelihood Explanation
No privileged access is required — any peer that the victim node treats as an outbound sync peer for Atlas (a normal, unauthenticated P2P/RPC relationship) can serve the oversized body when the victim's `AttachmentsDownloader` requests a missing attachment by content hash. No secrets, signatures, or admin roles are needed; this is a plain HTTP GET response from a peer's own `/v2/attachments/{hash}` endpoint, which is reachable by any peer serving that endpoint. It's fully repeatable across attachment batches/content hashes.

### Recommendation
Enforce `attachments_max_size` (from `network.atlasdb`'s `AtlasConfig`) at the point content bytes are accepted from a peer response — specifically before/at `Attachment::new` construction in `GetAttachmentResponse::deserialize` (or immediately after decode in `decode_atlas_get_attachment`/`extend_with_attachments`), rejecting (treating as a faulty-peer response) any body whose decoded length exceeds the configured max, before it ever reaches `insert_instantiated_attachment`.

### Proof of Concept
Rust net test plan:
1. Configure a test `AtlasConfig` with `attachments_max_size = ATTACHMENTS_MAX_SIZE_MIN` (1MB).
2. Register an `AttachmentInstance` with a known `content_hash` in a test `AtlasDB`/`AttachmentsDownloader` batch.
3. Stand up a mock peer HTTP responder for `/v2/attachments/{content_hash}` that returns a JSON body encoding a >10MB hex payload whose hash matches `content_hash`.
4. Drive `AttachmentsDownloader::run` through its state machine to completion for that batch.
5. Assert: `atlas_db.find_attachment(&content_hash)` returns content whose length exceeds `attachments_max_size`, demonstrating no truncation/rejection occurred (expected secure behavior would be rejection at `extend_with_attachments`/`decode_atlas_get_attachment` with the attachment marked as a faulty-peer response instead of being inserted). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** stackslib/src/net/atlas/mod.rs (L69-77)
```rust
impl<'de> Deserialize<'de> for GetAttachmentResponse {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<GetAttachmentResponse, D::Error> {
        let payload = String::deserialize(d)?;
        let hex_encoded = payload.parse::<String>().map_err(de_Error::custom)?;
        let bytes = hex_bytes(&hex_encoded).map_err(de_Error::custom)?;
        let attachment = Attachment::new(bytes);
        Ok(GetAttachmentResponse { attachment })
    }
}
```

**File:** stackslib/src/net/atlas/mod.rs (L116-144)
```rust
    pub fn validate(&self) -> Result<(), String> {
        if self.attachments_max_size < ATTACHMENTS_MAX_SIZE_MIN {
            Err(format!(
                "Invalid value for `attachments_max_size`: {}. Expected {} or greater",
                self.attachments_max_size, ATTACHMENTS_MAX_SIZE_MIN
            ))
        } else if self.max_uninstantiated_attachments < MAX_UNINSTANTIATED_ATTACHMENTS_MIN {
            Err(format!(
                "Invalid value for `max_uninstantiated_attachments`: {}. Expected {} or greater",
                self.max_uninstantiated_attachments, MAX_UNINSTANTIATED_ATTACHMENTS_MIN
            ))
        } else if self.uninstantiated_attachments_expire_after
            < UNINSTANTIATED_ATTACHMENTS_EXPIRE_AFTER_MIN
        {
            Err(format!(
                "Invalid value for `uninstantiated_attachments_expire_after`: {}. Expected {} or greater",
                self.uninstantiated_attachments_expire_after, UNINSTANTIATED_ATTACHMENTS_EXPIRE_AFTER_MIN
            ))
        } else if self.unresolved_attachment_instances_expire_after
            < UNRESOLVED_ATTACHMENT_INSTANCES_EXPIRE_AFTER_MIN
        {
            Err(format!(
                "Invalid value for `unresolved_attachment_instances_expire_after`: {}. Expected {} or greater",
                self.unresolved_attachment_instances_expire_after, UNRESOLVED_ATTACHMENT_INSTANCES_EXPIRE_AFTER_MIN
            ))
        } else {
            Ok(())
        }
    }
```

**File:** stackslib/src/net/atlas/mod.rs (L153-156)
```rust
impl Attachment {
    pub fn new(content: Vec<u8>) -> Attachment {
        Attachment { content }
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

**File:** stackslib/src/net/api/getattachment.rs (L158-166)
```rust
impl StacksHttpResponse {
    pub fn decode_atlas_get_attachment(self) -> Result<GetAttachmentResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: GetAttachmentResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
    }
}
```
