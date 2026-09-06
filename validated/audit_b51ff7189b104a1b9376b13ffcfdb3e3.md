### Title
Unvalidated attachment hash allows unauthenticated write of attacker-chosen, uncommitted blobs into the `attachments` table - (File: stackslib/src/net/atlas/download.rs)

### Summary
When `AttachmentsBatchStateContext::extend_with_attachments` processes a peer's `GetAttachmentResponse`, it inserts the returned `Attachment` into `context.attachments` without ever checking that `attachment.hash()` matches the `content_hash` of the `AttachmentRequest` that was sent. The `Done` state handler in `AttachmentsDownloader::run` then unconditionally calls `insert_instantiated_attachment(&attachment)`, persisting the attacker-supplied content keyed by its own (attacker-controlled) hash, with no cap/eviction logic applied to "instantiated" rows.

### Finding Description
`AttachmentsBatchStateContext::get_prioritized_attachments_requests` builds an `AttachmentRequest` for a specific `content_hash` (H1) taken from an on-chain `AttachmentInstance` [1](#0-0) . When the response comes back, `extend_with_attachments` decodes it and inserts `response.attachment` into the `attachments: HashSet<Attachment>` field with **no comparison against `request.content_hash`**: [2](#0-1) 

In `AttachmentsDownloader::run`, the `Done` branch iterates `context.attachments.drain()`, computes `attachment.hash()` (the *actual* content hash, potentially H2 ≠ H1), looks up `find_all_attachment_instances(&attachment.hash())`, and — regardless of whether any instance was found — unconditionally calls `network.atlasdb.insert_instantiated_attachment(&attachment)`: [3](#0-2) 

`insert_instantiated_attachment` performs `INSERT OR REPLACE INTO attachments (hash, content, was_instantiated, created_at) VALUES (?, ?, 1, ?)` keyed by `attachment.hash()`, with no check that a matching `AttachmentInstance.content_hash` exists, and — critically — with **no bound/eviction analogous to `max_uninstantiated_attachments`**, since the eviction logic (`evict_k_oldest_uninstantiated_attachments`, `evict_expired_uninstantiated_attachments`) only targets rows with `was_instantiated = 0`: [4](#0-3) [5](#0-4) [6](#0-5) 

`find_all_attachment_instances` only returns rows whose `content_hash` matches the (attacker-chosen) hash and whose status is `Checked`; since no on-chain instance ever references H2, this query returns empty and no attachment instance is "resolved" — but the row is still written to disk under H2 via `insert_instantiated_attachment`, as it is called unconditionally before checking whether any instances were found [7](#0-6) . `find_attachment(&H1)` therefore still returns `None` because H1 was never the key of any stored row, while an attacker-fabricated blob now persists in the DB, keyed under `was_instantiated = 1` with no eviction path — meaning normal periodic cleanup (`evict_expired_uninstantiated_attachments`) will never remove it.

### Impact Explanation
Any peer that gets selected as a data source for an `AttachmentRequest` (which only requires the peer to have earlier claimed the attachment in its inventory response) can return an arbitrary blob whose true hash differs from the requested `content_hash`. That data is persisted permanently (as `was_instantiated = 1`, exempt from the uninstantiated-row eviction routines) with no matching committed `AttachmentInstance`. This is a form of unauthenticated write to local node state with no bound on repeated exploitation — an attacker can repeat this for every attachment request it is chosen to serve, growing the `attachments` table without limit, since only `was_instantiated = 0` rows are capped/evicted. This matches "unauthenticated/unauthorized write to state" (Critical) and also "attachment/BNS mismatch" (High), since it stores non-canonical data under a hash with no on-chain commitment.

### Likelihood Explanation
Preconditions are modest and squarely within the unprivileged remote-attacker model: the attacker runs their own peer, responds to a legitimate `AttachmentsInventoryRequest` claiming to have the missing attachment (this claim is not independently verified before issuing the follow-up `AttachmentRequest`), and then answers the `AttachmentRequest` with arbitrary bytes packaged as a `GetAttachmentResponse`. No secret, signature, or privileged role is needed. This is repeatable per attachment-batch cycle and does not require any bandwidth-flooding — a single crafted response per request suffices.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (or immediately before persisting), verify `response.attachment.hash() == request.content_hash` before inserting into `context.attachments`; discard/treat as a faulty-peer response otherwise. Additionally, `insert_instantiated_attachment` should only persist an attachment if `find_all_attachment_instances` (or an equivalent existence check for a matching committed `content_hash`) returns non-empty, and/or "instantiated" rows should also be subject to a bound/eviction policy.

### Proof of Concept
Rust net test in `stackslib/src/net/atlas/tests.rs`:
1. Construct an `AttachmentsBatchStateContext` with an `AttachmentRequest{ content_hash: H1, ... }`.
2. Build a `BatchedRequestsResult` whose `succeeded` map pairs that `AttachmentRequest` with a `StacksHttpResponse` decoding to a `GetAttachmentResponse` containing `Attachment{ content: attacker_bytes }` where `Attachment::hash() == H2 != H1`.
3. Call `context.extend_with_attachments(&mut results)` and then drive `AttachmentsDownloader::run`'s `Done` handling logic (or directly call `atlasdb.insert_instantiated_attachment(&attachment)`).
4. Assert `atlasdb.find_attachment(&H1).unwrap().is_none()` (request never satisfied).
5. Assert the row exists under H2: `atlasdb.find_attachment(&H2).unwrap().is_some()` (attacker data persisted).
6. Assert `atlasdb.find_all_attachment_instances(&H2).unwrap().is_empty()` (no committed on-chain instance ever referenced H2), demonstrating uncommitted, unbounded data injected into the `attachments` table.

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

**File:** stackslib/src/net/atlas/download.rs (L404-472)
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

**File:** stackslib/src/net/atlas/db.rs (L549-561)
```rust
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

**File:** stackslib/src/net/atlas/db.rs (L630-639)
```rust
    pub fn find_all_attachment_instances(
        &self,
        content_hash: &Hash160,
    ) -> Result<Vec<AttachmentInstance>, db_error> {
        let hex_content_hash = to_hex(&content_hash.0[..]);
        let qry = "SELECT * FROM attachment_instances WHERE content_hash = ?1 AND status = ?2";
        let args = params![hex_content_hash, AttachmentInstanceStatus::Checked];
        let rows = query_rows(&self.conn, qry, args)?;
        Ok(rows)
    }
```
