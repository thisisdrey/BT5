### Title
Malicious peer's forged `GetAttachmentResponse` content is unconditionally marked `was_instantiated=1`, permanently bypassing attachment eviction - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentsBatchStateMachine::extend_with_attachments` blindly trusts the content returned by a peer in a `GetAttachmentResponse` without checking it against the requested content hash, and the `Done()` arm of `AttachmentsDownloader::run` unconditionally calls `insert_instantiated_attachment` on that content regardless of whether any matching `AttachmentInstance` exists. This lets a malicious outbound peer plant arbitrary "instantiated" (permanent) rows in the local node's Atlas `attachments` table that are immune to `evict_expired_uninstantiated_attachments`.

### Finding Description
The intended invariant is: an `attachments` row with `was_instantiated=1` should only exist because its hash matches a `content_hash` in some on-chain-committed `AttachmentInstance` row. Tracing the download path:

1. When fetching queued/unresolved attachments, the node issues a `GET /v2/attachments/<hash>` request to a peer via `StacksHttpRequest::new_getattachment` [1](#0-0) .
2. The response is parsed with `decode_atlas_get_attachment`, which only JSON-decodes the body into a `GetAttachmentResponse` - it never verifies that `response.attachment.hash()` equals the hash that was requested [2](#0-1) .
3. `extend_with_attachments` takes this unchecked attachment and inserts it into `self.attachments` (a `HashSet<Attachment>`) purely based on the peer's claimed content, with no cross-check against the original request's target hash [3](#0-2) .
4. In the `Done()` arm of `AttachmentsDownloader::run`, for every attachment drained from that set, the code calls `find_all_attachment_instances(&attachment.hash())` to look up matching on-chain `AttachmentInstance` rows, but then **unconditionally** calls `network.atlasdb.insert_instantiated_attachment(&attachment)` regardless of whether `attachments_instances` is empty [4](#0-3) .
5. `insert_instantiated_attachment` writes the row with `was_instantiated=1` unconditionally [5](#0-4) .
6. Immediately after, `evict_expired_uninstantiated_attachments` and `evict_expired_unresolved_attachment_instances` only ever target rows where `was_instantiated = 0` or `is_available = 0` [6](#0-5) [7](#0-6) .

A malicious peer that the victim has selected as an outbound sync peer (any peer can become one, per the threat model) can respond to a legitimate `GetAttachment` request for hash `H` with **arbitrary garbage content `C`** whose real hash `hash(C) != H`. Because there is no hash-equality check, this content is accepted into `context.attachments`, looked up as `find_all_attachment_instances(&hash(C))` (which returns an empty vec since no `AttachmentInstance` was ever queued for `hash(C)`), and then is still inserted as `was_instantiated=1` permanently. This garbage row is now permanent, immune to expiry, and will be served back on `find_attachment` (used by `GET /v2/attachments/<hash>` and by other peers requesting the same content), matching the "attachment/BNS mismatch" category — serving content under a hash that no on-chain `AttachmentInstance` ever referenced as committed.

### Impact Explanation
A single malicious/misbehaving peer can, per response, permanently insert one garbage attachment row (bounded by `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST`/message-size caps on that content) into a victim's Atlas `attachments` table with `was_instantiated=1`. This:
- Breaks the "instantiated ⇒ on-chain committed" invariant, meaning the node will serve unauthenticated/non-canonical attachment data as if it were validated data (over `GET /v2/attachments/<hash>`), matching "attachment/BNS mismatch" under the High severity category.
- Is repeatable across many distinct garbage contents (each producing a distinct hash and thus a distinct permanent row), causing unbounded growth of the `attachments` table since the normal eviction mechanisms cannot reclaim `was_instantiated=1` rows.

### Likelihood Explanation
The attacker only needs to run their own peer node and be selected by the victim as an outbound sync peer serving Atlas data (`network.get_outbound_sync_peers()` / `get_data_url`), which is within the stated unprivileged-attacker threat model (no secret, no privileged role required). The victim must have at least one queued/unresolved `AttachmentInstance` to trigger a download request, which is a normal operating condition (BNS name registrations create these regularly). The attacker's cost is trivial: respond to a single HTTP GET with attacker-chosen JSON body content.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (or in `decode_atlas_get_attachment`), verify that `response.attachment.hash()` equals the `content_hash` that was actually requested (`AttachmentRequest`'s target hash) before accepting the attachment into `self.attachments`; drop/penalize the peer otherwise. Additionally, in the `Done()` arm, only call `insert_instantiated_attachment` when `find_all_attachment_instances` returns a non-empty result (i.e., there is a real matching, checked `AttachmentInstance`), otherwise route the mismatched content into the uninstantiated pool (`insert_uninstantiated_attachment`) so it remains subject to `evict_expired_uninstantiated_attachments`.

### Proof of Concept
Rust test plan (in `stackslib/src/net/atlas/tests.rs` or `download.rs` tests):
1. Construct an `AttachmentsBatchStateContext` and manually build a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map contains one `AttachmentRequest` for hash `H` paired with a crafted `StacksHttpResponse` whose JSON body is `GetAttachmentResponse { attachment: Attachment { content: vec![0xDE, 0xAD] } }` (so `hash(content) != H`, and no `AttachmentInstance` row with `content_hash = hash(content)` was ever queued).
2. Call `context.extend_with_attachments(&mut results)`, producing `AttachmentsBatchStateMachine::Done(context)`.
3. Drive the equivalent of the `Done()` arm logic directly against an `AtlasDB` fixture: call `atlas_db.find_all_attachment_instances(&attachment.hash())` (assert it returns `vec![]`), then `atlas_db.insert_instantiated_attachment(&attachment)`.
4. Call `atlas_db.evict_expired_uninstantiated_attachments()` after advancing the mock clock past `uninstantiated_attachments_expire_after`.
5. Assert `atlas_db.find_attachment(&attachment.hash())` (queries `was_instantiated = 1`) still returns `Some(attachment)`, proving the garbage row survived the expiry pass and is permanent — at `stackslib/src/net/atlas/db.rs:641-648` (`find_attachment`) versus the eviction at `stackslib/src/net/atlas/db.rs:549-560`.

### Citations

**File:** stackslib/src/net/api/getattachment.rs (L145-156)
```rust
impl StacksHttpRequest {
    /// Make a new request for an attachment
    pub fn new_getattachment(host: PeerHost, attachment_id: Hash160) -> StacksHttpRequest {
        StacksHttpRequest::new_for_peer(
            host,
            "GET".into(),
            format!("/v2/attachments/{}", &attachment_id),
            HttpRequestContents::new(),
        )
        .expect("FATAL: failed to construct request from infallible data")
    }
}
```

**File:** stackslib/src/net/api/getattachment.rs (L158-166)
```rust
impl StacksHttpResponse {
    pub fn decode_atlas_get_attachment(self) -> Result<GetAttachmentResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: GetAttachmentResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
    }
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

**File:** stackslib/src/net/atlas/db.rs (L606-620)
```rust
    pub fn evict_expired_unresolved_attachment_instances(&mut self) -> Result<(), db_error> {
        let now = util::get_epoch_time_secs() as i64;
        let cut_off = now
            - self
                .atlas_config
                .unresolved_attachment_instances_expire_after as i64;
        let tx = self.tx_begin()?;
        let res = tx.execute(
            "DELETE FROM attachment_instances WHERE is_available = 0 AND created_at < ?",
            params![cut_off],
        );
        res.map_err(db_error::SqliteError)?;
        tx.commit().map_err(db_error::SqliteError)?;
        Ok(())
    }
```
