### Title
Missing `attachments_max_size` bound check in `AttachmentsBatchStateContext::extend_with_attachments` allows oversized attachments into Atlas storage - (File: stackslib/src/net/atlas/download.rs)

### Summary
`extend_with_attachments` decodes a remote peer's `GetAttachmentResponse` and inserts the resulting `Attachment` directly into `self.attachments` with no check that `content.len()` respects `AtlasConfig.attachments_max_size`. Since `Attachment` equality/hashing is only content-based and the queueing logic is driven purely by a legitimate on-chain `content_hash` commitment (whose size is never bound on-chain), an attacker who controls the content behind a `content_hash` they themselves committed can serve arbitrarily large attachment blobs that get accepted into the downloader's attachment set en route to `AtlasDB`.

### Finding Description
`AtlasConfig.attachments_max_size` (default floor `ATTACHMENTS_MAX_SIZE_MIN = 1_048_576`) is documented as the cap on attachment content size [1](#0-0) [2](#0-1) . `Attachment` is a plain wrapper around `content: Vec<u8>` with no size validation anywhere in its constructor or `hash()` method [3](#0-2) .

When a peer responds to an attachment download request, `extend_with_attachments` decodes the response and unconditionally inserts the attachment content into the in-memory set with no length comparison against `attachments_max_size`: [4](#0-3) 

There is no call to any config value, no `content.len() > attachments_max_size` check, and no rejection path other than a decode failure (`decode_atlas_get_attachment` returning `Err`). As long as the bytes decode into an `Attachment` (which is just a hex-decoded byte vector wrapped in a struct), the size is irrelevant to acceptance at this stage.

The `content_hash` values that this response is matched against originate from real, attacker-triggerable on-chain events (e.g., BNS contract calls) that only commit a `Hash160` on-chain — the actual content size is never constrained by the blockchain itself. Consequently, an attacker can:
1. Trigger a legitimate `AttachmentInstance` with a `content_hash` of their own choosing (by controlling the preimage content off-chain and only publishing its hash on-chain).
2. Run their own peer and, when queried via `AttachmentRequest`, respond with a multi-megabyte `Attachment` whose `content` hashes to the committed `content_hash`.
3. Have `extend_with_attachments` accept and store this oversized attachment with no size gate.

Repeating this with fresh `content_hash` commitments allows accumulation of arbitrarily many oversized attachments, each individually exceeding the documented per-attachment cap, bounded storage-wise only by `MAX_UNINSTANTIATED_ATTACHMENTS_MIN`/`max_uninstantiated_attachments` count rather than by total byte size.

### Impact Explanation
This allows a remote, unprivileged party (anyone who can submit an on-chain contract call referencing the Atlas-tracked contracts, and run a peer that serves attachment content) to bypass the intended per-attachment size ceiling and inflate node storage/memory with attachments far larger than `attachments_max_size` permits, unbounded in aggregate by any consensus-enforced size limit. This matches the "storage exhaustion tied to a consensus commitment" class of High-severity issue: the byte-size guarantee that operators rely on (`attachments_max_size`) is silently violated by the download path, even though the count of tracked instances is capped.

### Likelihood Explanation
- The attacker needs no special privileges: no RPC secret, no admin role, no held StackerDB slot key required — they only need to (a) submit an on-chain call that creates an `AttachmentInstance` with a `content_hash` they control the preimage of, and (b) run a normal P2P/HTTP peer that other nodes will query for the corresponding `GetAttachmentResponse`.
- Cost is bounded by transaction fees for step (a); step (b) is free/cheap (just serving bytes).
- The bug is deterministically reachable on every successful attachment download for a hash the attacker controls — fully repeatable.

### Recommendation
In `extend_with_attachments` (stackslib/src/net/atlas/download.rs:530-558), before calling `self.attachments.insert(response.attachment)`, check `response.attachment.content.len() <= atlas_config.attachments_max_size as usize` (threading the `AtlasConfig`/`attachments_max_size` value into `AttachmentsBatchStateContext`), and treat responses exceeding the limit the same as a failed request (`report.bump_failed_requests()` and skip insertion), rather than relying solely on the AtlasDB layer (if any) to enforce it later.

### Proof of Concept
Rust test plan (net/atlas module test):
1. Build an `AttachmentsBatchStateContext` with a real `AtlasConfig` (`attachments_max_size = ATTACHMENTS_MAX_SIZE_MIN`).
2. Construct a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map contains one `AttachmentRequest` (with a legitimate queued `content_hash`) mapped to a `StacksHttpResponse` decodable via `decode_atlas_get_attachment()` into a `GetAttachmentResponse` whose `attachment.content` is, e.g., `vec![0u8; 5 * 1_048_576]` (5x over the max) and whose `Hash160::from_data(&content)` equals `content_hash`.
3. Call `context.extend_with_attachments(&mut results)`.
4. Assert `context.attachments` contains the 5MB attachment (`content.len() == 5*1_048_576`), demonstrating that `self.attachments.insert(response.attachment)` at stackslib/src/net/atlas/download.rs:548 executed with no size check, violating the invariant `content.len() <= attachments_max_size`.

### Citations

**File:** stackslib/src/net/atlas/mod.rs (L52-52)
```rust
const ATTACHMENTS_MAX_SIZE_MIN: u32 = 1_048_576;
```

**File:** stackslib/src/net/atlas/mod.rs (L92-98)
```rust
pub struct AtlasConfig {
    pub contracts: HashSet<QualifiedContractIdentifier>,
    pub attachments_max_size: u32,
    pub max_uninstantiated_attachments: u32,
    pub uninstantiated_attachments_expire_after: u32,
    pub unresolved_attachment_instances_expire_after: u32,
    pub genesis_attachments: Option<Vec<Attachment>>,
```

**File:** stackslib/src/net/atlas/mod.rs (L149-165)
```rust
pub struct Attachment {
    pub content: Vec<u8>,
}

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
