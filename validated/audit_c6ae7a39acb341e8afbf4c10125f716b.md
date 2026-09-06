### Title
`AttachmentsBatchStateContext::extend_with_attachments` inserts an attacker-served attachment without verifying its hash matches the requested `content_hash` - ([File: stackslib/src/net/atlas/download.rs])

### Summary
`extend_with_attachments` decodes the peer's HTTP response into an `Attachment` and inserts it into `self.attachments` without ever comparing `response.attachment.hash()` against `request.content_hash` (the hash that was actually requested). This lets a malicious peer serve an arbitrary attachment `Y` in reply to a request for `X`, which is then persisted via `AtlasDB::insert_instantiated_attachment`.

### Finding Description
The claimed equality — "`Attachment.hash()` served for `request.content_hash` == `request.content_hash`" — is never enforced in code.

In `extend_with_attachments` [1](#0-0) , for each `(request, response)` pair from `results.succeeded`, the code does:
```
if let Ok(response) = response.decode_atlas_get_attachment() {
    self.attachments.insert(response.attachment);
    report.bump_successful_requests();
}
```
`request.content_hash` (the hash that this specific `AttachmentRequest` was constructed for, set in `get_prioritized_attachments_requests`, [2](#0-1) ) is completely ignored — there is no `if response.attachment.hash() == request.content_hash` check anywhere in this function.

The decode path itself does not validate the hash either: `decode_atlas_get_attachment` [3](#0-2)  just parses the JSON body into a `GetAttachmentResponse { attachment }` with no hash check against the URL path that was requested.

Downstream, `AttachmentsDownloader::run` drains `context.attachments` and, for every attachment `Y` obtained this way, calls `AtlasDB::insert_instantiated_attachment(&attachment)` [4](#0-3) , which persists it into the `attachments` table keyed by `attachment.hash()` [5](#0-4) , and resolves the batch using `attachment.hash()` (i.e., `hash(Y)`), not the originally requested `X`.

An unprivileged remote peer running the `/v2/attachments/inv` and `/v2/attachments/:hash` RPC handler on its own node can respond to a request for `/v2/attachments/X` with a 200 JSON body containing any arbitrary attachment content whose hash is `Y != X` (e.g. content it never legitimately holds/serves for `X`). The requesting node will decode this into `Attachment{content: ..., hash()=Y}` and unconditionally insert it.

### Impact Explanation
This causes the node to write `insert_instantiated_attachment` for a `Y`-hashed blob into its local `attachments` table even though no `AttachmentInstance`/name operation on-chain ever referenced `Y` as a `content_hash`. Specifically:
- `UPDATE attachment_instances SET is_available = 1 WHERE content_hash = ?1` will simply find no matching row for `Y` (since no instance references it), so no `attachment_instances` row gets falsely marked resolved via this vector alone — but the raw content blob for `Y` is stored, consuming storage.
- The `attachments_batch.resolve_attachment(&attachment.hash())` call marks the position keyed by `hash(Y)`, not `X`, so `X`'s missing entry is never resolved by this insertion (as the question's hypothesis intended), meaning `X` continues to be retried while unrelated attacker-supplied bytes accumulate in the database under `Y`.
- Repeatable per request: any malicious peer selected as a download source (chosen because it merely claimed via an inventory response to have the page bit set) can do this on every attachment request routed to it, filling the attachments table with garbage content and wasting bandwidth/storage. This matches the "attachment/BNS mismatch" bucket of High severity described in the audit's impact list, since content unconnected to any committed name operation is durably stored as if it were requested/relevant data.

### Likelihood Explanation
- Attacker precondition: control an outbound-reachable peer node that the victim treats as a sync peer with a data URL (`network.get_outbound_sync_peers()` / `get_data_url`) — this is achievable by any unprivileged node operator running the standard P2P/RPC stack, no secret or special role needed.
- The attacker only needs to (a) report having the target page/attachment index in its `/v2/attachments/inv` inventory response so it gets selected as a `source` in `get_prioritized_attachments_requests` [6](#0-5) , and (b) return a normal 200 response body from its own `/v2/attachments/:hash` endpoint with attacker-chosen content.
- Cost is minimal — one crafted HTTP response — and it is fully repeatable across every batch cycle/retry.

### Recommendation
In `extend_with_attachments`, after `decode_atlas_get_attachment()` succeeds, verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; otherwise treat it as a failed/faulty response (`report.bump_failed_requests()`, and optionally mark the peer/event as faulty for deregistration), mirroring how `AtlasDB::insert_uninstantiated_attachment` / instance verification are supposed to gate on the on-chain-committed hash.

### Proof of Concept
Add a test in `stackslib/src/net/atlas/tests.rs` alongside `test_downloader_context_attachment_requests`:
1. Build an `AttachmentRequest { content_hash: X, .. }` (from `new_attachment_from("facadeAA")`, hash = X).
2. Build a `StacksHttpResponse` (200 OK, JSON) whose body encodes `GetAttachmentResponse { attachment: new_attachment_from("facadeBB") }` (content hashing to `Y != X`).
3. Insert `(request, Some(response))` into `BatchedRequestsResult::succeeded`.
4. Call `context.extend_with_attachments(&mut results)`.
5. Assert: `context.attachments.iter().any(|a| a.hash() == Y)` is `true` while no attachment with hash `X` is present — proving an attachment never matching the requested `content_hash` was accepted into the resolved set, confirming the missing equality check at [7](#0-6) .

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

**File:** stackslib/src/net/atlas/download.rs (L434-459)
```rust
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

**File:** stackslib/src/net/api/getattachment.rs (L158-165)
```rust
impl StacksHttpResponse {
    pub fn decode_atlas_get_attachment(self) -> Result<GetAttachmentResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: GetAttachmentResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
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
