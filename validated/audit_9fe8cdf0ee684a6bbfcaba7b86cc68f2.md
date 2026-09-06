### Title
Attacker-served attachment content is stored under its own hash without verifying it matches the requested `content_hash`, polluting `AtlasDB.attachments` with fabricated rows - ([File: stackslib/src/net/atlas/download.rs])

### Summary
When the Atlas downloader fetches an attachment from the "most reliable" (and possibly only) peer source for a given `content_hash`, `AttachmentsBatchStateContext::extend_with_attachments` accepts and stores whatever bytes the peer returns without checking `Hash160::from_data(&content) == request.content_hash`. The content is then persisted via `AtlasDB::insert_instantiated_attachment`, which computes and stores the row keyed by the attacker-controlled `attachment.hash()` rather than the originally requested/committed hash, so the `attachments` table accumulates rows whose `hash` column has no relation to any on-chain-committed `AttachmentInstance.content_hash`.

### Finding Description
The equality the system should enforce is: for every row inserted into `attachments`, `hash == Hash160::from_data(content)` **and** that hash equals a `content_hash` that was actually queued via a confirmed BNS `AttachmentInstance` (`queue_attachment_instance`). The code only guarantees the first half (the SQL always stores `attachment.hash()`, so a row's stored hash is internally self-consistent with its content) but never checks the second half.

The exploit path:
1. `AttachmentsDownloader::get_prioritized_attachments_requests` builds an `AttachmentRequest` for a legitimately missing `content_hash` [1](#0-0) , and `AttachmentRequest::get_most_reliable_source` picks a peer URL to query, which can be a single attacker-controlled peer if it is the only source in `sources` [2](#0-1) .
2. The request is `GET /v2/attachments/<content_hash>` [3](#0-2) . The attacker peer replies with an HTTP 200 body containing a hex-encoded `GetAttachmentResponse` whose `attachment.content` is arbitrary, attacker-chosen bytes unrelated to `content_hash` (up to `attachments_max_size`, min 1 MiB) [4](#0-3) .
3. `AttachmentsBatchStateContext::extend_with_attachments` decodes the response and inserts `response.attachment` into `self.attachments` (a `HashSet<Attachment>`) with **no check** that `response.attachment.hash() == request.content_hash` [5](#0-4) .
4. `AttachmentsDownloader::run` then iterates `context.attachments.drain()` and calls `network.atlasdb.insert_instantiated_attachment(&attachment)` for each fabricated `Attachment` [6](#0-5) .
5. `insert_instantiated_attachment` executes `INSERT OR REPLACE INTO attachments (hash, content, was_instantiated, created_at) VALUES (?, ?, 1, ?)` using `attachment.hash()` — i.e., `Hash160::from_data(&fabricated_content)` — with no comparison to any queued/committed `content_hash` [7](#0-6) .

Because the stored key is derived purely from the attacker's own bytes, the attacker can produce an unlimited number of distinct fabricated `(hash, content)` pairs, each up to ~1 MiB, and every one is persisted as `was_instantiated = 1` regardless of whether it maps to a real BNS commitment. The follow-up `UPDATE attachment_instances SET is_available = 1 WHERE content_hash = ?1` uses the same fabricated hash, so it will not match the real, still-outstanding instance (whose `content_hash` differs) — the legitimate instance stays unresolved and is retried, giving the attacker repeated opportunities to inject more garbage rows on every retry/batch cycle, all while never fulfilling the actual requested content.

No code path checks `stored_hash == Hash160::from_data(&content)` against the *requested* `content_hash` before writing to the `attachments` table; the only cap present is the per-attachment content-length limit enforced at the HTTP/JSON layer (bounding each row to `attachments_max_size`), not the total number of rows or their legitimacy.

### Impact Explanation
A single malicious peer that is selected as a `content_hash`'s only/most-reliable source can, on every Atlas download cycle, cause the victim node to write attacker-chosen ~1 MiB blobs into its local `attachments` SQLite table under fabricated hashes that never correspond to any confirmed BNS `AttachmentInstance.content_hash`. This is a persistent, repeatable local storage-exhaustion vector (bounded per insert by `attachments_max_size`, unbounded in row count over time since batches needing the un-served real hash keep retrying) and it degrades the node's Atlas subsystem by never actually resolving the legitimate attachment while filling the DB with junk. This matches the "attachment/BNS mismatch" High-impact category: content served and stored as if valid Atlas data despite not being committed by any on-chain BNS name-op.

### Likelihood Explanation
- The attacker needs no privileges: any P2P/RPC-reachable node can serve attachment content via a plain HTTP response, since Atlas peer selection here relies on outbound sync peers and reliability scores.
- Being the "most reliable"/only source for a content_hash is plausible for an attacker running its own peer with which the victim syncs, especially early in scoring or if it's the sole peer that claims (via `/v2/attachments/inv`) to have that attachment in its inventory.
- The attack is cheap (one HTTP response per content_hash per retry cycle) and repeatable indefinitely, bounded only by retry-count settings and eviction of uninstantiated attachments (which does not apply here since these rows are marked `was_instantiated = 1`, i.e., not evicted by `evict_expired_uninstantiated_attachments`, which only targets `was_instantiated = 0` rows) [8](#0-7) .

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (or immediately before storing), verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; on mismatch, treat it as a failed/faulty response (bump `report.bump_failed_requests()` and optionally penalize/deregister the peer) instead of accepting the content. Additionally, `AtlasDB::insert_instantiated_attachment` should require the caller to supply the expected `content_hash` and assert it matches `Attachment::hash()` before performing the `INSERT OR REPLACE`, to defend against any other future caller that might skip the check.

### Proof of Concept
Rust test plan in `stackslib::net::atlas::download` (or `tests.rs`):
1. Build an `AttachmentsBatchStateContext` with a single `AttachmentRequest` for a known `content_hash` `H`, sourced from one peer URL.
2. Simulate `BatchedRequestsResult::succeeded` containing a `StacksHttpResponse` whose decoded `GetAttachmentResponse.attachment.content` is arbitrary bytes `X` such that `Hash160::from_data(&X) != H`.
3. Call `context.extend_with_attachments(&mut results)` and assert `context.attachments` contains the `Attachment{content: X}` despite the hash mismatch (demonstrating no equality check).
4. Feed this into `AttachmentsDownloader::run`'s `Done` branch equivalent, or directly call `atlasdb.insert_instantiated_attachment(&Attachment{content: X})`, then assert `atlasdb.find_attachment(&Hash160::from_data(&X)).unwrap().is_some()` while `atlasdb.find_all_attachment_instances(&H)` still shows the instance unresolved (`is_available == 0`), proving a fabricated, non-committed hash was persisted as instantiated attachment data. Repeating with N distinct fabricated payloads demonstrates unbounded row growth in the `attachments` table.

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

**File:** stackslib/src/net/atlas/download.rs (L1073-1079)
```rust
impl AttachmentRequest {
    pub fn get_most_reliable_source(&self) -> (&UrlString, &ReliabilityReport) {
        self.sources
            .iter()
            .max_by_key(|(_, v)| v.score())
            .expect("Atlas: trying to select an Url out of an empty set")
    }
```

**File:** stackslib/src/net/atlas/download.rs (L1104-1119)
```rust
impl Requestable for AttachmentRequest {
    fn get_url(&self) -> &UrlString {
        let (url, _) = self.get_most_reliable_source();
        url
    }

    fn make_request_type(&self, peer_host: PeerHost) -> StacksHttpRequest {
        StacksHttpRequest::new_for_peer(
            peer_host,
            "GET".to_string(),
            format!("/v2/attachments/{}", &self.content_hash),
            HttpRequestContents::new(),
        )
        .expect("FATAL: failed to create an HTTP request for infallible data")
    }
}
```

**File:** stackslib/src/net/atlas/mod.rs (L52-52)
```rust
const ATTACHMENTS_MAX_SIZE_MIN: u32 = 1_048_576;
```

**File:** stackslib/src/net/atlas/db.rs (L549-560)
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
