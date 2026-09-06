Confirmed. `should_keep_attachment` is not called anywhere in the `AttachmentsDownloader::run` path; the `Done` state directly calls `network.atlasdb.insert_instantiated_attachment(&attachment)` after decoding a peer's GET response via `decode_atlas_get_attachment`, with no size or contract check applied. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

The HTTP response body decoding relies on JSON deserialization from `get_http_payload_ok()`, whose size is bounded only by the general HTTP content-length/body caps in `httpcore.rs`/`server.rs` (`MAX_MESSAGE_LEN`-scale limits), not by `atlas_config.attachments_max_size`. That general cap is typically far larger than a configured `attachments_max_size` (e.g., 16 bytes in the PoC config), so an attacker peer serving a legitimately-requested attachment can inflate the stored content up to the generic HTTP body limit while completely bypassing the per-attachment size/contract gate that the POST path enforces via `should_keep_attachment` (see its use in `posttransaction.rs`/attachment POST handler). I could not find any additional size check specific to `decode_atlas_get_attachment` or to the `Done` branch of `AttachmentsBatchStateMachine` in `download.rs`.

### Title
Attachment size/contract gate (`should_keep_attachment`) is bypassed on the peer-download path - (File: stackslib/src/net/atlas/db.rs)

### Summary
`AtlasDB::should_keep_attachment` enforces both a contract allow-list and `attachments_max_size` cap, but it is only invoked on the RPC POST attachment-submission path. The `AttachmentsDownloader::run` peer-GET-response path calls `AtlasDB::insert_instantiated_attachment` directly, so a malicious/compromised peer answering a legitimate `AttachmentRequest` can have arbitrarily large (bounded only by generic HTTP limits) or off-list-contract content stored on disk.

### Finding Description
The intended invariant is: no `Attachment` whose `content.len() > atlas_config.attachments_max_size` (or whose owning contract isn't in `atlas_config.contracts`) should ever be persisted via `insert_instantiated_attachment`. `should_keep_attachment` implements this check [4](#0-3)  but is wired up only for the RPC-submitted-attachment code path. In the P2P attachment-sync path, `AttachmentsBatchStateContext::extend_with_attachments` decodes a peer's HTTP GET response via `decode_atlas_get_attachment` and inserts the resulting `Attachment` into `self.attachments` with no size/contract validation [2](#0-1) . `AttachmentsDownloader::run`'s `Done` branch then iterates `context.attachments` and calls `atlasdb.insert_instantiated_attachment(&attachment)` unconditionally [1](#0-0) , and `insert_instantiated_attachment` performs a raw SQL insert with no length/contract gate [5](#0-4) . The only remaining limits on this path are generic HTTP body-size caps in `httpcore.rs`/`server.rs`, which are unrelated to and typically much larger than the operator-configured `attachments_max_size`.

### Impact Explanation
A remote peer that answers a node's own `AttachmentRequest` (triggered when the victim node's own on-chain state references an attachment hash it doesn't yet have) can cause the victim to store attachment content vastly exceeding its configured `attachments_max_size`, and/or content for contracts outside the node's `atlas_config.contracts` allow-list. This is a disk/storage-exhaustion and configuration-bypass issue in the Atlas subsystem, repeatable per distinct attachment hash the victim requests, from any peer the victim happens to sync attachments with.

### Likelihood Explanation
Preconditions: the victim node must have an outstanding `AttachmentInstance` (from processed chain state) whose content hash it doesn't have locally, and must select the attacking peer as a data source (attacker just needs to be a legitimate synced peer and answer the `AttachmentRequest` truthfully with oversized content, since the content hash isn't verified in the shown decode/insert path against `attachment_instance.content_hash`, though hash validation may occur elsewhere — this was not fully traced). Attacker cost is low: a single crafted oversized HTTP response body. Remotely reachable via the standard Atlas HTTP data path, no privileged role or secret required.

### Recommendation
Call `AtlasDB::should_keep_attachment` (or an equivalent size/contract check) in `AttachmentsDownloader::run`'s `Done` branch, before invoking `insert_instantiated_attachment`, for every attachment obtained from peer responses in `extend_with_attachments`/`decode_atlas_get_attachment`. Reject and drop attachments exceeding `attachments_max_size` or belonging to non-allow-listed contracts, mirroring the RPC POST path's enforcement.

### Proof of Concept
1. Construct an `AtlasConfig` with `attachments_max_size = 16` and a `contracts` allow-list containing one contract.
2. Build an `AttachmentsBatchStateContext` (or drive `AttachmentsDownloader::run` end-to-end using a mocked/test peer) whose `AttachmentRequest` response is a `StacksHttpResponse` with a JSON body decodable via `decode_atlas_get_attachment` containing an `Attachment` with a 1MB `content` field.
3. Run `AttachmentsBatchStateMachine` through to `Done`, then call `AttachmentsDownloader::run`, letting it call `network.atlasdb.insert_instantiated_attachment(&attachment)`.
4. Assert via `AtlasDB::find_attachment` that the 1MB attachment is present in the DB despite `attachments_max_size = 16`, and (for comparison) assert that feeding the same oversized attachment through the RPC POST path (`should_keep_attachment` check) is rejected — demonstrating the path asymmetry described above.

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

**File:** stackslib/src/net/atlas/db.rs (L249-266)
```rust
    pub fn should_keep_attachment(
        &self,
        contract_id: &QualifiedContractIdentifier,
        attachment: &Attachment,
    ) -> bool {
        if !self.atlas_config.contracts.contains(contract_id) {
            info!(
                "Atlas: will discard posted attachment - {} not in supported contracts",
                contract_id
            );
            return false;
        }
        if attachment.content.len() as u32 > self.atlas_config.attachments_max_size {
            info!("Atlas: will discard posted attachment - attachment too large");
            return false;
        }
        true
    }
```

**File:** stackslib/src/net/atlas/db.rs (L576-592)
```rust
    pub fn insert_instantiated_attachment(
        &mut self,
        attachment: &Attachment,
    ) -> Result<(), db_error> {
        let now = util::get_epoch_time_secs() as i64;
        let tx = self.tx_begin()?;
        tx.execute(
            "INSERT OR REPLACE INTO attachments (hash, content, was_instantiated, created_at) VALUES (?, ?, 1, ?)",
            params![attachment.hash(), attachment.content, now],
        )?;
        tx.execute(
            "UPDATE attachment_instances SET is_available = 1 WHERE content_hash = ?1 AND status = ?2",
            params![attachment.hash(), AttachmentInstanceStatus::Checked],
        )?;
        tx.commit()?;
        Ok(())
    }
```
