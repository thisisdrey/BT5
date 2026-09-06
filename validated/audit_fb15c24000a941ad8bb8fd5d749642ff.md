### Title
Downloaded attachment content is stored without verifying it hashes to the requested `content_hash` - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` decodes an HTTP `GetAttachmentResponse` from a peer and unconditionally inserts the returned `Attachment` into `self.attachments`, with no check that the attachment's actual hash equals the `content_hash` that was requested. Because the requesting peer is not required to be honest (any peer serving the attachment's `AttachmentRequest.get_url()` can return arbitrary bytes), an attacker-controlled attachment can be substituted for the correct one before it is passed downstream to `AtlasDB::insert_instantiated_attachment`.

### Finding Description
The equality the system should enforce is `Attachment::hash() == AttachmentRequest.content_hash` before accepting a downloaded attachment. In `extend_with_attachments`: [1](#0-0) 

for each succeeded `(request, response)` pair, the code calls `response.decode_atlas_get_attachment()` and, if it decodes without error, does `self.attachments.insert(response.attachment)` — there is no comparison against `request.content_hash` anywhere in this function. The `AttachmentRequest` struct itself carries the expected `content_hash`, obtained originally from on-chain `AttachmentInstance` data: [2](#0-1) 

so the value needed to validate the response is available in scope but simply never used. The only gating that occurs is whether the JSON decodes successfully (`decode_atlas_get_attachment`), not whether its content matches the committed hash. This means a malicious HTTP server behind the requested peer URL (an unprivileged remote party that a node dials to fetch an attachment it is missing) can return a `GetAttachmentResponse` containing arbitrary bytes as `attachment.content`, and that Attachment is accepted into the batch's attachment pool. From there it flows into `AttachmentsBatchStateMachine::try_proceed`'s Done branch and ultimately into `AtlasDB::insert_instantiated_attachment`, which computes and stores it under its own hash and updates the `is_available` flag for any `attachment_instances` row whose committed `content_hash` matches that computed hash.

### Impact Explanation
An attacker who controls (or has compromised) only the HTTP server referenced by a peer's inventory-advertised attachment URL — which is by design not trust-anchored beyond serving the p2p/RPC port — can inject data that gets marked "available" and served by BNS/Atlas resolution as if it were the canonical, on-chain-committed attachment content, as long as its self-computed hash happens to coincide with some other pending instance's expected `content_hash` (or is merely stored inertly, corrupting the local `AtlasDB` state). This matches the "High - attachment/BNS mismatch, serving non-canonical state as canonical" category: name resolution consumers of the Atlas attachment store could be served data that no canonical block actually committed to.

### Likelihood Explanation
Preconditions: the node must have a pending `AttachmentInstance` (an unresolved zonefile/attachment expected by a BNS name update) and must select the malicious peer/URL as a download source (a normal, unprivileged condition since Atlas fetches attachments from whichever peer inventory shows it as available). No secret, admin role, or privileged trust relationship is needed — the attacker simply needs to run (or man-in-the-middle) the HTTP endpoint that a node is directed to for the attachment fetch. This is repeatable per-fetch and costs the attacker nothing beyond serving one crafted HTTP response.

### Recommendation
In `extend_with_attachments` (stackslib/src/net/atlas/download.rs), after `response.decode_atlas_get_attachment()` succeeds, compute `response.attachment.hash()` and compare it against `request.content_hash`; only call `self.attachments.insert(...)` on a match, and treat mismatches as a failed request (`report.bump_failed_requests()`), analogous to existing hash verification patterns used elsewhere in the Atlas subsystem (e.g., `AtlasDB::insert_instantiated_attachment`'s hash-keyed lookups).

### Proof of Concept
Add a test in `stackslib/src/net/atlas/tests.rs` (or `download.rs`'s test module):
1. Construct an `AttachmentsBatchStateContext` with a `peers` map containing one URL, and craft a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map has one entry: `(AttachmentRequest { content_hash: H1, ... }, Some(StacksHttpResponse_for_GetAttachmentResponse_with_content_hashing_to_H2)))` where `H1 != H2`.
2. Call `context.extend_with_attachments(&mut results)`.
3. Assert that `context.attachments` does NOT contain an attachment whose content hashes to H2 (expected fix), or, on the current code, observe that `context.attachments.insert(...)` inserted the H2 attachment — demonstrating the missing equality check at `download.rs:547-548`.

### Citations

**File:** stackslib/src/net/atlas/download.rs (L466-476)
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
            }
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
