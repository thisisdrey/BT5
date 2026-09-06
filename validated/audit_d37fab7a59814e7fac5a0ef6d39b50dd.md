### Title
Unbounded attachment storage via unvalidated `GetAttachmentResponse` content in `insert_instantiated_attachment` - ([File: stackslib/src/net/atlas/db.rs])

### Summary
`AtlasDB::insert_instantiated_attachment` unconditionally executes `INSERT OR REPLACE INTO attachments (... was_instantiated=1 ...)` with no check against `AtlasConfig.attachments_max_size` or `AtlasConfig.max_uninstantiated_attachments`, unlike its sibling `insert_uninstantiated_attachment` which evicts old rows when the uninstantiated count exceeds the configured bound. Because the downloader path (`AttachmentsBatchStateContext::extend_with_attachments` → `AttachmentsDownloader::run`) never verifies that a peer's returned `Attachment` content actually hashes to the `content_hash` that was requested before calling `insert_instantiated_attachment`, a malicious sync peer can return arbitrary, unrelated content on every `GetAttachment` request and have it permanently stored ("instantiated") with zero enforced size/count cap.

### Finding Description
The broken equality: "number/size of stored `attachments` rows == bound enforced by `AtlasConfig` at insertion time" fails for the `insert_instantiated_attachment` path.

- `AtlasConfig::validate` [1](#0-0)  only checks config values at startup; it is never consulted per-insert.
- `insert_uninstantiated_attachment` correctly enforces `max_uninstantiated_attachments` by evicting oldest rows before inserting [2](#0-1) .
- `insert_instantiated_attachment` has no equivalent check at all — it just does an `INSERT OR REPLACE` with `was_instantiated = 1` [3](#0-2) .
- The network path that feeds `insert_instantiated_attachment`: when the `AttachmentsBatchStateMachine` finishes downloading, it drains `context.attachments` (a `HashSet<Attachment>` built purely from decoded HTTP responses) and calls `insert_instantiated_attachment` for every entry regardless of whether any matching `attachment_instances` rows exist (`find_all_attachment_instances` may return an empty vec) [4](#0-3) .
- Crucially, `extend_with_attachments` decodes the HTTP response and inserts `response.attachment` into the set with **no check that `response.attachment.hash() == request.content_hash`** [5](#0-4) .
- `GetAttachmentResponse` deserialization simply hex-decodes whatever bytes the peer supplies into `Attachment::new(bytes)` with no length/hash validation [6](#0-5) .

Exploit flow: an attacker runs their own peer, gets included among a victim's `outbound_sync_peers` (a normal, unprivileged P2P peering outcome), falsely advertises (via `GetAttachmentsInvResponse`) that it holds a real attachment's `content_hash`, gets selected as a `source` in `get_prioritized_attachments_requests` [7](#0-6) , and then, upon receiving a `GetAttachment` request for that hash, responds with a `GetAttachmentResponse` containing arbitrary random content of a different hash. The victim stores this content verbatim via `insert_instantiated_attachment`, with no relation to the requested hash and no bound enforcement. Repeating this with distinct random content each time (distinct `Hash160`) causes each call to be a fresh row (`INSERT OR REPLACE` keyed by hash), growing the `attachments` table without limit.

### Impact Explanation
Each successful forged response is one row permanently added to on-disk SQLite storage (`was_instantiated = 1`), consuming disk space with attacker-chosen content that never corresponds to any validated on-chain attachment/BNS reference. Repeated over many attachment-request cycles (each batch round only issues a bounded set of requests, but the attack is repeatable indefinitely across the downloader's retry loop and across the lifetime of the node), this causes unbounded storage growth on the victim, matching the "attachment/BNS mismatch" and storage-exhaustion characterization in the High severity bucket.

### Likelihood Explanation
Preconditions: the attacker must be one of the victim's outbound sync peers (achievable by any unprivileged remote node through normal P2P handshake/neighbor-walk, no secret or privileged role required) and must be selected as a "source" for an attachment request by claiming (via inv response) to possess a given `content_hash`. Both are attacker-controlled and require no privileged access — only running a peer and answering RPC/P2P HTTP endpoints that the Atlas downloader queries. Repeatability is bounded by the downloader's batching/retry cadence rather than a hard cap, so an attacker sustaining peering with the victim can continue injecting distinct-hash garbage over time.

### Recommendation
1. In `AttachmentsBatchStateContext::extend_with_attachments` (`stackslib/src/net/atlas/download.rs`), reject/discard any `GetAttachmentResponse` whose `attachment.hash()` does not equal `request.content_hash` before inserting into `self.attachments`.
2. In `AtlasDB::insert_instantiated_attachment`, enforce `AtlasConfig.attachments_max_size` on `attachment.content.len()` and enforce an overall bound/eviction policy on the `attachments` table (mirroring the eviction logic already present in `insert_uninstantiated_attachment`), rejecting or evicting when limits are exceeded.

### Proof of Concept
Rust unit test in `stackslib/src/net/atlas/tests.rs`:
```rust
#[test]
fn test_insert_instantiated_attachment_unbounded() {
    let atlas_config = AtlasConfig {
        contracts: HashSet::new(),
        attachments_max_size: 1024,
        max_uninstantiated_attachments: 50_000, // MAX_UNINSTANTIATED_ATTACHMENTS_MIN
        uninstantiated_attachments_expire_after: 86_400,
        unresolved_attachment_instances_expire_after: 172_800,
        genesis_attachments: None,
    };
    let mut atlas_db = AtlasDB::connect_memory(atlas_config).unwrap();

    // Simulate N forged GetAttachmentResponse deliveries with distinct random content
    // and NO matching attachment_instances rows.
    let n = 200_000; // far exceeds MAX_UNINSTANTIATED_ATTACHMENTS_MIN
    for i in 0..n {
        let content = format!("forged-random-content-{i}").into_bytes();
        let attachment = Attachment::new(content);
        // No attachment_instances row exists for attachment.hash()
        atlas_db.insert_instantiated_attachment(&attachment).unwrap(); // never rejected
    }

    let count: u32 = query_count(
        &atlas_db.conn,
        "SELECT COUNT(rowid) FROM attachments WHERE was_instantiated = 1",
        NO_PARAMS,
    ).unwrap() as u32;

    // Demonstrates no cap is enforced despite exceeding MAX_UNINSTANTIATED_ATTACHMENTS_MIN
    assert_eq!(count, n);
}
```
This confirms `insert_instantiated_attachment` never rejects inserts irrespective of `AtlasConfig.max_uninstantiated_attachments`/`attachments_max_size`, corroborating the network-reachable exploit via forged `GetAttachmentResponse` content in `AttachmentsBatchStateContext::extend_with_attachments`.

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

**File:** stackslib/src/net/atlas/download.rs (L404-474)
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
