### Title
Oversized `Attachment.content` from a malicious peer bypasses `attachments_max_size` and is stored via `insert_instantiated_attachment` - (File: stackslib/src/net/atlas/download.rs)

### Summary
The Atlas attachment-download state machine accepts an `Attachment` returned by a remote peer for a `GET /v2/attachments/{hash}` request and unconditionally persists it via `AtlasDB::insert_instantiated_attachment` in the `Done` branch of `AttachmentsDownloader::run`, without ever checking the response's `content.len()` against `AtlasConfig.attachments_max_size`. This bypasses the exact bound (`ATTACHMENTS_MAX_SIZE_MIN` / configured `attachments_max_size`) that is otherwise enforced by `AtlasDB::should_keep_attachment` for attachments received via the RPC POST-attachment path.

### Finding Description
`AttachmentsBatchStateContext::extend_with_attachments` decodes the HTTP response body from a downloaded-attachment request and inserts the resulting `Attachment` directly into `self.attachments` with no size validation: [1](#0-0) 

`AttachmentsDownloader::run`'s `Done` branch then drains this set and, for every attachment, calls `insert_instantiated_attachment` unconditionally — there is no call to `should_keep_attachment` or any comparison against `attachments_max_size`: [2](#0-1) 

Contrast this with the RPC/gossip attachment-ingestion path, which explicitly enforces the size bound before accepting an attachment: [3](#0-2) 

The configured minimum bound is `ATTACHMENTS_MAX_SIZE_MIN = 1_048_576` (1 MiB), and `AtlasConfig.attachments_max_size` is meant to gate all attachment content admitted into the `attachments` table: [4](#0-3) [5](#0-4) 

Because the download path in `download.rs` never calls `should_keep_attachment` (or checks `attachment.content.len()` against `attachments_max_size`), an attacker who runs a peer and responds to a legitimate `AttachmentRequest` (queued because of a real, on-chain `AttachmentInstance` whose `content_hash` is unresolved) can return an arbitrarily large `content` blob in the `GetAttachmentResponse`. That oversized attachment is stored in the local `attachments` SQLite table with no cap check, unlike the equivalent RPC ingestion path.

### Impact Explanation
A remote, unprivileged peer that a node is syncing Atlas attachments from can cause the node to store attachment blobs of arbitrary size (bounded only by whatever HTTP body-size limits exist elsewhere in the RPC/P2P HTTP stack, not by the Atlas-specific `attachments_max_size` bound) in its local `attachments` table, keyed to a real, confirmed on-chain commitment hash. This is repeatable for every distinct queued `AttachmentInstance`/`content_hash` the node is trying to resolve, since each resolves through the same unguarded `Done` branch. This matches the "attachment/BNS mismatch"-adjacent, bounded-cap-bypass class of High-severity issues: state (attachment storage) is admitted that violates the size invariant the rest of the codebase (`should_keep_attachment`) enforces for the same table.

### Likelihood Explanation
Preconditions: the target node must have at least one unresolved `AttachmentInstance` (from real, already-confirmed on-chain BNS activity) queued for download, and the attacker must be one of the outbound peers the node is attempting to sync attachments from (achievable by running an ordinary peer/node that the target dials for attachment sync — no secret, StackerDB slot, or privileged role required). The attacker's cost is a single crafted HTTP 200 response to a `GET /v2/attachments/{hash}` request the node itself initiated. This is a straightforward, remotely reachable and repeatable path with no cryptographic or authorization gate in `download.rs`'s handling of the response body size.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (or in the `Done` branch of `AttachmentsDownloader::run`), reject/drop any decoded `Attachment` whose `content.len()` exceeds `network.atlasdb.atlas_config.attachments_max_size` (mirroring the check already performed in `AtlasDB::should_keep_attachment`) before it is added to `context.attachments` / passed to `insert_instantiated_attachment`, and bump the peer's failure count / mark it faulty for exceeding the bound.

### Proof of Concept
Rust net test plan (extending `stackslib/src/net/atlas/tests.rs`):
1. Build an `AttachmentsBatchStateContext` from a real `AttachmentInstance` (as in `test_downloader_context_attachment_requests`) with a queued `AttachmentRequest` for a known `content_hash`.
2. Simulate `BatchedRequestsResult<AttachmentRequest>::succeeded` containing a `StacksHttpResponse` whose decoded `GetAttachmentResponse.attachment.content` is several MB (e.g., `vec![0u8; 5 * 1024 * 1024]`), configured with `AtlasConfig.attachments_max_size` set to the default 1 MiB minimum.
3. Call `context.extend_with_attachments(&mut results)` and then drive `AttachmentsBatchStateMachine` to `Done`, invoking the same logic as `AttachmentsDownloader::run`'s `Done` branch (`atlasdb.insert_instantiated_attachment(&attachment)`).
4. Assert `insert_instantiated_attachment` succeeds (no `Err`) and that querying the `attachments` table for the hash returns a row whose `content.len()` exceeds `atlas_config.attachments_max_size`, proving the size cap enforced by `should_keep_attachment` is not applied on the download path.

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

**File:** stackslib/src/net/atlas/mod.rs (L52-52)
```rust
const ATTACHMENTS_MAX_SIZE_MIN: u32 = 1_048_576;
```

**File:** stackslib/src/net/atlas/mod.rs (L92-114)
```rust
pub struct AtlasConfig {
    pub contracts: HashSet<QualifiedContractIdentifier>,
    pub attachments_max_size: u32,
    pub max_uninstantiated_attachments: u32,
    pub uninstantiated_attachments_expire_after: u32,
    pub unresolved_attachment_instances_expire_after: u32,
    pub genesis_attachments: Option<Vec<Attachment>>,
}

impl AtlasConfig {
    pub fn new(mainnet: bool) -> AtlasConfig {
        let mut contracts = HashSet::new();
        contracts.insert(boot_code_id("bns", mainnet));
        AtlasConfig {
            contracts,
            attachments_max_size: ATTACHMENTS_MAX_SIZE_MIN,
            max_uninstantiated_attachments: MAX_UNINSTANTIATED_ATTACHMENTS_MIN,
            uninstantiated_attachments_expire_after: UNINSTANTIATED_ATTACHMENTS_EXPIRE_AFTER_MIN,
            unresolved_attachment_instances_expire_after:
                UNRESOLVED_ATTACHMENT_INSTANCES_EXPIRE_AFTER_MIN,
            genesis_attachments: None,
        }
    }
```
