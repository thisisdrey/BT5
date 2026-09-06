### Title
Unbounded, unevictable growth of `AtlasDB.attachments` via unverified `insert_instantiated_attachment` writes that no eviction routine ever reaches - ([File: stackslib/src/net/atlas/db.rs])

### Summary
`evict_expired_uninstantiated_attachments` and `evict_k_oldest_uninstantiated_attachments` only ever delete rows with `was_instantiated = 0`, and `insert_instantiated_attachment` writes rows directly with `was_instantiated = 1` with no size cap and no hash verification against the originally requested content. A remote peer that is selected as an "attachment source" can therefore inject arbitrary content that becomes permanently unevictable in the local SQLite `attachments` table.

### Finding Description
The equality the codebase relies on — "every stored attachment is subject to some eviction/expiry bound" — is broken because the two eviction functions filter exclusively on `was_instantiated = 0`: [1](#0-0) 

while `insert_instantiated_attachment` inserts rows with `was_instantiated = 1` unconditionally, with no size cap check equivalent to `count_uninstantiated_attachments`/`max_uninstantiated_attachments`: [2](#0-1) 

The reachable path from an untrusted remote peer is in `AttachmentsDownloader`. A peer becomes an eligible "source" for a specific attachment simply by claiming (via `GetAttachmentsInvResponse`) to have the content for a legitimately-queued, on-chain-derived `content_hash`: [3](#0-2) 

When the victim later sends a `GetAttachment` request to that peer, the response is decoded and inserted into the in-memory `HashSet<Attachment>` with **no verification that the returned bytes hash to the requested `content_hash`**: [4](#0-3) 

Then, in `AttachmentsDownloader::run`, every attachment collected this way is unconditionally passed to `insert_instantiated_attachment`, regardless of whether any real `attachment_instances` actually matched its hash: [5](#0-4) 

Because this write path lands directly in `was_instantiated = 1` and neither `evict_expired_uninstantiated_attachments` nor `evict_k_oldest_uninstantiated_attachments` ever touches that state, the attacker-controlled content is never expired or capacity-evicted by any AtlasDB mechanism — only `evict_expired_unresolved_attachment_instances`, which acts on the separate `attachment_instances` table and does not delete `attachments` rows, runs alongside it.

### Impact Explanation
A malicious peer can repeatedly claim (falsely) to hold missing attachments referenced by legitimate on-chain `AttachmentInstance` records, then serve arbitrary garbage bytes for each `GetAttachment` request. Each accepted response results in a permanent, unbounded row in the victim's `attachments` SQLite table with `was_instantiated = 1`, since no cap or eviction path ever reaches it. This is an unauthenticated write to persistent local node state with no reclaim mechanism, causing unbounded disk growth purely from being selected as a peer/source and answering HTTP GetAttachment requests with fabricated data — no secret, signature, or privileged role required.

### Likelihood Explanation
The attacker only needs to run an ordinary peer that (a) gets included among the victim's outbound sync peers, and (b) responds to `AttachmentsInventoryRequest`/`GetAttachment` requests, which requires no authentication, secret, or special role — any remote peer satisfies this. The attack is fully repeatable per attachment instance and can be scaled across many queued/on-chain content hashes, and across restarts since the DB persists to disk.

### Recommendation
1. In `AttachmentsBatchStateContext::extend_with_attachments` (`download.rs`), verify `response.attachment.hash() == request.content_hash` before accepting the attachment; discard and penalize (via `report.bump_failed_requests()`) mismatched responses.
2. Enforce a total size/count cap on `was_instantiated = 1` rows (mirroring `max_uninstantiated_attachments`), and add an eviction routine for instantiated attachments (e.g., LRU/expiry) analogous to `evict_k_oldest_uninstantiated_attachments`.

### Proof of Concept
Rust test in `stackslib::net::atlas::db`:
```rust
#[test]
fn test_instantiated_attachments_are_never_evicted() {
    let atlas_config = AtlasConfig::new(false /* mainnet */);
    let mut db = AtlasDB::connect_memory(atlas_config).unwrap();

    for i in 0..50u8 {
        let attachment = Attachment { content: vec![i; 32] }; // fabricated bytes
        db.insert_instantiated_attachment(&attachment).unwrap();
    }

    let before = query_count(&db.conn, "SELECT COUNT(rowid) FROM attachments WHERE was_instantiated = 1", NO_PARAMS).unwrap();
    assert_eq!(before, 50);

    db.evict_expired_uninstantiated_attachments().unwrap();
    db.evict_k_oldest_uninstantiated_attachments(1000).unwrap();

    let after = query_count(&db.conn, "SELECT COUNT(rowid) FROM attachments WHERE was_instantiated = 1", NO_PARAMS).unwrap();
    assert_eq!(after, 50); // unchanged: no eviction path reaches was_instantiated=1 rows
}
```
This demonstrates that `evict_expired_uninstantiated_attachments` (db.rs:549-560) and `evict_k_oldest_uninstantiated_attachments` (db.rs:538-547) never affect rows inserted via `insert_instantiated_attachment` (db.rs:576-592), confirming the unbounded, unevictable growth reachable end-to-end through `download.rs`'s unverified attachment acceptance path (download.rs:530-558, 153-175).

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

**File:** stackslib/src/net/atlas/download.rs (L153-175)
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

                // Carrying events for centralized deregistration
                events_to_deregister.append(&mut context.events_to_deregister);

                // Every once in a while, we delete uninstantiated attachments
                network.atlasdb.evict_expired_uninstantiated_attachments()?;
```

**File:** stackslib/src/net/atlas/download.rs (L430-472)
```rust
                let mut sources = HashMap::new();
                let position_in_page =
                    attachment_index % AttachmentInstance::ATTACHMENTS_INV_PAGE_SIZE;

                for (peer_url, response) in peers_responses.iter() {
                    // Considering the response, look for the page with the index
                    // we're looking for.
                    let search_page = response.pages.iter().find(|page| page.index == page_index);

                    let has_attachment = search_page
                        .and_then(|search_page| {
                            search_page.inventory.get(position_in_page as usize)
                        })
                        .map(|result| *result == 1)
                        .unwrap_or(false);

                    if !has_attachment {
                        debug!(
                            "Atlas: peer does not have attachment ({}, {}) in its inventory {:?}",
                            page_index, position_in_page, response.pages
                        );
                        continue;
                    }

                    let report = self
                        .peers
                        .get(peer_url)
                        .expect("Atlas: unable to retrieve reliability report for peer");
                    sources.insert(peer_url.clone(), report.clone());
                }

                if sources.is_empty() {
                    warn!("Atlas: could not find a peer including attachment in its inventory");
                    continue;
                }

                // Success, we found at least one inventory including the attachment we're looking for.
                let request = AttachmentRequest {
                    sources,
                    content_hash: content_hash.clone(),
                    stacks_block_height: self.attachments_batch.stacks_block_height,
                    canonical_stacks_tip_height: self.attachments_batch.canonical_stacks_tip_height,
                };
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
