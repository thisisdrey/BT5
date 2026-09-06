Based on the code traced, this is a valid finding.

### Title
`AttachmentsDownloader::run` stores peer-served GET attachments without enforcing `attachments_max_size` - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AtlasDB::should_keep_attachment` enforces the `attachments_max_size` bound only via manual call sites, but the peer-to-peer attachment download path (`AttachmentsBatchStateContext::extend_with_attachments` -> `AttachmentsDownloader::run` -> `AtlasDB::insert_instantiated_attachment`) never invokes it. A malicious peer answering a legitimate `AttachmentRequest` can therefore return content of arbitrary size, and it will be persisted to the node's Atlas database unconditionally.

### Finding Description
`AtlasDB::should_keep_attachment` checks both that the contract is supported and that `attachment.content.len() as u32 > self.atlas_config.attachments_max_size` [1](#0-0) . However, in the peer-download path, responses are decoded and inserted directly into the in-memory `attachments: HashSet<Attachment>` with no size or content-hash check at all: [2](#0-1) 

Then in `AttachmentsDownloader::run`, every attachment collected in `context.attachments` is written straight to the database via `insert_instantiated_attachment`, which performs an unconditional `INSERT OR REPLACE` with no size gate: [3](#0-2) [4](#0-3) 

Notably, `extend_with_attachments` also does not verify that the decoded `response.attachment`'s content hash matches the `AttachmentRequest.content_hash` that was requested, so a responding peer fully controls both the content and its size for whatever hash was asked. The only gate that exists (`should_keep_attachment`) is used exclusively on the RPC-POST attachment ingestion path (e.g., for BNS name registration), not here, so it never runs for this call sequence.

### Impact Explanation
An attacker running a peer node that answers `AttachmentRequest`s (`GET /v2/attachments/:hash`) can respond with content far larger than the configured `attachments_max_size` (which defaults to a small bound, e.g. the `ATTACHMENTS_MAX_SIZE_MIN`/configured value). This content is persisted into the victim's `atlasdb` SQLite `attachments` table with no size cap enforced on this path, unlike the POST path. Repeated over multiple attachment instances/batches this leads to unbounded disk growth in the Atlas database — a storage-exhaustion condition on a legitimate peer that merely answers real attachment requests, matching the "bounded compute/storage DoS" high-severity category for a read/sync endpoint.

### Likelihood Explanation
Preconditions are minimal: the attacker only needs to run an ordinary Stacks peer, appear in the victim's outbound peer set (`network.get_outbound_sync_peers()`), and be selected to serve an `AttachmentRequest` for some content hash that the victim is trying to resolve (e.g., because it appeared in that peer's advertised attachment inventory). No secrets, admin roles, or privileged state are required — this is a standard part of the Atlas attachment-sync protocol that a remote peer participates in by design. This is repeatable per attachment resolved and is bounded only by how many distinct attachment instances the victim tries to resolve from the attacker peer.

### Recommendation
Enforce `AtlasDB::should_keep_attachment` (or at minimum the size and content-hash checks) before accepting a peer-served attachment in `AttachmentsBatchStateContext::extend_with_attachments`, and again before `insert_instantiated_attachment` is called in `AttachmentsDownloader::run`. Additionally, verify that `Hash160::from_data(&response.attachment.content) == request.content_hash` before inserting the attachment, so a peer cannot substitute unrelated/oversized content for a requested hash.

### Proof of Concept
1. Construct an `AtlasConfig` with `attachments_max_size: 16` and a `bns` contract, `AtlasDB::connect_memory(atlas_config)`.
2. Build an `AttachmentsBatchStateContext` with a pending `AttachmentRequest` for some `content_hash`.
3. Simulate a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map contains a `GetAttachmentResponse` wrapping a `Attachment::new(vec![0u8; 1_000_000])` (1 MB), returned via `decode_atlas_get_attachment`.
4. Call `context.extend_with_attachments(&mut results)` and observe `context.attachments` now contains the 1 MB attachment (no size check).
5. Drive this through `AttachmentsDownloader::run` (or directly call `atlasdb.insert_instantiated_attachment(&attachment)`), then query `atlasdb.find_attachment(&attachment.hash())` and assert `content.len() == 1_000_000`, despite `attachments_max_size == 16` — i.e., `should_keep_attachment` would have returned `false` for this same attachment (`assert!(!atlasdb.should_keep_attachment(&bns_contract_id, &attachment))`), proving the bypass on the download path.

### Citations

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

**File:** stackslib/src/net/atlas/download.rs (L540-552)
```rust
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
```
