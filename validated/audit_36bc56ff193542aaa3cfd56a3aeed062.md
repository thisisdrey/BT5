### Title
Unauthenticated remote peer can force unbounded, uncommitted `attachments` DB writes via `AttachmentsDownloader::run` - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` accepts any successfully-decoded `GetAttachmentResponse` from a peer without ever checking that the returned attachment's hash matches the `content_hash` that was actually requested. `AttachmentsDownloader::run`'s `Done` branch then unconditionally calls `network.atlasdb.insert_instantiated_attachment(&attachment)` for every attachment collected, regardless of whether `find_all_attachment_instances(&attachment.hash())` found any matching on-chain-committed instance.

### Finding Description
The claimed equality — "attachment hash stored == an on-chain `AttachmentInstance.content_hash`" — is never enforced before the row is written:

- In `extend_with_attachments` [1](#0-0) , each successfully parsed HTTP response is trusted and inserted into `self.attachments` purely based on `decode_atlas_get_attachment()` succeeding; there is no comparison between `request.content_hash` (the hash that was actually queued, see `AttachmentRequest.content_hash` at [2](#0-1) ) and `response.attachment.hash()`.
- In `run()`'s `Done` branch, the code looks up `find_all_attachment_instances(&attachment.hash())` but does not gate the write on the result being non-empty — `insert_instantiated_attachment(&attachment)` runs unconditionally for every attachment in `context.attachments`: [3](#0-2) .

Because a malicious peer controls the content of every `GET /v2/attachments/{hash}` response it serves (it is chosen from `network.get_outbound_sync_peers()`), it can return arbitrary, never-committed byte blobs for every requested hash. Each such blob is added to `context.attachments` as a distinct `Attachment` (since attachment identity/hash is derived from its content), and each is unconditionally persisted via `insert_instantiated_attachment`, producing one permanent, "instantiated" DB row per malicious response — with no check that any `AttachmentInstance.content_hash` on chain ever referenced that content.

### Impact Explanation
A single malicious/compromised outbound sync peer that answers attachment-content requests can force the victim node to persist unlimited garbage rows into the Atlas `attachments` SQLite table, marked `was_instantiated=1`. This is an unauthenticated write to persistent node state performed purely by responding to legitimate outbound HTTP requests — no signature, secret, or on-chain commitment is checked. Unlike uninstantiated attachments, which are periodically evicted via `evict_expired_uninstantiated_attachments()` (called right after the insert loop, [4](#0-3) ), there is no corresponding eviction call for instantiated attachments in this code path, so the injected rows accumulate indefinitely, causing unbounded disk growth on the victim node. This matches the "Critical - unauthenticated write to state" category.

### Likelihood Explanation
Preconditions are modest and attacker-achievable: the node must have queued at least one `AttachmentInstance` for download (normal operation whenever new subdomain/BNS-zonefile attachments are announced on-chain) and the attacker must be one of the peers in `network.get_outbound_sync_peers()` that the node happens to query for that batch — attainable by simply running a reachable Stacks P2P node that peers with the victim and advertises the requested attachments in its inventory response. No RPC secret, signer key, or admin role is needed; the attacker only serves HTTP responses to requests the victim node itself initiates. The attack is fully repeatable — the attacker can serve a fresh random blob for every future poll of `check_queued_attachment_instances`/batch retries, each producing a new row (attachment identity depends on content, so identical requests can yield distinct stored blobs each time).

### Recommendation
In `extend_with_attachments`, verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; drop and treat as a faulty-peer response otherwise. Additionally, in `run()`'s `Done` branch, only call `insert_instantiated_attachment` when `find_all_attachment_instances(&attachment.hash())` returns a non-empty result (i.e., an on-chain-committed instance actually exists for that hash), mirroring the same guard used in `check_attachment_instances`/`find_uninstantiated_attachment` paths in `AttachmentsDownloader`.

### Proof of Concept
Add a test in `stackslib/src/net/atlas/tests.rs` or `download.rs`'s test module that:
1. Constructs an `AttachmentsBatchStateContext` with a single peer URL and a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map contains N `AttachmentRequest`s (each with a distinct, legitimate `content_hash`) paired with `StacksHttpResponse`s that decode via `decode_atlas_get_attachment()` into `Attachment` blobs of **random bytes not equal to the requested `content_hash`'s pre-image**.
2. Call `extend_with_attachments` and assert `context.attachments.len() == N` even though none of the attachments' `.hash()` match any requested `content_hash`.
3. Drive `AttachmentsBatchStateMachine::Done` through `AttachmentsDownloader::run`, then query `network.atlasdb` (e.g. via `count_attachments`/direct SQL `SELECT COUNT(*) FROM attachments WHERE was_instantiated=1`) and assert the row count equals N, while `find_all_attachment_instances(&attachment.hash())` returns empty for each — demonstrating unauthenticated, uncommitted writes accumulate with no corresponding `AttachmentInstance`.

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

**File:** stackslib/src/net/atlas/download.rs (L174-176)
```rust
                // Every once in a while, we delete uninstantiated attachments
                network.atlasdb.evict_expired_uninstantiated_attachments()?;

```

**File:** stackslib/src/net/atlas/download.rs (L464-475)
```rust
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
