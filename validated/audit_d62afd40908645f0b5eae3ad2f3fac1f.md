### Title
Unbounded attachment size accepted via `GetAttachmentResponse::deserialize`, bypassing `AtlasConfig.attachments_max_size` - (File: stackslib/src/net/atlas/mod.rs)

### Summary
`GetAttachmentResponse::deserialize` decodes an attacker-controlled hex string into arbitrary-length bytes via `hex_bytes` and wraps it in `Attachment::new(bytes)` with no size check against `AtlasConfig.attachments_max_size`. This response is consumed by `decode_atlas_get_attachment`, then `extend_with_attachments`, and finally stored unconditionally via `AtlasDB::insert_instantiated_attachment`, none of which enforce the size policy.

### Finding Description
The broken equality: every attachment that ends up in `AtlasDB` should be bounded by `AtlasConfig.attachments_max_size` (enforced for locally-submitted attachments via `AtlasDB::should_keep_attachment`, used only in `posttransaction.rs`), but attachments arriving from the P2P/RPC attachment-download path are never checked against this limit.

Path: a malicious peer answers a `GET /v2/attachments/{content_hash}` request (issued by `AttachmentRequest::make_request_type`) with a JSON string of hex data. `GetAttachmentResponse::deserialize` (stackslib/src/net/atlas/mod.rs:69-77) calls `hex_bytes` on the payload with no length bound and constructs `Attachment::new(bytes)`. `StacksHttpResponse::decode_atlas_get_attachment` (stackslib/src/net/api/getattachment.rs:159-165) invokes this deserializer directly on the parsed JSON body with no additional size validation. `AttachmentsBatchStateContext::extend_with_attachments` (stackslib/src/net/atlas/download.rs:530-558) takes the decoded `response.attachment` and inserts it into `self.attachments` unconditionally on success. That set is later persisted via `AtlasDB::insert_instantiated_attachment` (stackslib/src/net/atlas/db.rs:576-592), called from `stackslib/src/net/atlas/download.rs:161`, which performs a raw SQL insert with no size or contract check whatsoever.

By contrast, `AtlasDB::should_keep_attachment` (stackslib/src/net/atlas/db.rs:249-266) does enforce `attachment.content.len() as u32 > self.atlas_config.attachments_max_size`, but it is only invoked from the locally-submitted-transaction path (`posttransaction.rs`), not from the peer-download path. The only remaining bound on the wire is the generic HTTP transport limit `MAX_MESSAGE_LEN` (stacks-common/src/codec/mod.rs) enforced in `stackslib/src/net/httpcore.rs` (chunked-transfer reader and non-chunked payload length checks), which is a global message-size cap unrelated to and typically much larger than the configured `attachments_max_size` (minimum 1 MiB per `ATTACHMENTS_MAX_SIZE_MIN`). Thus a hostile peer can supply an attachment whose size is anywhere between `attachments_max_size` and `MAX_MESSAGE_LEN`, and it will be accepted and written to the local `AtlasDB` even though policy dictates it should be rejected.

### Impact Explanation
A remote, unprivileged peer that is asked for an attachment (or that answers unsolicited/otherwise-reachable attachment requests) can cause the downloading node to store attachment content that exceeds its own configured `attachments_max_size` policy, inflating the local `AtlasDB` on disk without any corresponding size check. This is a policy-bypass/state-integrity issue: state (the AtlasDB `attachments` table) is written that violates the node's own configured invariant, and is repeatable per attachment request/hash the node is trying to resolve.

### Likelihood Explanation
Preconditions are minimal: the node must be actively resolving an attachment instance (a normal, common operation during BNS/Atlas sync), and any peer serving that URL can supply an oversized payload. No secret, signature, or privileged role is required — this is a standard unauthenticated GET RPC response. Attacker cost is a single crafted HTTP response.

### Recommendation
Enforce `attachments_max_size` inside `GetAttachmentResponse::deserialize` (or immediately after, in `decode_atlas_get_attachment`) by rejecting the response when the decoded byte length exceeds the configured limit, and additionally call `AtlasDB::should_keep_attachment` (or an equivalent size/contract check) before `insert_instantiated_attachment` in `stackslib/src/net/atlas/download.rs`'s `try_proceed`/`extend_with_attachments` path.

### Proof of Concept
```rust
// stackslib/src/net/atlas/tests.rs (illustrative)
#[test]
fn test_oversized_attachment_bypasses_max_size() {
    let atlas_config = AtlasConfig {
        contracts: HashSet::new(),
        attachments_max_size: 1024, // policy: 1KB max
        max_uninstantiated_attachments: 100,
        uninstantiated_attachments_expire_after: 200,
        unresolved_attachment_instances_expire_after: 200,
        genesis_attachments: None,
    };
    let mut atlas_db = AtlasDB::connect_memory(atlas_config.clone()).unwrap();

    // Craft an oversized hex-encoded JSON body, e.g. 10x attachments_max_size
    let oversized_bytes = vec![0xAAu8; (atlas_config.attachments_max_size as usize) * 10];
    let hex_body = format!("\"{}\"", to_hex(&oversized_bytes));

    // Simulate GetAttachmentResponse::deserialize directly on this payload
    let resp: GetAttachmentResponse = serde_json::from_str(&hex_body).unwrap();
    assert_eq!(resp.attachment.content.len(), oversized_bytes.len());

    // No check against attachments_max_size occurred during deserialize.
    // Now show it is accepted for storage without a should_keep_attachment gate:
    atlas_db.insert_instantiated_attachment(&resp.attachment).unwrap();
    let stored = atlas_db.find_attachment(&resp.attachment.hash()).unwrap().unwrap();
    assert!(stored.content.len() as u32 > atlas_config.attachments_max_size); // policy violated
}
```
This demonstrates that `GetAttachmentResponse::deserialize` and `AtlasDB::insert_instantiated_attachment` accept and store content exceeding `AtlasConfig.attachments_max_size`, whereas `AtlasDB::should_keep_attachment` (used only on the local-submission path) would have rejected it.