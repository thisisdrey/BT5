## Title
Downloaded attachments bypass `attachments_max_size` before being stored in the AtlasDB - (File: stackslib/src/net/atlas/download.rs)

### Summary
The attachment download path decodes remote peer HTTP responses via `decode_atlas_get_attachment()` into an `Attachment` and directly inserts it into the state machine's `attachments` set and later into `AtlasDB` via `insert_instantiated_attachment`, without ever calling `AtlasDB::should_keep_attachment` (the only place `attachments_max_size` is enforced). The `Attachment::new`/`hash()` constructors in `stackslib/src/net/atlas/mod.rs` also perform no size validation.

### Finding Description
`Attachment::new(content: Vec<u8>)` and `Attachment::hash()` in `stackslib/src/net/atlas/mod.rs` (lines 153-165) contain no length check against `AtlasConfig.attachments_max_size`. The size check exists only in `AtlasDB::should_keep_attachment` (`stackslib/src/net/atlas/db.rs:249-266`), which compares `attachment.content.len() as u32 > self.atlas_config.attachments_max_size`.

Tracing the downloader path: `AttachmentsBatchStateContext::extend_with_attachments` (`download.rs:530-558`) takes the raw HTTP response, calls `response.decode_atlas_get_attachment()`, and on success does `self.attachments.insert(response.attachment)` — with no size check at all. Then in `AttachmentsDownloader::run` (`download.rs:150-169`), for the `Done` state, each attachment in `context.attachments.drain()` is passed straight to `network.atlasdb.insert_instantiated_attachment(&attachment)` — again with no size check.

`should_keep_attachment` is never called in this path; it appears to be reserved for a different ingestion path (e.g. attachments POSTed via RPC), not for attachments fetched by the P2P downloader from a peer-supplied `AttachmentRequest`. This means a byte-length cap declared in `Stacks.toml` as `attachments_max_size` is not enforced against attachments obtained from the download/gossip path, allowing arbitrary-size attachment content (bounded only by whatever caps exist in the underlying HTTP/response body length, not by the Atlas-specific config) to be persisted into `AtlasDB`.

### Impact Explanation
An attacker acting as an `AttachmentRequest` source peer can serve an oversized `Attachment.content` in its HTTP response body. If the outer HTTP/StacksHttpResponse layer does not independently enforce `attachments_max_size` (the question's premise is that only a generic HTTP body length pre-check exists, not a field-specific cap tied to the Atlas config), the oversized content is stored keyed by its hash in `AtlasDB`, unconditionally growing local storage/memory beyond the operator-configured `attachments_max_size` bound. This is a bounded-per-message resource-consumption issue on a background download path (not a single-message unauthenticated crash), matching the "High" bucket of "bounded compute/storage DoS."

### Likelihood Explanation
Preconditions: the node must have an active `AttachmentInstance` referencing a real `content_hash` that the attacker's peer claims to have (learned via `GetAttachmentsInvResponse`), and the attacker must be selected as a download source. This requires only being an ordinary outbound-sync peer serving Atlas endpoints — no privileged role, secret, or admin access — satisfying the "unprivileged remote peer" threat model. Repeatable per attachment instance/content_hash the attacker can serve for.

### Recommendation
Enforce `attachments_max_size` at the point attachments are accepted from the network: in `AttachmentsBatchStateContext::extend_with_attachments` (`download.rs:530-558`), check `response.attachment.content.len() as u32 <= connection_options`/`atlas_config.attachments_max_size` before inserting into `self.attachments`; and/or add the same enforcement inside `AtlasDB::insert_instantiated_attachment` and `insert_uninstantiated_attachment` so that no code path can bypass the cap, mirroring the existing check in `AtlasDB::should_keep_attachment`.

### Proof of Concept
1. In `stackslib/src/net/atlas/download.rs` or a new test module, construct an `AtlasConfig` with `attachments_max_size: 16` (as done in `tests.rs::test_keep_uninstantiated_attachments`).
2. Build a `GetAttachmentResponse`-equivalent `Attachment` with `content` of length > 16 bytes (e.g., 1024 bytes) and craft a `StacksHttpResponse` whose body decodes to that via `decode_atlas_get_attachment()`.
3. Feed this response into `AttachmentsBatchStateContext::extend_with_attachments`, then call `AtlasDB::insert_instantiated_attachment` on the resulting `Attachment` as `AttachmentsDownloader::run`'s `Done` branch does (`download.rs:153-169`).
4. Assert `atlas_db.find_attachment(&attachment.hash())` returns `Some` with the oversized content, despite `attachment.content.len() as u32 > atlas_config.attachments_max_size`, proving no rejection occurs (contrast with `AtlasDB::should_keep_attachment` which would have returned `false` for the same content, but is never invoked on this path). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** stackslib/src/net/atlas/mod.rs (L153-165)
```rust
impl Attachment {
    pub fn new(content: Vec<u8>) -> Attachment {
        Attachment { content }
    }

    pub fn hash(&self) -> Hash160 {
        Hash160::from_data(&self.content)
    }

    pub fn empty() -> Attachment {
        Attachment { content: vec![] }
    }
}
```

**File:** stackslib/src/net/atlas/db.rs (L249-266)
```rust
    pub fn should_keep_attachment(
        &self,
        contract_id: &QualifiedContractIdentifier,
        attachment: &Attachment,
    ) -> bool {
        if !self.atlas_config.contracts.contains(contract_id) {
            info!(
                "Atlas: will discard posted attachment - {} not in supported contracts",
                contract_id
            );
            return false;
        }
        if attachment.content.len() as u32 > self.atlas_config.attachments_max_size {
            info!("Atlas: will discard posted attachment - attachment too large");
            return false;
        }
        true
    }
```

**File:** stackslib/src/net/atlas/download.rs (L150-169)
```rust
            AttachmentsBatchStateMachine::try_proceed(ongoing_fsm, dns_client, network);

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
