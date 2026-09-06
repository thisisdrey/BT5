### Title
Unvalidated attachment content accepted from any download-peer response, allowing arbitrary-hash writes into the Atlas `attachments` table - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`AttachmentRequest` is built with an expected `content_hash` [1](#0-0) , but when the response arrives, `extend_with_attachments` decodes the `GetAttachmentResponse` and inserts `response.attachment` into the `attachments` set with no check that `response.attachment.hash() == request.content_hash` [2](#0-1) . Later, `run()` unconditionally calls `insert_instantiated_attachment(&attachment)` for every attachment in that set — regardless of whether `find_all_attachment_instances(&attachment.hash())` returns any confirmed `AttachmentInstance` match [3](#0-2) .

### Finding Description
The intended invariant is: every hash key stored in the `attachments` table (`was_instantiated=1`) should correspond to a `content_hash` that appeared in a confirmed on-chain `AttachmentInstance`. This invariant is broken here: any peer selected as an outbound sync source for a legitimate, chain-derived `AttachmentRequest` (for some real pending `content_hash` X) can respond with a `GetAttachmentResponse{attachment}` body containing **arbitrary attacker-chosen content** Y, where `Hash160::from_data(Y) != X`.

`extend_with_attachments` accepts any successfully-decoded response and inserts it into `self.attachments: HashSet<Attachment>` without comparing the decoded attachment's hash against the `AttachmentRequest.content_hash` that was actually sent [4](#0-3) . In `AttachmentsDownloader::run()`, for every attachment drained from that set, the code computes `attachment.hash()` (i.e., re-derives the hash from the attacker's own bytes, not X), looks up matching instances via `find_all_attachment_instances(&attachment.hash())`, and then — independent of whether any instance matched — calls `network.atlasdb.insert_instantiated_attachment(&attachment)` [5](#0-4) . `insert_instantiated_attachment` performs an unconditional `INSERT OR REPLACE INTO attachments ... (hash, content, was_instantiated=1, ...)` keyed by `attachment.hash()` [6](#0-5) , with no size cap enforcement (`should_keep_attachment`'s size/contract checks are not invoked in this path) and no eviction policy for `was_instantiated=1` rows (only the `was_instantiated=0` table has `evict_k_oldest_uninstantiated_attachments`/`max_uninstantiated_attachments` bounds) [7](#0-6) .

Thus, the real pending instance for X remains unresolved (since `attachment.hash() != X`, so no `AttachmentInstance` match is found and it stays unavailable/queued for retry), while a permanent, attacker-controlled, arbitrary-hash junk row is written into the node's local Atlas SQLite database — a hash that was never referenced by any `AttachmentInstance` derived from a validated block, exactly the equality break identified in the question.

`getattachment.rs`'s `RPCGetAttachmentRequestHandler::try_handle_request` itself is read-only (only calls `find_attachment`) [8](#0-7)  and does not perform any insert — the actual write path is entirely in `download.rs`'s peer-response handling, confirming the write occurs on the client (requester) side when parsing a malicious peer's HTTP response to its own outbound `GET /v2/attachments/{hash}` request.

### Impact Explanation
A malicious/attacker-controlled Atlas download peer can cause the victim node to persist arbitrary attacker-chosen blobs (bounded only by whatever body-size limits the HTTP/JSON layer enforces, not by Atlas's own `attachments_max_size` check) into its local `attachments` table under hash keys of the attacker's choosing, with no relation to any confirmed on-chain `AttachmentInstance`. This is an unauthenticated write to local state and unbounded storage growth (no eviction for `was_instantiated=1` rows), matching the Critical "unauthenticated/unauthorized write to state" / storage-exhaustion category. It also causes legitimate attachment resolution for the real pending `content_hash` to stall/retry indefinitely as long as the malicious peer keeps responding with mismatched content, since the real instance is never resolved.

### Likelihood Explanation
The attacker only needs to be selected as one of the outbound sync peers for the Atlas downloader (`network.get_outbound_sync_peers()`), report having the missing page in its `AttachmentsInventoryRequest` response (attacker-controlled, since inventory content itself is not independently verified before requesting), and then answer the resulting `AttachmentRequest` with a syntactically valid `GetAttachmentResponse` containing arbitrary content. This requires only unprivileged peer connectivity (no RPC secret, no signer/slot key) and is repeatable on every batch cycle for every distinct pending instance the node is trying to resolve — the attacker's cost is a single crafted HTTP JSON response per request.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments`, verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; discard and mark the peer's reliability report as failed otherwise. Additionally, enforce `should_keep_attachment`-style size/contract checks in the `run()` insertion path, and only call `insert_instantiated_attachment` when at least one matching `AttachmentInstance` was found (`!attachments_instances.is_empty()`), otherwise treat the response as invalid/failed rather than persisting it.

### Proof of Concept
Rust test plan (in `stackslib/src/net/atlas/tests.rs` or a new integration test):
1. Construct an `AttachmentsBatchStateContext` with one `AttachmentRequest{content_hash: X, ...}` in `attachments_batch.attachments_instances`.
2. Simulate `BatchedRequestsResult::succeeded` containing that request mapped to a crafted `StacksHttpResponse` whose JSON body is `GetAttachmentResponse{attachment: Attachment{content: b"attacker_junk".to_vec()}}` (so `attachment.hash() = Hash160::from_data(b"attacker_junk") != X`).
3. Call `context.extend_with_attachments(&mut results)` and assert `context.attachments` contains the attacker's `Attachment` despite `attachment.hash() != X`.
4. Drive `AttachmentsDownloader::run()` (or directly replicate its `Done` arm logic) and assert `atlas_db.find_attachment(&Hash160::from_data(b"attacker_junk")).unwrap().is_some()` — proving a row was inserted under a hash never present in any `AttachmentInstance` — while `atlas_db.find_attachment(&X).unwrap().is_none()` remains true (the real content_hash never got resolved).

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

**File:** stackslib/src/net/atlas/download.rs (L466-474)
```rust
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

**File:** stackslib/src/net/api/getattachment.rs (L104-119)
```rust
        let attachment_res = node.with_node_state(
            |network, _sortdb, _chainstate, _mempool, _rpc_args| match network
                .get_atlasdb()
                .find_attachment(&attachment_hash)
            {
                Ok(Some(attachment)) => Ok(GetAttachmentResponse { attachment }),
                _ => {
                    let msg = "Unable to find attachment".to_string();
                    warn!("{msg}");
                    Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpNotFound::new(msg),
                    ))
                }
            },
        );
```
