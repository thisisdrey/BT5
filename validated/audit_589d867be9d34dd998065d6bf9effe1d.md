### Title
Unvalidated attachment content accepted and permanently persisted as "instantiated", bypassing the `max_uninstantiated_attachments` bound - ([File: stackslib/src/net/atlas/download.rs, stackslib/src/net/atlas/db.rs])

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` accepts any HTTP `GET /v2/attachments/{hash}` response and inserts the returned `Attachment` into the batch's result set without verifying that `Hash160::from_data(content)` matches the originally requested `content_hash`. The downloader then unconditionally calls `AtlasDB::insert_instantiated_attachment`, which writes the bogus content directly into the `attachments` table with `was_instantiated = 1`, a bucket that `evict_expired_uninstantiated_attachments`/`count_uninstantiated_attachments`/`max_uninstantiated_attachments` never touch, allowing unbounded growth of committed-looking attachment storage from content that was never actually validated against any consensus-queued commitment.

### Finding Description
The equality the Atlas subsystem is supposed to maintain is: every row in the `attachments` table with `was_instantiated = 1` corresponds to content whose `Hash160` actually equals a `content_hash` committed on-chain via a queued `AttachmentInstance`. This equality is broken.

- `AttachmentRequest` is built per queued `content_hash` and sent to peer(s) claiming (via inventory) to have it [1](#0-0) .
- When the response comes back, `extend_with_attachments` decodes it and inserts `response.attachment` straight into `self.attachments` with **no check** that `response.attachment.hash() == request.content_hash` [2](#0-1) .
- In the `Done` state, every drained attachment is unconditionally written via `insert_instantiated_attachment`, keyed by the content's *own* computed hash (not the originally requested hash) [3](#0-2) .
- `insert_instantiated_attachment` always sets `was_instantiated = 1` and uses `INSERT OR REPLACE`, keyed by `attachment.hash()` [4](#0-3) .
- `evict_expired_uninstantiated_attachments`, `evict_k_oldest_uninstantiated_attachments`, and `count_uninstantiated_attachments` only ever operate on `WHERE was_instantiated = 0` [5](#0-4) ; `insert_uninstantiated_attachment` is the only path that enforces `max_uninstantiated_attachments` [6](#0-5) , and `insert_instantiated_attachment` never routes through it.

Because the batch's internal matching (`attachments_batch.resolve_attachment(&attachment.hash())`) also keys off the attacker-controlled hash, a bogus response simply fails to resolve the originally-tracked `content_hash` and the batch is retried (up to `max_attachment_retry_count`) — but the bogus row has *already* been permanently committed to disk before that mismatch is even noticed. Any remote peer that is selected as a source for a queued `content_hash` (which only requires the attacker's own peer to advertise the hash as present in its attachment inventory) can return a unique fabricated payload on every request/retry. Each such payload produces a new, permanent row in `attachments` because the primary key is `Hash160::from_data(content)`, which the attacker fully controls.

### Impact Explanation
An unprivileged remote peer can force the victim node to persist unbounded amounts of attacker-chosen data into its local Atlas SQLite database as "instantiated" (`was_instantiated = 1`) attachments, with no bound analogous to `max_uninstantiated_attachments`, and no requirement that the data actually correspond to any consensus-committed `content_hash`. This is an unauthenticated write to node state via network input, and repeatable indefinitely (new sybil peer identities and/or retries each yield new unique hashes), causing disk/storage exhaustion — a real, repeatable resource-exhaustion vector tied to a subsystem the question is specifically scoped to (Atlas attachments), not the excluded epoch2x/neon block-download/inv paths.

### Likelihood Explanation
- Attacker needs only to run an ordinary P2P/HTTP peer that the victim already talks to and gossips an attachment inventory bit claiming to hold some queued `content_hash` (this only requires the attacker to be a normal peer, no secret or privileged role).
- The victim's Atlas downloader will then legitimately issue `GET /v2/attachments/{content_hash}` to that attacker peer [7](#0-6) .
- The attacker returns arbitrary bytes (bounded only by `attachments_max_size`, not row count) as the attachment body; each unique payload becomes a unique permanent row.
- The attack can be repeated across retries (bounded by `max_attachment_retry_count` per batch) and across multiple attacker-controlled peer identities per single legitimately-queued instance, and is fully remote/unauthenticated.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (stackslib/src/net/atlas/download.rs), verify `response.attachment.hash() == request.content_hash` before accepting the response into `self.attachments`; discard/penalize (bump_failed_requests) any response whose hash does not match. Additionally, `AtlasDB::insert_instantiated_attachment` should reject or discard attachments whose content hash does not correspond to any known/queued `attachment_instances.content_hash`, and total row growth in the `attachments` table (both buckets) should be bounded, not just the `was_instantiated = 0` subset.

### Proof of Concept
Rust test plan (stackslib/src/net/atlas/tests.rs or a new download.rs test using the existing mock HTTP peer harness used for `AttachmentsBatchStateMachine`/`AttachmentsDownloader` tests):
1. Configure `AtlasConfig { max_uninstantiated_attachments: 5, .. }` and start an `AtlasDB`.
2. Queue N (N > max_uninstantiated_attachments) distinct `AttachmentInstance` values via `queue_attachment_instance`, each with a distinct `content_hash`.
3. Run the attachment inventory/download state machine against a mock peer server that, for each requested `content_hash`, returns a `GetAttachmentResponse` whose `attachment.content` hashes to a value different from the requested `content_hash` (i.e., bogus/non-matching payload, unique per request).
4. After processing completes, query `SELECT COUNT(*) FROM attachments` directly (or via a helper) and assert the count equals N, and `count_uninstantiated_attachments()` remains ≤ `max_uninstantiated_attachments` — demonstrating the `attachments` table (via `was_instantiated = 1` rows from `insert_instantiated_attachment`) grew unboundedly relative to `max_uninstantiated_attachments`, with none of the stored content actually matching any queued `AttachmentInstance.content_hash`.

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

**File:** stackslib/src/net/atlas/download.rs (L404-478)
```rust
    pub fn get_prioritized_attachments_requests(&self) -> BinaryHeap<AttachmentRequest> {
        let mut queue = BinaryHeap::new();
        let mut enqueued = HashSet::new();
        for ((contract_id, pages, _), peers_responses) in self.inventories.iter() {
            let missing_attachments = match self
                .attachments_batch
                .attachments_instances
                .get(contract_id)
            {
                None => continue,
                Some(missing_attachments) => missing_attachments,
            };
            // Note: we're getting missing_attachments (attachment_id: content_hash)
            for (attachment_index, content_hash) in missing_attachments.iter() {
                let page_index = attachment_index / AttachmentInstance::ATTACHMENTS_INV_PAGE_SIZE;
                // Since there's a limit in the number of pages that a node can request,
                // we can potentially have multiple inventory request at once.
                if !pages.contains(&page_index) {
                    continue;
                }

                if enqueued.contains(content_hash) {
                    debug!("Atlas: {} already enqueued", content_hash);
                    continue;
                }

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
                enqueued.insert(content_hash);
                queue.push(request);
            }
        }
        queue
    }
