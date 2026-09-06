### Title
`AttachmentsDownloader::run`'s Done branch stores attacker-served `Attachment` content with no size check and no verification that its hash matches any requested `AttachmentInstance` - ([File: stackslib/src/net/atlas/download.rs])

### Summary
The Done branch of `AttachmentsBatchStateMachine`/`AttachmentsDownloader::run` unconditionally calls `network.atlasdb.insert_instantiated_attachment(&attachment)` for every `Attachment` a remote peer returned in a `GetAttachmentResponse`, regardless of whether any known `AttachmentInstance` actually references that attachment's hash, and regardless of the attachment's content length. This bypasses the `attachments_max_size` / BNS-zonefile size gate (`AtlasDB::should_keep_attachment`) that is enforced on other ingestion paths (e.g. the RPC POST-attachment path), allowing a malicious outbound-sync peer to make the node persist oversized, unrelated blobs into its Atlas attachments table.

### Finding Description
`AttachmentsBatchStateContext::extend_with_attachments` (`stackslib/src/net/atlas/download.rs:530-559`) takes every successfully-decoded `GetAttachmentResponse` from a peer and inserts `response.attachment` into `self.attachments`, a `HashSet<Attachment>`, without ever checking that the returned attachment's `hash()` matches the `content_hash` of the `AttachmentInstance`/page that was actually requested: [1](#0-0) 

Then, in `AttachmentsDownloader::run`, the `Done` branch drains this set and, for every attachment, unconditionally calls `insert_instantiated_attachment` **before** checking whether any matching instance was actually found: [2](#0-1) 

`insert_instantiated_attachment` itself performs no size validation at all — it just inserts `attachment.content` verbatim: [3](#0-2) 

This is in contrast to `AtlasDB::should_keep_attachment`, which explicitly enforces `attachment.content.len() as u32 > self.atlas_config.attachments_max_size` before accepting an attachment on other paths (e.g. attachments posted/gossiped via RPC): [4](#0-3) 

`GetAttachmentResponse`'s deserializer also performs no length check — it simply hex-decodes whatever string the peer sent into `Attachment::new(bytes)`: [5](#0-4) 

Attack flow: the attacker is (or controls) an outbound-sync peer that the victim node queries for attachments (`network.get_outbound_sync_peers()` / `get_data_url`). When the node issues a legitimate `AttachmentRequest`, the attacker replies over HTTP with a `GetAttachmentResponse` whose `attachment.content` is a large, arbitrary blob unrelated to any real BNS zonefile the node is trying to fetch. The response is bounded only by the generic HTTP/message-size limits (`MAX_MESSAGE_LEN`) used by `httpcore.rs`, not by the Atlas-specific `attachments_max_size` (default 1 MiB, `ATTACHMENTS_MAX_SIZE_MIN`) that the protocol intends as the ceiling for legitimate zonefile-sized content. The oversized/unrelated content is inserted into the `attachments` table with `was_instantiated = 1`, permanently persisted, with the only "check" being whether some `AttachmentInstance` happens to reference the same content hash — which is irrelevant to whether the *insert* happens, since the insert call is unconditional and happens before that check's result is used.

### Impact Explanation
A remote peer the node syncs Atlas attachments from can cause the node to persist arbitrary, oversized blobs into its local `AtlasDB` `attachments` table with no enforcement of `attachments_max_size`, bypassing the size gate that exists for every other ingestion path. This is repeatable per attachment batch/poll cycle and per distinct attacker-chosen content (since content is keyed by its own hash, an attacker can flood many distinct oversized blobs across repeated polling), leading to unbounded disk growth on the victim's Atlas database — a storage-exhaustion condition tied to bypassing the consensus-adjacent BNS zonefile size commitment, matching the "High" category (attachment/BNS mismatch, bounded compute/storage impact via a read/sync endpoint).

### Likelihood Explanation
Preconditions: the victim node must have Atlas/BNS attachment syncing enabled (default for a full node) and must select the attacker as one of its outbound sync peers with a configured data URL — an attacker can achieve this simply by running their own reachable Stacks node (explicitly allowed "run their own peer" in the threat model). No secret, signature, or privileged role is required; the attacker only needs to respond to a legitimate `AttachmentRequest` HTTP GET with a crafted JSON body. Attacker cost is minimal (one HTTP response per request), and the behavior is fully repeatable across polling cycles/batches.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (or in `AttachmentsDownloader::run`'s `Done` branch), before calling `insert_instantiated_attachment`:
1. Verify that `find_all_attachment_instances(&attachment.hash())` returns at least one match (i.e., only store attachments that were actually requested/expected) and skip/penalize the peer otherwise.
2. Enforce `attachment.content.len() as u32 <= atlas_config.attachments_max_size` (reuse `AtlasDB::should_keep_attachment` or an equivalent check) before insertion, rejecting and treating the peer as faulty if violated.

### Proof of Concept
Rust test plan (in `stackslib/src/net/atlas/download.rs` or a new test module):
1. Construct an `AttachmentsBatchStateContext` with a pending `AttachmentsBatch` requesting a specific `content_hash` `H` for some contract/index.
2. Simulate a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map contains a `StacksHttpResponse` encoding a `GetAttachmentResponse { attachment: Attachment { content: vec![0u8; 8 * 1024 * 1024] } }` (8 MiB, far exceeding `ATTACHMENTS_MAX_SIZE_MIN = 1_048_576`), whose hash is *not* `H`.
3. Call `extend_with_attachments` and then drive `AttachmentsBatchStateMachine` to `Done`; call `AttachmentsDownloader::run`.
4. Assert that `network.atlasdb.find_attachment(&fake_hash)` returns `Some(attachment)` with `content.len() == 8*1024*1024` even though no `AttachmentInstance` referenced that hash and it exceeds `atlas_config.attachments_max_size` — demonstrating `insert_instantiated_attachment` was called with no size or relevance check, unlike `AtlasDB::should_keep_attachment`'s gate used elsewhere.

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

**File:** stackslib/src/net/atlas/download.rs (L547-552)
```rust
            if let Ok(response) = response.decode_atlas_get_attachment() {
                self.attachments.insert(response.attachment);
                report.bump_successful_requests();
            } else {
                report.bump_failed_requests();
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
