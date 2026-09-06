Based on the code I traced, the download path does not verify the attachment hash before storage.

### Title
Missing content-hash verification allows attacment cache poisoning via `AttachmentsBatchStateMachine::extend_with_attachments` - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` decodes an HTTP 200 response from a remote peer into a `GetAttachmentResponse` and inserts `response.attachment` directly into `self.attachments` without ever comparing `Hash160::from_data(&response.attachment.content)` against the `AttachmentRequest.content_hash` that was requested. That set is later drained in `AttachmentsDownloader::run`'s `Done` arm and passed straight to `AtlasDB::insert_instantiated_attachment`, meaning any peer in `context.peers` can supply arbitrary content for a requested `content_hash` and have it accepted into the Atlas attachment cache.

### Finding Description
The relevant code: [1](#0-0) 

`extend_with_attachments` is called from `try_proceed`'s `DownloadingAttachment` branch when `BatchedRequestsState::Done` is reached: [2](#0-1) 

For each successfully-decoded response, the code does `self.attachments.insert(response.attachment)` — it never checks the equality `Hash160::from_data(&response.attachment.content) == request.content_hash`. The `content_hash` that was actually requested (`request.content_hash`, from `AttachmentRequest`) is dropped after being used to build the outgoing request in `get_prioritized_attachments_requests` (lines 404-478); it is never carried forward to be checked against the response body.

Then in `AttachmentsDownloader::run`, the `Done` branch drains `context.attachments` and, for each `attachment`, calls `network.atlasdb.insert_instantiated_attachment(&attachment)` directly, keyed by `attachment.hash()` (i.e., whatever hash the *attacker's* content actually produces, not the originally requested `content_hash`): [3](#0-2) 

I attempted to confirm whether `Attachment::hash()`, `GetAttachmentResponse` decoding (`decode_atlas_get_attachment`), or `AtlasDB::insert_instantiated_attachment` perform the hash-equality check elsewhere (e.g., in `stackslib/src/net/atlas/mod.rs` or `stackslib/src/net/atlas/db.rs`), but tool access ran out before I could read those definitions in full — this is a limitation of my investigation, not a confirmed absence of a check. Based on what is visible in `download.rs`, however, the state-machine step that consumes the HTTP response and feeds `context.attachments` performs **no comparison at all** between the requested `content_hash` and the received content's hash; the request object (and its `content_hash`) is discarded once matched via the `results.succeeded` map key, and only the response's self-reported attachment is trusted.

### Impact Explanation
If no hash check exists downstream (in `Attachment::hash()`/`insert_instantiated_attachment`), a malicious peer that is a legitimate `context.peers` entry (any outbound sync peer the node is already connected to — not a privileged party) can respond to a `GET /v2/attachments/{content_hash}` request with a 200 body containing attacker-chosen `content` bytes. Since `attachment.hash()` is computed from the attacker's own bytes rather than validated against the originally-requested `content_hash`, the attachment gets stored/indexed under a hash of the attacker's choosing rather than under the legitimate `content_hash` tied to a BNS name commitment — meaning the specific commitment's `content_hash` slot would simply never resolve to attacker content (since it's indexed by the actual computed hash, not by `content_hash`). This still represents a correctness/DoS-adjacent issue (name attachments never resolving, or requests being wasted/repeated), but without confirming whether `insert_instantiated_attachment` or `AttachmentInstance` resolution re-checks the hash equality against the instance's stored `content_hash`, I cannot confirm the specific "BNS cache poisoned with attacker content associated with a legitimate name's hash" impact as stated in the question.

### Likelihood Explanation
Unable to fully confirm — see above. This requires further investigation of `stackslib/src/net/atlas/mod.rs` (`GetAttachmentResponse`, `Attachment::hash()`) and `stackslib/src/net/atlas/db.rs` (`insert_instantiated_attachment`, `find_attachment`) to determine whether a hash-equality gate exists there that `download.rs` relies on.

### Recommendation
N/A pending further code reading — cannot be certain without confirming the full path.

### Proof of Concept
N/A — cannot be finalized without confirming whether `Attachment::hash()`/`insert_instantiated_attachment` cross-check `content_hash`.

---

**Important caveat**: I was unable to complete verification of `stackslib/src/net/atlas/mod.rs` (definitions of `GetAttachmentResponse`, `Attachment`, `Attachment::hash()`, `decode_atlas_get_attachment`) and `stackslib/src/net/atlas/db.rs` (`insert_instantiated_attachment`, `find_attachment`) before running out of tool iterations. The `download.rs` file alone shows no explicit `Hash160::from_data(...) == content_hash` comparison in the `extend_with_attachments`/`run` path, but I cannot rule out that such a check is enforced inside `Attachment::hash()`'s consumers or `AtlasDB::insert_instantiated_attachment`/`find_attachment` (e.g., attachments might only ever be looked up by their self-computed hash, and instance resolution in `check_attachment_instances` at lines 246-254 already looks up by `attachment_instance.content_hash` via `atlas_db.find_attachment(&attachment_instance.content_hash)`, which would only succeed if the stored attachment's hash key equals the instance's expected `content_hash`). Given this uncertainty, I cannot assert with confidence that this constitutes a confirmed, exploitable vulnerability as scoped by the question. A Devin session with full file-reading capability would be needed to conclusively verify or refute this against `mod.rs` and `db.rs`.

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

**File:** stackslib/src/net/atlas/download.rs (L641-657)
```rust
            AttachmentsBatchStateMachine::DownloadingAttachment((
                attachments_requests,
                context,
            )) => {
                match BatchedRequestsState::try_proceed(
                    attachments_requests,
                    &context.dns_lookups,
                    network,
                    &context.connection_options,
                ) {
                    BatchedRequestsState::Done(ref mut results) => {
                        let context = context.extend_with_attachments(results);
                        AttachmentsBatchStateMachine::Done(context)
                    }
                    state => AttachmentsBatchStateMachine::DownloadingAttachment((state, context)),
                }
            }
```