```

**File:** stackslib/src/net/atlas/download.rs (L530-553)
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
```

**File:** stackslib/src/net/atlas/db.rs (L511-536)
```rust
    pub fn insert_uninstantiated_attachment(
        &mut self,
        attachment: &Attachment,
    ) -> Result<(), db_error> {
        // Insert the new attachment
        let uninstantiated_attachments = self.count_uninstantiated_attachments()?;
        if uninstantiated_attachments >= self.atlas_config.max_uninstantiated_attachments {
            let to_delete =
                1 + uninstantiated_attachments - self.atlas_config.max_uninstantiated_attachments;
            self.evict_k_oldest_uninstantiated_attachments(to_delete)?;
        }

        let tx = self.tx_begin()?;
        let now = util::get_epoch_time_secs() as i64;
        let res = tx.execute(
            "INSERT OR REPLACE INTO attachments (hash, content, was_instantiated, created_at) VALUES (?, ?, 0, ?)",
            params![
                attachment.hash(),
                attachment.content,
                now,
            ],
        );
        res.map_err(db_error::SqliteError)?;
        tx.commit().map_err(db_error::SqliteError)?;
        Ok(())
    }
```

**File:** stackslib/src/net/atlas/db.rs (L538-567)
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

    pub fn count_uninstantiated_attachments(&self) -> Result<u32, db_error> {
        let qry = "SELECT COUNT(rowid) FROM attachments
                   WHERE was_instantiated = 0";
        let count = query_count(&self.conn, qry, NO_PARAMS)? as u32;
        Ok(count)
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
