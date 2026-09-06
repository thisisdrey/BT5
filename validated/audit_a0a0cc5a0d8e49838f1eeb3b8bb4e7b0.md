### Title
Unbounded storage growth via unverified `AttachmentRequest` responses bypassing `insert_instantiated_attachment`'s missing content-hash check - ([File: stackslib/src/net/atlas/download.rs])

### Summary
In `AttachmentsBatchStateContext::extend_with_attachments`, any peer's `GetAttachmentResponse` returned for an `AttachmentRequest` is accepted and stored via `AtlasDB::insert_instantiated_attachment` in `run_scan`'s drain loop, without ever checking that the attachment's actual hash matches the `content_hash` that was requested/queued via `AtlasDB::queue_attachment_instance`. This allows a malicious peer to make the node permanently store arbitrary blobs that are not backed by any on-chain-confirmed `AttachmentInstance`, bypassing the `max_uninstantiated_attachments` cap that only applies to `insert_uninstantiated_attachment`.

### Finding Description
The equality that should hold is: *every row inserted into the `attachments` table via `insert_instantiated_attachment` must correspond to a `content_hash` that some queued, on-chain-derived `AttachmentInstance` actually committed to*. This is never enforced.

The flow:
1. `run_scan` drives `AttachmentsBatchStateMachine` through DNS lookup, inventory download, and attachment download states, ending in `extend_with_attachments` at [1](#0-0) , which for each successfully-decoded response simply does `self.attachments.insert(response.attachment)` — no comparison of `response.attachment.hash()` against the `content_hash` the request was originally made for.
2. Back in `run_scan`'s `Done` branch, the code drains `context.attachments` and, **for every attachment regardless of whether any instance references it**, calls `insert_instantiated_attachment`: [2](#0-1) . The lookup via `find_all_attachment_instances(&attachment.hash())` is only used to decide which resolved instances to report back — its emptiness does not prevent the unconditional `insert_instantiated_attachment` call two lines below.
3. `AtlasDB::insert_instantiated_attachment` performs an unconditional `INSERT OR REPLACE INTO attachments ... VALUES (?, ?, 1, ...)` with no size cap check at all: [3](#0-2) . This is in stark contrast to `insert_uninstantiated_attachment`, which enforces `max_uninstantiated_attachments` via eviction before insert: [4](#0-3) .
4. On the HTTP response-decoding side, `decode_atlas_get_attachment` merely JSON-deserializes the response body into a `GetAttachmentResponse` with no hash validation against the request path's `attachment_hash`: [5](#0-4) .

So a malicious peer, upon receiving a `GET /v2/attachments/{hash}` request for a real, queued `content_hash`, can respond with an arbitrarily large/distinct blob whose true content hash does not match the requested hash. `find_all_attachment_instances` will return no rows for that mismatched hash, so nothing is "resolved" from the victim's perspective — but the attachment is stored anyway, permanently marked `was_instantiated = 1`, with no expiry and no cap, since `evict_expired_uninstantiated_attachments`/`evict_k_oldest_uninstantiated_attachments` only ever target rows with `was_instantiated = 0`.

### Impact Explanation
A remote, unprivileged peer that participates in Atlas attachment gossip (any peer the node syncs attachments from) can force unbounded growth of the local `attachments` SQLite table by repeatedly answering distinct queued `AttachmentRequest`s with unrelated payloads. Each such response consumes disk space permanently (no eviction path exists for `was_instantiated = 1` rows), and the attacker can generate an unlimited number of distinct request/response pairs as new attachment instances are queued from legitimate on-chain BNS activity, or simply by controlling what it serves per distinct requested hash. This is a storage-exhaustion / unbounded-disk-growth fault on the node's persisted state, matching the "High" bound: a low-cost path to unbounded resource consumption bypassing an explicit existing bound (`max_uninstantiated_attachments`) that the code clearly intended to enforce for this table.

### Likelihood Explanation
Preconditions are minimal: the attacker only needs to be selected as an outbound sync peer for Atlas attachment downloads (`network.get_outbound_sync_peers()`), which is a normal, unprivileged peer role reachable over the P2P/RPC HTTP surface (`GET /v2/attachments/:hash`). No secret, signature, or special role is required — the attacker just needs to answer `AttachmentRequest`s it receives with mismatched content. The behavior is fully repeatable per distinct queued `content_hash`; queued instances are created from real BNS activity via `AtlasDB::queue_attachment_instance`, giving the attacker a steady stream of legitimate hashes to answer with garbage.

### Recommendation
In `extend_with_attachments` (or immediately before calling `insert_instantiated_attachment` in `run_scan`), verify that the returned `attachment.hash()` equals the `content_hash` of the `AttachmentRequest` that produced it before accepting/storing it; discard and penalize the peer's reliability report otherwise. Additionally, `insert_instantiated_attachment` should only be called when `find_all_attachment_instances` (or equivalent verification) confirms the hash is backed by at least one known `AttachmentInstance`, and/or the `attachments` table should have its own size/age-based eviction for `was_instantiated = 1` rows independent of `max_uninstantiated_attachments`.

### Proof of Concept
Rust test plan in `stackslib::net::atlas::download` (or `db`):
1. Set up an `AtlasDB` with `AtlasConfig::default()` (small `max_uninstantiated_attachments`).
2. Construct an `AttachmentsBatchStateContext` with a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map contains N distinct `AttachmentRequest`s (each built from a distinct, legitimately-queued `content_hash`, mimicking real `queue_attachment_instance` calls), but whose HTTP responses' `GetAttachmentResponse.attachment` bodies are synthetic `Attachment` blobs whose actual `.hash()` does **not** match any queued `content_hash`.
3. Call `extend_with_attachments` then simulate the `run_scan` drain loop: for each attachment, call `atlasdb.find_all_attachment_instances(&attachment.hash())` (assert it returns empty) followed by `atlasdb.insert_instantiated_attachment(&attachment)`.
4. Assert that after N iterations, `SELECT COUNT(*) FROM attachments WHERE was_instantiated = 1` equals N even though N exceeds any configured cap and none of the rows are referenced by `attachment_instances`, and that no eviction call (`evict_expired_uninstantiated_attachments`, `evict_k_oldest_uninstantiated_attachments`) reduces this count since those only target `was_instantiated = 0` rows.

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

**File:** stackslib/src/net/api/getattachment.rs (L159-165)
```rust
    pub fn decode_atlas_get_attachment(self) -> Result<GetAttachmentResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: GetAttachmentResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
    }
```
