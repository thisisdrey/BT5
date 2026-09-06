### Title
Missing `attachments_max_size` enforcement in `AttachmentsBatchStateContext::extend_with_attachments` allows unbounded attachment storage growth - (File: `stackslib/src/net/atlas/download.rs`)

### Summary
`extend_with_attachments` decodes a peer's `GetAttachmentResponse` and inserts the raw `Attachment` into the in-memory result set with no check against `AtlasConfig.attachments_max_size` before the data is later persisted to `AtlasDB`. Since the only gate on acceptance is a correct `Hash160` match to a `content_hash` the node already queued, and an attacker fully controls both the committed `content_hash` (by submitting their own on-chain transaction to a whitelisted contract such as BNS) and the corresponding content, they can make the node download and store arbitrarily large attachments, repeated for many distinct `content_hash` values.

### Finding Description
`Attachment.content` is an unbounded `Vec<u8>` [1](#0-0) , and `GetAttachmentResponse` is deserialized by simply hex-decoding the payload with no size validation at all [2](#0-1) .

`AtlasConfig.attachments_max_size` (defaulting to `ATTACHMENTS_MAX_SIZE_MIN = 1_048_576`) is only validated to be *at least* the minimum at config load time [3](#0-2) ; it is never passed into or consulted by the attachment-fetch pipeline.

When a batch of attachment downloads completes, `extend_with_attachments` decodes each peer response and unconditionally inserts the attachment into the result `HashSet` on any successful hash-checked decode, with no comparison of `response.attachment.content.len()` against any size bound: [4](#0-3) 

The only validation gating acceptance of the content is that its hash matches a queued `content_hash` (performed inside `decode_atlas_get_attachment`/the request-matching logic upstream), not any size constraint. Since `content_hash` values originate from real on-chain `AttachmentInstance` events emitted by a whitelisted contract (e.g., `bns`) [5](#0-4) , and nothing on-chain bounds the size of the off-chain content a `content_hash` may correspond to, an attacker who submits their own contract-call transaction can freely choose arbitrarily large content, compute its `Hash160`, and have that hash accepted as a legitimate `content_hash` to fetch. The attacker (or any peer advertising an inventory bit for that hash) then serves that oversized content over the attachment-fetch HTTP path; `extend_with_attachments` accepts it without a size check and the batch result flows on to be persisted into `AtlasDB.attachments`.

### Impact Explanation
Each accepted oversized attachment is written to `AtlasDB.attachments`, which is otherwise only bounded by `max_uninstantiated_attachments` (default `MAX_UNINSTANTIATED_ATTACHMENTS_MIN = 50_000`) as a *count*, not a byte budget [6](#0-5) . By repeating the on-chain-commitment + serve pattern across many distinct `content_hash` values, an attacker can cause a victim node's Atlas/attachment storage to grow far beyond what `attachments_max_size` was designed to bound per-item, leading to disk exhaustion that is not tied to any real consensus-enforced content-size limit. This is a storage-exhaustion DoS reachable by any unprivileged party able to submit on-chain transactions to a whitelisted Atlas contract and run a peer that serves attachment content over the standard attachment download RPC path.

### Likelihood Explanation
The attacker needs no privileged role: they submit ordinary transactions to a contract already whitelisted in `AtlasConfig.contracts` (e.g., `bns`) to create `AttachmentInstance` events with self-chosen, self-computed `content_hash` values, and run a normal peer/node that answers attachment GET requests with correspondingly large content. Cost is bounded by transaction fees for each new commitment, and by the cost of hosting/serving the large content once, after which the data is durably replicated onto the victim's disk. The attack is straightforwardly repeatable up to the uninstantiated-attachment count cap, with total exhausted storage scaling with content size × cap.

### Recommendation
Enforce `attachments_max_size` at the point attachment content is accepted, before it is inserted into the batch state (`extend_with_attachments` in `stackslib/src/net/atlas/download.rs`) and/or before it is written into `AtlasDB`. Reject and record a failed-request reliability hit for any decoded attachment whose `content.len()` exceeds `AtlasConfig.attachments_max_size`, and thread the `AtlasConfig` (or just the size limit) into the download/state-machine context so this check is actually reachable from `extend_with_attachments`.

### Proof of Concept
1. In `stackslib/src/net/atlas/tests.rs`, construct an `AttachmentsBatchStateContext` with a queued `AttachmentRequest` whose `content_hash` is `Hash160::from_data(&big_content)` for a `big_content: Vec<u8>` of, say, 8 MB (larger than `ATTACHMENTS_MAX_SIZE_MIN`).
2. Build a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map contains that request mapped to a `StacksHttpResponse` encoding `GetAttachmentResponse { attachment: Attachment::new(big_content.clone()) }`.
3. Call `context.extend_with_attachments(&mut results)` and assert that `context.attachments` contains an `Attachment` with `content.len() == big_content.len()` (i.e., 8 MB), even though `AtlasConfig::new(..).attachments_max_size == ATTACHMENTS_MAX_SIZE_MIN` (1 MB) — demonstrating no rejection occurs at `download.rs:547-549`.

### Citations

**File:** stackslib/src/net/atlas/mod.rs (L52-55)
```rust
const ATTACHMENTS_MAX_SIZE_MIN: u32 = 1_048_576;
const MAX_UNINSTANTIATED_ATTACHMENTS_MIN: u32 = 50_000;
const UNINSTANTIATED_ATTACHMENTS_EXPIRE_AFTER_MIN: u32 = 86_400;
const UNRESOLVED_ATTACHMENT_INSTANCES_EXPIRE_AFTER_MIN: u32 = 172_800;
```

**File:** stackslib/src/net/atlas/mod.rs (L69-77)
```rust
impl<'de> Deserialize<'de> for GetAttachmentResponse {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<GetAttachmentResponse, D::Error> {
        let payload = String::deserialize(d)?;
        let hex_encoded = payload.parse::<String>().map_err(de_Error::custom)?;
        let bytes = hex_bytes(&hex_encoded).map_err(de_Error::custom)?;
        let attachment = Attachment::new(bytes);
        Ok(GetAttachmentResponse { attachment })
    }
}
```

**File:** stackslib/src/net/atlas/mod.rs (L116-121)
```rust
    pub fn validate(&self) -> Result<(), String> {
        if self.attachments_max_size < ATTACHMENTS_MAX_SIZE_MIN {
            Err(format!(
                "Invalid value for `attachments_max_size`: {}. Expected {} or greater",
                self.attachments_max_size, ATTACHMENTS_MAX_SIZE_MIN
            ))
```

**File:** stackslib/src/net/atlas/mod.rs (L149-151)
```rust
pub struct Attachment {
    pub content: Vec<u8>,
}
```

**File:** stackslib/src/net/atlas/mod.rs (L172-181)
```rust
pub struct AttachmentInstance {
    pub content_hash: Hash160,
    pub attachment_index: u32,
    pub stacks_block_height: u64,
    pub index_block_hash: StacksBlockId,
    pub metadata: String,
    pub contract_id: QualifiedContractIdentifier,
    pub tx_id: Txid,
    pub canonical_stacks_tip_height: Option<u64>,
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
