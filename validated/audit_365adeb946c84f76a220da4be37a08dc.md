### Title
Unvalidated `GetAttachment` response content lets a remote peer permanently poison the `attachments` table with junk data unrelated to any on-chain commitment - (File: `stackslib/src/net/atlas/download.rs`, `stackslib/src/net/atlas/db.rs`)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` accepts any HTTP `GetAttachmentResponse` from a peer and inserts `response.attachment` into `self.attachments` without checking that `response.attachment.hash()` equals the `content_hash` of the `AttachmentRequest` that was sent. The downloader then unconditionally calls `AtlasDB::insert_instantiated_attachment` on every such attachment, which `INSERT OR REPLACE`s it into the `attachments` table (`was_instantiated = 1`) keyed by the attacker-chosen bytes' own hash, with no check that this hash actually corresponds to any `AttachmentInstance.content_hash` ever committed on-chain.

### Finding Description
The claimed broken equality holds: nothing enforces `attachment.hash() == request.content_hash` between the point a `GetAttachment` request is issued and the point the response is stored.

- `extend_with_attachments` decodes the peer's response and inserts it blind: [1](#0-0) 
- The batch driver then, for every attachment collected, looks up matching instances by the *attacker-supplied* content's own hash and unconditionally persists it regardless of whether any instance matched: [2](#0-1) 
- `insert_instantiated_attachment` performs the `INSERT OR REPLACE` into `attachments` with `was_instantiated = 1`, keyed by `attachment.hash()` computed from the attacker's own bytes, and only tries (best-effort, no failure if it matches zero rows) to flip `is_available` on any `attachment_instances` row that happens to share that hash: [3](#0-2) 
- `Attachment::hash()` is exactly `Hash160::from_data(&self.content)`, i.e., self-consistent but attacker-controlled — there is no comparison against the originally requested `content_hash` anywhere in this path: [4](#0-3) 

Attack flow: a remote peer that the victim node picks as an outbound "sync peer" (used to serve Atlas attachment downloads) can, on receiving a `GetAttachment` request for content-hash `X`, return an HTTP response containing arbitrary bytes `C` where `Hash160(C) != X`. `decode_atlas_get_attachment()` only hex-decodes the payload into an `Attachment` (`stackslib/src/net/atlas/mod.rs` `GetAttachmentResponse::deserialize`) with no hash check, so `extend_with_attachments` happily stores it. The downloader then calls `insert_instantiated_attachment(&attachment)` for this junk attachment unconditionally — it is inserted into the `attachments` table with `was_instantiated = 1` regardless of whether `find_all_attachment_instances(&attachment.hash())` returned any row. There is no eviction routine for `was_instantiated = 1` rows analogous to `evict_expired_uninstantiated_attachments` (which only targets `was_instantiated = 0`), so these rows persist indefinitely.

### Impact Explanation
A remote, unprivileged peer that becomes an outbound sync source for a victim's Atlas downloader can force the victim to permanently store arbitrary attacker-chosen byte blobs (bounded by `attachments_max_size`) in its local `attachments` table on every attachment request/response round, with no corresponding on-chain `AttachmentInstance` ever created for that specific hash. This is disk/storage growth on the victim node uncorrelated to any consensus commitment — each malicious response consumes one DB row permanently marked "instantiated," independent of `attachment_instances` table size. This matches the "attachment/BNS mismatch"-class High-severity impact category (serving/storing data with no matching canonical/on-chain reference), scoped strictly to storage exhaustion via non-consensus-backed attachment persistence.

### Likelihood Explanation
Preconditions: the attacker must be selected by the victim as an outbound Atlas sync peer serving attachment inventories/content (a normal, unprivileged operational role for any node participating in P2P/HTTP Atlas sync — no secret, no signature, no StackerDB slot required). Attacker cost is a single crafted HTTP response per attachment request; the attack is fully repeatable across the retry/batch cycle (`AttachmentsDownloader::run`) for as many distinct fake byte blobs as the attacker wants to send, each producing a new stored row. No rate limiting or hash verification exists to block or throttle this.

### Recommendation
In `extend_with_attachments` (`stackslib/src/net/atlas/download.rs:547`), verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; discard and treat as a failed/faulty response otherwise (bump `report.bump_failed_requests()` or mark the peer faulty). Additionally, `insert_instantiated_attachment` should only be called when `find_all_attachment_instances(&attachment.hash())` returns at least one row, or the DB layer should refuse to persist `was_instantiated = 1` rows that don't correspond to any known `attachment_instances.content_hash`.

### Proof of Concept
Rust test in `stackslib::net::atlas::download` (mirroring existing `extend_with_attachments` unit tests):
1. Construct an `AttachmentRequest` with `content_hash = X` (some `Hash160`).
2. Construct a fake `GetAttachmentResponse` whose `attachment.content` hashes to `Y != X`.
3. Call `AttachmentsBatchStateContext::extend_with_attachments` with a `BatchedRequestsResult` where this request "succeeded" with the mismatched response, and assert the fake attachment is present in `context.attachments` (showing the missing equality check at `download.rs:547-552`).
4. Feed this context through to `AtlasDB::insert_instantiated_attachment` and assert via `SELECT COUNT(*) FROM attachments WHERE was_instantiated=1` that the row count increases while `SELECT COUNT(*) FROM attachment_instances WHERE content_hash = ?` (bound to `Y`) returns 0 — demonstrating storage growth with zero on-chain commitment correlation.

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

**File:** stackslib/src/net/atlas/mod.rs (L153-160)
```rust
impl Attachment {
    pub fn new(content: Vec<u8>) -> Attachment {
        Attachment { content }
    }

    pub fn hash(&self) -> Hash160 {
        Hash160::from_data(&self.content)
    }
```
