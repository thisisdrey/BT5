### Title
Attacker-served attachment content is stored under `was_instantiated=1` with no hash verification, creating a permanent unevictable storage-growth path unrelated to any on-chain commitment - (File: stackslib/src/net/atlas/download.rs, stackslib/src/net/atlas/db.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` accepts any `Attachment` body returned by a peer for an `AttachmentRequest` without verifying that `attachment.hash() == request.content_hash`. The accepted content is then unconditionally written via `AtlasDB::insert_instantiated_attachment` with `was_instantiated = 1`, and `AtlasDB`'s eviction routines (`evict_k_oldest_uninstantiated_attachments`, `evict_expired_uninstantiated_attachments`) only ever delete rows with `was_instantiated = 0`. This breaks the assumed invariant that "all uncommitted attachment rows are evictable," because a garbage payload with no matching `attachment_instances` row is stored as if instantiated/validated and can never be reclaimed.

### Finding Description
The claimed equality — "evictable_uncommitted_rows == all_uncommitted_rows_in_table" — does not hold. Eviction only scans `was_instantiated = 0`: [1](#0-0) 

But data can enter the `attachments` table with `was_instantiated = 1` from an unauthenticated download response, without ever being tied to a real on-chain `attachment_instances` commitment. In `extend_with_attachments`, a peer's HTTP response is decoded and its `attachment` field is inserted into the batch's result set with no check that its hash matches the requested `content_hash`: [2](#0-1) 

When the state machine reaches `Done`, every attachment collected this way is committed via `insert_instantiated_attachment` unconditionally — the lookup of matching `attachment_instances` (`find_all_attachment_instances(&attachment.hash())`) is only used to decide which resolved instances to report, not whether to store the content: [3](#0-2) 

`insert_instantiated_attachment` writes the row keyed by `attachment.hash()` (the hash of the attacker-supplied bytes) with `was_instantiated = 1`, regardless of whether any instance actually references that hash: [4](#0-3) 

Because `was_instantiated = 1`, this row is permanently excluded from both eviction paths, which only target `was_instantiated = 0` rows: [5](#0-4) [6](#0-5) 

Exploit flow: a node has a legitimate queued `AttachmentInstance` (from processing a real on-chain contract event) for some `content_hash`. It builds an `AttachmentRequest` and selects a source URL among peers whose `/v2/attachments/inv` responses claimed to have the content — an attacker's own peer can trivially claim this in its inventory response. The victim then issues `GET /v2/attachments/<content_hash>` to the attacker's node. The attacker replies with an HTTP 200 body containing arbitrary bytes as `Attachment.content` (bounded only by whatever body-size cap exists on the HTTP client, not by `content_hash` correctness). `extend_with_attachments` accepts it into `self.attachments` without hash verification, and it is written to `attachments` with `was_instantiated = 1`, `hash = Hash160(attacker_bytes)` — a fresh, distinct hash each time the attacker varies its payload. The row is never associated with any `attachment_instances` entry (since the real instance's `content_hash` differs from the fabricated hash) and is never evicted.

### Impact Explanation
Each malicious response call permanently consumes disk space in the victim's `attachments` table with `was_instantiated = 1` content unconnected to any consensus-committed `attachment_instances` row. This is repeatable per attachment-batch retry cycle and per distinct payload byte pattern (varying content yields a distinct hash, defeating dedup), giving an attacker an unbounded, non-evictable disk-growth primitive against any node that ends up choosing the attacker as an attachment-download source. This matches the "attachment/BNS mismatch" High-impact category: content is stored and treated as a validated/instantiated attachment despite corresponding to no actual on-chain commitment, and the storage-growth is not remediable by the DB's own designed eviction/expiry mechanism (`max_uninstantiated_attachments` / `uninstantiated_attachments_expire_after` configs are silently bypassed).

### Likelihood Explanation
Preconditions are modest and match the allowed unprivileged-remote-attacker model: the attacker runs their own P2P peer, advertises a data URL, and answers `/v2/attachments/inv` and `/v2/attachments/<hash>` requests over its own RPC surface — no secret, no privileged role, no local access needed. The only requirement is that the victim selects the attacker as a download source for some `AttachmentRequest`, which happens naturally once the attacker's inventory response claims to hold the requested page/index. The attack is cheap (one crafted HTTP response per request) and repeatable indefinitely as attachment instances continue to be created/processed.

### Recommendation
In `extend_with_attachments` (or immediately before), verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; discard and penalize (via `report.bump_failed_requests()`) any response whose content hash does not match the requested hash. As defense in depth, `insert_instantiated_attachment` should refuse to store `was_instantiated = 1` content unless a corresponding `attachment_instances` row is confirmed to exist for that hash, and/or `evict_expired_uninstantiated_attachments`/`evict_k_oldest_uninstantiated_attachments` should also cap/evict `was_instantiated = 1` rows that have zero matching `attachment_instances` after some age.

### Proof of Concept
Rust test plan in `stackslib/src/net/atlas/tests.rs` (or a new download-poisoning test module):
1. Construct an `AtlasDB` in memory and a real `AttachmentInstance` for `content_hash = H(real_bytes)`, queued normally.
2. Build an `AttachmentsBatchStateContext` and craft a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map associates the `AttachmentRequest{content_hash: H(real_bytes), ...}` with a `StacksHttpResponse` whose decoded `GetAttachmentResponse.attachment.content = garbage_bytes` (so `attachment.hash() = H(garbage_bytes) != H(real_bytes)`).
3. Call `extend_with_attachments(&mut results)` and observe `context.attachments` contains the garbage `Attachment` despite the hash mismatch — assert `context.attachments.iter().any(|a| a.hash() != real_content_hash)`.
4. Drive this into `AttachmentsDownloader::run`/the `Done` arm equivalent (or call `atlas_db.insert_instantiated_attachment(&garbage_attachment)` directly to mirror the exact production code path at `download.rs:161`).
5. Call `atlas_db.evict_k_oldest_uninstantiated_attachments(u32::MAX)` and `atlas_db.evict_expired_uninstantiated_attachments()`.
6. Assert: `SELECT COUNT(*) FROM attachments WHERE was_instantiated = 1 AND hash = <hex of H(garbage_bytes)>` still returns 1 (row survives eviction), and `find_all_attachment_instances(&H(garbage_bytes))` returns an empty vec (proving the stored content is tied to no consensus commitment). Repeat with N distinct garbage payloads to show table growth is unbounded and none are reclaimed.

### Citations

**File:** stackslib/src/net/atlas/db.rs (L538-560)
```rust
    pub fn evict_k_oldest_uninstantiated_attachments(&mut self, k: u32) -> Result<(), db_error> {
        let tx = self.tx_begin()?;
        let res = tx.execute(
            "DELETE FROM attachments WHERE hash IN (SELECT hash FROM attachments WHERE was_instantiated = 0 ORDER BY created_at ASC LIMIT ?)",
            params![k],
        );
        res.map_err(db_error::SqliteError)?;
        tx.commit().map_err(db_error::SqliteError)?;
        Ok(())
    }

    pub fn evict_expired_uninstantiated_attachments(&mut self) -> Result<(), db_error> {
        let now = util::get_epoch_time_secs() as i64;
        let cut_off = now - self.atlas_config.uninstantiated_attachments_expire_after as i64;
        let tx = self.tx_begin()?;
        let res = tx.execute(
            "DELETE FROM attachments WHERE was_instantiated = 0 AND created_at < ?",
            params![cut_off],
        );
        res.map_err(db_error::SqliteError)?;
        tx.commit().map_err(db_error::SqliteError)?;
        Ok(())
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
