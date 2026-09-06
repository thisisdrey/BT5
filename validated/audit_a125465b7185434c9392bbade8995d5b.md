Based on the code I reviewed, `extend_with_attachments` in `stackslib/src/net/atlas/download.rs` decodes the peer's HTTP response and inserts the returned `Attachment` directly into `self.attachments` with no comparison against the `AttachmentRequest.content_hash` that was used to select and request that peer in the first place.

### Title
Remote peer can serve arbitrary attachment bytes that don't match the requested `content_hash`, polluting AtlasDB storage - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` accepts and stores any `Attachment` returned by a peer for an `AttachmentRequest`, without verifying that `Hash160::from_data(&attachment.content) == request.content_hash`. A malicious peer selected via `get_prioritized_attachments_requests` can therefore return attachment bytes that do not hash to the committed `content_hash`, and those bytes get inserted into `self.attachments` and eventually written to the AtlasDB.

### Finding Description
The loop at [1](#0-0)  drains `results.succeeded`, decodes the response via `response.decode_atlas_get_attachment()`, and on success does `self.attachments.insert(response.attachment)` — there is no check that `response.attachment.hash() == request.content_hash`. Compare this to `check_attachment_instances` in the same file, which looks up attachments strictly by content hash key (`atlas_db.find_attachment(&attachment_instance.content_hash)`), implying the invariant that attachments are supposed to be indexed/validated by hash, but that invariant is never enforced at the point the attachment bytes are received from the network in `extend_with_attachments`.

The request that solicits the data, `AttachmentRequest`, is built in `get_prioritized_attachments_requests` at [2](#0-1)  and carries the expected `content_hash` — a value derived from a confirmed name operation. Any of the peers in `sources` (selected purely because their self-reported inventory bitmap claims to have the attachment, with no cryptographic proof) can serve back arbitrary bytes for that request, and those bytes are accepted as long as the response decodes into the expected wire format (`GetAttachmentResponse`).

Downstream, in `AttachmentsDownloader::run`, `context.attachments.drain()` iterates the (potentially mismatched) attachments and calls `network.atlasdb.insert_instantiated_attachment(&attachment)` at [3](#0-2)  — persisting the unverified blob into the `attachments` table keyed presumably by its own (self-consistent) hash, not tied to any actually-committed name-op hash. Because the attachment is looked up in the DB by `attachment.hash()` (line 157: `find_all_attachment_instances(&attachment.hash())`), a mismatched attachment simply fails to pair with any `AttachmentInstance` — it doesn't get "confirmed" as resolving the pending instance, but it is still permanently stored in the DB via `insert_instantiated_attachment`, and `context.attachments_batch.resolve_attachment(&attachment.hash())` is called using the wrong (served) hash rather than the original requested hash, which can incorrectly mark the batch's original target hash as unresolved forever while occupying DB space with a value nobody ever committed to on-chain.

### Impact Explanation
A remote, unprivileged peer that legitimately runs its own P2P node (or is otherwise selected as an outbound sync peer) can, for every attachment inventory entry it falsely advertises as being present, serve arbitrary bytes whose hash does not match the committed `content_hash`. Each such response causes one bogus `Attachment` row to be permanently inserted into the AtlasDB `attachments` table, since there is no code path that ever re-validates or garbage-collects instantiated attachments against their claimed hash after insertion. This matches the "attachment/BNS mismatch" / "serving non-canonical state as canonical" categories in the High severity bucket — the node's persistent Atlas storage now contains a blob that no confirmed name-operation actually committed to.

### Likelihood Explanation
Preconditions are minimal: the attacker only needs to be selected as one of `network.get_outbound_sync_peers()` (i.e., run/operate a normal reachable peer) and get its inventory response counted as having the attachment page bit set for the target index, which requires no proof-of-possession — the bitmap value is trusted at face value in `get_prioritized_attachments_requests`. From there, every subsequent legitimate attachment fetch attempt for a hash the attacker chooses to falsely advertise can be answered with garbage, repeatable per attachment/retry cycle, with essentially zero attacker cost and no privileged credentials.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments`, after decoding the response, compute `Hash160::from_data(&response.attachment.content)` and compare it against `request.content_hash` before inserting into `self.attachments`; on mismatch, treat it as a failed request (`report.bump_failed_requests()`) and optionally penalize/deregister the offending peer, mirroring the same by-hash validation already performed when locally-known attachments are looked up in `check_attachment_instances`.

### Proof of Concept
Add a unit test in `stackslib/src/net/atlas/download.rs` (or a new test in `stackslib/src/net/atlas/tests`) that:
1. Constructs an `AttachmentsBatchStateContext` via `AttachmentsBatchStateContext::new` with a dummy `AttachmentsBatch`, a `peers` map containing one `UrlString` with a `ReliabilityReport`, and default `ConnectionOptions`.
2. Builds an `AttachmentRequest` with `content_hash = Hash160::from_data(b"expected")`.
3. Builds a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map has that request keyed to `Some(StacksHttpResponse)` wrapping a `GetAttachmentResponse { attachment: Attachment { content: b"different-bytes".to_vec() } }` (constructed directly, bypassing the wire decode, or by crafting the raw HTTP body that `decode_atlas_get_attachment` parses).
4. Calls `context.extend_with_attachments(&mut results)`.
5. Asserts: `context.attachments.contains(&Attachment { content: b"different-bytes".to_vec() })` is `true`, while `Hash160::from_data(b"different-bytes") != request.content_hash` — demonstrating the mismatched attachment is stored despite hashing to a different value than what was requested/committed.

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

**File:** stackslib/src/net/atlas/download.rs (L467-474)
```rust
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
