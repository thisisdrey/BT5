### Title
Attachment inventory bit is trusted without byte-level verification, letting a lying peer steer AttachmentRequest sourcing and stall BNS attachment resolution - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::get_prioritized_attachments_requests` (download.rs:404-478) builds the `sources` map for an `AttachmentRequest` purely from the `inventory` bitvector reported in a peer's `GetAttachmentsInvResponse`, with no verification that the peer's advertised bit actually corresponds to bytes hashing to `content_hash`. A remote peer answering `GET /v2/attachments/inv` can set `inventory[position] = 1` for a page/slot it has no real data for, causing the downloader to select it as a (or the only) source for that attachment.

### Finding Description
In `get_prioritized_attachments_requests` (download.rs:434-459), for each candidate peer the code does:
```
let has_attachment = search_page
    .and_then(|search_page| search_page.inventory.get(position_in_page as usize))
    .map(|result| *result == 1)
    .unwrap_or(false);
``` [1](#0-0) 
If `has_attachment` is true, the peer is unconditionally inserted into `sources` using only its self-reported `ReliabilityReport`, with no check against `content_hash` or any other corroborating evidence [2](#0-1) . The `AttachmentRequest` is then dispatched to peer(s) in `sources` exclusively based on this unverified claim [3](#0-2) .

When the response to the actual attachment fetch arrives, `extend_with_attachments` (download.rs:530-558) decodes it and inserts whatever `Attachment` bytes were returned into `self.attachments`, then later, in `AttachmentsDownloader::run`, the batch is resolved via `context.attachments_batch.resolve_attachment(&attachment.hash())` — i.e., resolution is keyed off the hash of the bytes actually received, not the originally requested `content_hash` [4](#0-3) . This means a lying peer's malformed/mismatched bytes will not falsely resolve the target attachment (no forged data is accepted as canonical), but it also means the *only* consequence of a false inventory bit is wasted round-trips: the peer either fails to respond (404 → `bump_failed_requests`, `extend_with_attachments` lines 540-545) or sends bytes that don't hash to the needed `content_hash` (silently ignored, since resolution requires an exact hash match against the batch's tracked missing attachments).

If the lying peer is the sole source in `sources` for that content_hash (e.g., a node's only outbound Atlas-capable peer, or the peer that happens to have the best/only inventory claim for a given page), the attachment instance for that on-chain commitment never resolves through this peer. The batch is re-queued up to `max_attachment_retry_count` (download.rs:188-205) and then permanently dropped, with no fallback logic in this code path to independently discover other peers holding the true data beyond the already-known peer set for that round.

### Impact Explanation
No forged attachment is stored or served as canonical — `resolve_attachment` is keyed by the hash of the actually-received bytes, so a mismatched payload from the lying peer cannot masquerade as the true attachment. The concrete effect is repeated failed round-trips and, if the malicious peer is the attacker-controlled sole source for that content_hash, eventual permanent abandonment of that attachment batch after `max_attachment_retry_count` retries. This degrades BNS zonefile/attachment resolution availability for names whose commitment happens to be sourced solely from the malicious peer, but it does not cause the node to accept/relay forged state, nor does it crash the node or write unauthorized state. This is a bounded availability/reliability degradation limited to attachments for which the attacker is the (only) advertised source — not a network-wide or state-corrupting issue.

### Likelihood Explanation
Requires the attacker to be an outbound sync peer of the victim node with a reachable data URL (`network.get_outbound_sync_peers()` / `get_data_url`) and to be selected as a source — most impactful when it is the *sole* peer claiming the attachment in its inventory. This is a plausible but non-trivial precondition (single-peer or low-diversity topologies), and the attack is fully repeatable per attachment/page the attacker chooses to lie about.

### Recommendation
Treat inventory advertisement as a hint only. After downloading attachment bytes, always verify `Attachment::hash() == content_hash` before accepting (already implicitly enforced by keying resolution on the received hash) and, additionally, penalize peers whose advertised inventory bit does not correspond to a byte-verified match by bumping their reliability failure count (currently a hash mismatch is silently dropped without a `bump_failed_requests` call), so mendacious peers are demoted and excluded from being sole/repeated sources. Also consider retaining/soliciting alternative peer inventories across retries rather than relying solely on the peer set gathered before batch initialization.

### Proof of Concept
Add a test in `stackslib/src/net/atlas/tests.rs` modeled on existing `AttachmentsBatchStateContext` tests:
1. Construct an `AttachmentsBatch` with one missing attachment instance `(index, content_hash)`.
2. Build a `AttachmentsBatchStateContext` with a single peer `url_a` and its `ReliabilityReport`.
3. Populate `context.inventories` with a `GetAttachmentsInvResponse` from `url_a` whose `AttachmentPage.inventory[position_in_page] = 1` for the relevant page, without any backing `Attachment` data.
4. Call `context.get_prioritized_attachments_requests()` and assert the resulting `AttachmentRequest.sources` contains only `url_a`.
5. Simulate `extend_with_attachments` with a `BatchedRequestsResult` where `url_a`'s request either times out (`succeeded.insert(request, None)`) or returns an `Attachment` whose `hash()` != `content_hash`.
6. Assert `attachments_batch.has_fully_succeed()` remains false and that after `max_attachment_retry_count` iterations of `AttachmentsDownloader::run`, the batch is dropped (not re-enqueued), while `resolved_attachments` never contains the entry for `content_hash`.

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

**File:** stackslib/src/net/atlas/download.rs (L439-444)
```rust
                    let has_attachment = search_page
                        .and_then(|search_page| {
                            search_page.inventory.get(position_in_page as usize)
                        })
                        .map(|result| *result == 1)
                        .unwrap_or(false);
```

**File:** stackslib/src/net/atlas/download.rs (L454-459)
```rust
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
