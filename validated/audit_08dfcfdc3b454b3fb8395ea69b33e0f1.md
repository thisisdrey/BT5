### Title
Missing content-hash verification allows a malicious peer to serve arbitrary bytes as a requested Atlas attachment - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` decodes a peer's HTTP response into an `Attachment` and inserts it directly into `self.attachments` without ever comparing the resulting `Attachment::hash()` to the `content_hash` that was originally requested in the corresponding `AttachmentRequest`. The mismatched attachment then flows unchecked into `AttachmentsDownloader::run`'s `Done` branch, which persists it via `network.atlasdb.insert_instantiated_attachment(&attachment)` and resolves attachment instances using `attachment.hash()` rather than the hash actually committed on-chain.

### Finding Description
The equality that should hold — `response.attachment.hash() == request.content_hash` — is never checked anywhere in the download path: [1](#0-0) 

In `extend_with_attachments`, for each `(request, response)` pair the code only checks that `decode_atlas_get_attachment()` succeeds, then blindly does `self.attachments.insert(response.attachment)`. The `request.content_hash` field (which was populated from the on-chain-committed hash in `get_prioritized_attachments_requests`) is read only to build the `AttachmentRequest` (used for the outbound URL/query), and is discarded — it's never compared to the hash of the actual bytes returned. [2](#0-1) 

Downstream, `AttachmentsDownloader::run` consumes the `Done` state and, for every entry in `context.attachments`, looks up matching instances and persists the attachment using `attachment.hash()` — i.e., whatever hash the served bytes happen to produce, not the hash that was requested: [3](#0-2) 

Because `HashSet<Attachment>` and `find_all_attachment_instances`/`resolve_attachment` are keyed by `attachment.hash()` computed from the served payload, a malicious peer can return any bytes it likes for a requested `content_hash`; the node will compute a new (attacker-controlled) hash from those bytes and store/index the attachment under that hash without ever verifying it matches the hash requested. This breaks the invariant that Atlas attachments are content-addressed and trusted based on hash-matching to an on-chain-committed value.

### Impact Explanation
A malicious peer selected as an attachment source (via `get_most_reliable_source`) can cause the victim node to store and serve attacker-chosen data under a fabricated hash entry in its own Atlas DB. This is a BNS resolution integrity break: name records that reference this content indirectly (or clients later fetching by whatever hash was actually computed) can be served non-canonical data that no on-chain operation committed to. This matches the "High" impact category: attachment/BNS mismatch and serving non-canonical state as canonical.

### Likelihood Explanation
Preconditions are modest and attacker-cost is low: the node must be mid-sync of an `AttachmentsBatch` (a routine, frequent condition), and the attacker only needs to be selected as one of the reachable peer URLs serving that batch — no privileged role, RPC secret, or StackerDB slot is required. The attacker simply answers a `GET /v2/attachments/{content_hash}` request with a well-formed JSON body containing different bytes; `decode_atlas_get_attachment()` only validates structure/encoding, not content-hash equality. This is repeatable per attachment request and requires no volumetric attack.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments`, after successfully decoding the response, verify `response.attachment.hash() == request.content_hash` before inserting into `self.attachments`; on mismatch, treat it as a failed request (`report.bump_failed_requests()`) and optionally penalize/deregister the peer, rather than accepting attacker-controlled content.

### Proof of Concept
Add a test in `stackslib::net::atlas::download` (or a new test module) that:
1. Constructs an `AttachmentRequest { content_hash: H, .. }` for a known hash `H`.
2. Constructs a `StacksHttpResponse` whose JSON body decodes via `decode_atlas_get_attachment()` into an `Attachment` with content `b"attacker bytes"` such that `Attachment::hash() != H`.
3. Builds a `BatchedRequestsResult` with `succeeded = { (request, Some(response)) }`.
4. Calls `AttachmentsBatchStateContext::extend_with_attachments(context, &mut results)`.
5. Asserts `context.attachments.contains(&attacker_attachment)` is `true` even though `attacker_attachment.hash() != H`, demonstrating the missing equality check — and optionally continues to simulate the `Done` branch in `AttachmentsDownloader::run` to show `insert_instantiated_attachment` is called with the mismatched attachment.

### Citations

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

**File:** stackslib/src/net/atlas/download.rs (L464-474)
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
