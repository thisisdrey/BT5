### Title
`AtlasDB::find_attachment` serves attacker-planted content with no confirming `AttachmentInstance`/BNS commitment - ([File: stackslib/src/net/atlas/db.rs], [File: stackslib/src/net/api/getattachment.rs])

### Summary
`RPCGetAttachmentRequestHandler::try_handle_request` resolves `GET /v2/attachments/{hash}` purely via `AtlasDB::find_attachment`, which matches on `hash = ?1 AND was_instantiated = 1` with no join against `attachment_instances`. Because Atlas's attachment-download pipeline (`extend_with_attachments`) accepts a peer's attachment response without checking it matches the requested `content_hash`, a malicious Atlas peer can seed the local `attachments` table with arbitrary content under a hash for which no name-operation `AttachmentInstance` ever existed, and any unrelated remote client can then retrieve that content as if it were validated BNS-committed data.

### Finding Description
The read path is: [1](#0-0) 

`find_attachment` only checks `was_instantiated = 1` on the `attachments` table, never joining to `attachment_instances`: [2](#0-1) 

The poisoning path is in the Atlas attachment-download state machine. When a batch of attachment fetches completes, `extend_with_attachments` decodes each peer's HTTP response and inserts the returned `Attachment` into a `HashSet<Attachment>` with **no check that the returned content's hash equals the `content_hash` that was actually requested** (`AttachmentRequest::content_hash`, set from the local `AttachmentsBatch`): [3](#0-2) [4](#0-3) 

Once in the `Done` state, every drained attachment is persisted with `insert_instantiated_attachment(&attachment)`, which is content-addressed (it computes `attachment.hash()` itself, i.e. hashes the bytes the attacker sent) and always sets `was_instantiated = 1`: [5](#0-4) [6](#0-5) 

Because a malicious peer fully controls the bytes it returns for an `AttachmentRequest`, it also fully controls the resulting hash `Y = hash(garbage)`. Since `extend_with_attachments` never verifies the response's hash against the originally requested `content_hash`, the attacker can return content unrelated to what was asked for; that content is stored under its own real hash `Y`, for which no `attachment_instance` row (i.e., no confirmed name-operation commitment) exists — `find_all_attachment_instances(Y)` returns empty. Yet `find_attachment(Y)` still returns the content because it only checks `was_instantiated = 1`. `try_handle_request` then returns `200 OK` with `GetAttachmentResponse{ attachment }` containing this uncommitted content.

The existing guards (the query filter on `was_instantiated`) do not check the equality that the endpoint's semantics require: that the served bytes for hash `Y` correspond to an on-chain-committed BNS/name attachment reference. No signature, secret, or admission check exists to prevent this, and the attacker only needs to be a legitimate Atlas-participating peer answering `AttachmentRequest`s during normal Atlas sync — no privileged role or secret required.

### Impact Explanation
Any remote, unauthenticated client querying `/v2/attachments/{Y}` on the poisoned node receives a `200 OK` with attacker-chosen bytes that were never referenced by any confirmed BNS/name-registration attachment instance, i.e., the node serves non-canonical/uncommitted state as if it were validated Atlas/BNS data. This matches the specified High-severity category ("serving non-canonical state as canonical / attachment/BNS mismatch"). The attack is repeatable for arbitrarily many distinct hashes and pollutes the node's attachment store, degrading trust in the node's BNS-adjacent API for any downstream consumer.

### Likelihood Explanation
Preconditions: the target node must be running Atlas attachment sync and receiving `AttachmentRequest` responses from a peer the attacker controls (a normal, unprivileged role reachable by any P2P participant); the attacker needs no RPC secret, no signer/slot ownership, and no admin role. The attacker's cost is a single crafted HTTP response during an ordinary Atlas attachment-fetch round trip. This is fully reproducible per hash and requires no elevated timing or race conditions.

### Recommendation
1. In `AttachmentsBatchStateContext::extend_with_attachments` (`stackslib/src/net/atlas/download.rs`), verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; discard and penalize (`report.bump_failed_requests()`) any mismatched response.
2. Defense in depth: change `AtlasDB::find_attachment` (`stackslib/src/net/atlas/db.rs`) to require joining against `attachment_instances` with `status = Checked`/`is_available = 1` for the same hash before serving via `/v2/attachments/{hash}`, so `RPCGetAttachmentRequestHandler` never returns content lacking a confirmed name-operation commitment.

### Proof of Concept
Rust test in `stackslib/src/net/api/tests/getattachment.rs`-style harness:
1. Construct an `AtlasDB` (test peer) and directly call `insert_instantiated_attachment(&garbage_attachment)` with attacker-chosen bytes, simulating the accepted-mismatch path from `extend_with_attachments` (skip the full download-FSM plumbing; this isolates the exact effect of the missing hash check).
2. Confirm precondition: `atlas_db.find_all_attachment_instances(&garbage_attachment.hash()).unwrap()` is empty (no committing name operation exists for this hash).
3. Build `StacksHttpRequest::new_getattachment(addr, garbage_attachment.hash())` and drive it through `RPCGetAttachmentRequestHandler::try_handle_request` (via the existing `test_rpc` harness used in `test_try_make_response`).
4. Assert the response status is `200`, and `response.decode_atlas_get_attachment().unwrap().attachment == garbage_attachment`, demonstrating the endpoint serves uncommitted, attacker-planted content as valid Atlas/BNS attachment data.

### Citations

**File:** stackslib/src/net/api/getattachment.rs (L104-118)
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

**File:** stackslib/src/net/atlas/db.rs (L641-648)
```rust
    pub fn find_attachment(&self, content_hash: &Hash160) -> Result<Option<Attachment>, db_error> {
        let hex_content_hash = to_hex(&content_hash.0[..]);
        let qry = "SELECT content, hash FROM attachments WHERE hash = ?1 AND was_instantiated = 1"
            .to_string();
        let args = params![hex_content_hash];
        let row = query_row::<Attachment, _>(&self.conn, &qry, args)?;
        Ok(row)
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
