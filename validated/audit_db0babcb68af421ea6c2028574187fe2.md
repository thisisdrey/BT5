### Title
Missing `attachments_max_size` enforcement before persisting downloaded attachments - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsBatchStateContext::extend_with_attachments` accepts any `Attachment` decoded from a peer's `GetAttachmentResponse` and inserts it into `self.attachments` without checking its length against `AtlasConfig.attachments_max_size`. The `AttachmentsDownloader::run` `Done` handler then calls `AtlasDB::insert_instantiated_attachment` directly on every such attachment, so an oversized BLOB is persisted to the SQLite `attachments` table regardless of the configured cap.

### Finding Description
`AtlasConfig::attachments_max_size` [1](#0-0)  is only checked at config load time via `AtlasConfig::validate` [2](#0-1) ; it is never consulted again in the attachment download/store code path.

`GetAttachmentResponse`'s `Deserialize` impl only hex-decodes the payload into raw bytes with no size limit: `hex_bytes(&hex_encoded)` then `Attachment::new(bytes)` [3](#0-2) .

In `extend_with_attachments`, a successfully decoded response is inserted unconditionally into the context's attachment set with no length check against `attachments_max_size`: [4](#0-3) 

The `AttachmentsDownloader::run` `Done` branch then iterates `context.attachments.drain()` and calls `network.atlasdb.insert_instantiated_attachment(&attachment)` for each one with no size gate at all: [5](#0-4) .

The `attachments` table stores `content` as an unconstrained `BLOB NOT NULL` [6](#0-5) , and nothing in the traced write path (`extend_with_attachments` → `run`'s `Done` handler → `insert_instantiated_attachment`) re-checks `content.len()` against `atlas_config.attachments_max_size`. A malicious outbound sync peer serving `/v2/attachments/{hash}` can therefore respond to a `GetAttachmentRequest` with a `GetAttachmentResponse` whose `attachment.content` is arbitrarily larger than the configured `attachments_max_size`, and it will be decoded and stored as-is.

### Impact Explanation
A peer that a victim node treats as an "outbound sync peer" for Atlas downloads (no special privilege required beyond being selected as a data-URL peer) can cause the victim to write attachments of unbounded size into its local `atlasdb` SQLite database, exceeding the operator-configured `attachments_max_size` cap. This is a resource-consumption/state-integrity violation: an unauthenticated write into persistent node state that bypasses an explicit configured limit, and it is repeatable per downloaded content hash (each new distinct oversized attachment adds unconstrained disk usage). This does not directly cause an immediate crash, but it defeats a security-relevant configuration guarantee (the max attachment size bound) with no code-level enforcement at the point of storage.

### Likelihood Explanation
The attacker only needs to be selected as one of the victim's Atlas data-URL peers (`network.get_outbound_sync_peers()` / `get_data_url`) and needs the victim to have a pending `AttachmentInstance` referencing an unknown content hash so a `GetAttachmentRequest` is issued to it. No secret, signature check, or privileged role is required to answer with a crafted `GetAttachmentResponse` — decoding is a simple hex-decode with no length validation. This makes the precondition realistic for any operator running an outbound Atlas sync against untrusted/attacker-controlled peers.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments` (or immediately before `insert_instantiated_attachment` in `AttachmentsDownloader::run`), reject any decoded `Attachment` whose `content.len()` exceeds `network.atlasdb`'s configured `attachments_max_size`, treating it the same as a failed/faulty response (bump `report.bump_failed_requests()` and/or deregister the peer). Additionally, enforce the same bound inside `GetAttachmentResponse::deserialize` so oversized attachments are rejected at decode time rather than relying solely on downstream callers.

### Proof of Concept
Rust test plan (net test module, e.g. `stackslib/src/net/atlas/tests.rs`):
1. Construct an `AtlasConfig` with `attachments_max_size = ATTACHMENTS_MAX_SIZE_MIN` (1_048_576 bytes).
2. Build a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map contains one `AttachmentRequest` mapped to `Some(StacksHttpResponse)` encoding a `GetAttachmentResponse { attachment: Attachment::new(vec![0u8; ATTACHMENTS_MAX_SIZE_MIN as usize + 1]) }` (hex-encoded payload, N = max_size + 1 bytes).
3. Call `AttachmentsBatchStateContext::extend_with_attachments` on this result and assert `context.attachments` contains an entry with `content.len() == N`.
4. Feed that context through the `Done` handling logic (or call `atlasdb.insert_instantiated_attachment(&attachment)` directly as `run` does) and then call `atlasdb.find_attachment(&attachment.hash())`.
5. Assert the stored row's `content.len() == N` even though `N > atlas_config.attachments_max_size`, proving the bound is not enforced at storage time.

### Citations

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

**File:** stackslib/src/net/atlas/mod.rs (L92-99)
```rust
pub struct AtlasConfig {
    pub contracts: HashSet<QualifiedContractIdentifier>,
    pub attachments_max_size: u32,
    pub max_uninstantiated_attachments: u32,
    pub uninstantiated_attachments_expire_after: u32,
    pub unresolved_attachment_instances_expire_after: u32,
    pub genesis_attachments: Option<Vec<Attachment>>,
}
```

**File:** stackslib/src/net/atlas/mod.rs (L116-144)
```rust
    pub fn validate(&self) -> Result<(), String> {
        if self.attachments_max_size < ATTACHMENTS_MAX_SIZE_MIN {
            Err(format!(
                "Invalid value for `attachments_max_size`: {}. Expected {} or greater",
                self.attachments_max_size, ATTACHMENTS_MAX_SIZE_MIN
            ))
        } else if self.max_uninstantiated_attachments < MAX_UNINSTANTIATED_ATTACHMENTS_MIN {
            Err(format!(
                "Invalid value for `max_uninstantiated_attachments`: {}. Expected {} or greater",
                self.max_uninstantiated_attachments, MAX_UNINSTANTIATED_ATTACHMENTS_MIN
            ))
        } else if self.uninstantiated_attachments_expire_after
            < UNINSTANTIATED_ATTACHMENTS_EXPIRE_AFTER_MIN
        {
            Err(format!(
                "Invalid value for `uninstantiated_attachments_expire_after`: {}. Expected {} or greater",
                self.uninstantiated_attachments_expire_after, UNINSTANTIATED_ATTACHMENTS_EXPIRE_AFTER_MIN
            ))
        } else if self.unresolved_attachment_instances_expire_after
            < UNRESOLVED_ATTACHMENT_INSTANCES_EXPIRE_AFTER_MIN
        {
            Err(format!(
                "Invalid value for `unresolved_attachment_instances_expire_after`: {}. Expected {} or greater",
                self.unresolved_attachment_instances_expire_after, UNRESOLVED_ATTACHMENT_INSTANCES_EXPIRE_AFTER_MIN
            ))
        } else {
            Ok(())
        }
    }
```

**File:** stackslib/src/net/atlas/download.rs (L153-162)
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
```

**File:** stackslib/src/net/atlas/download.rs (L547-552)
```rust
            if let Ok(response) = response.decode_atlas_get_attachment() {
                self.attachments.insert(response.attachment);
                report.bump_successful_requests();
            } else {
                report.bump_failed_requests();
            }
```

**File:** stackslib/src/net/atlas/db.rs (L64-71)
```rust
const ATLASDB_INITIAL_SCHEMA: &[&str] = &[
    r#"
    CREATE TABLE attachments(
        hash TEXT UNIQUE PRIMARY KEY,
        content BLOB NOT NULL,
        was_instantiated INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    );"#,
```
