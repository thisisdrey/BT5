### Title
Unbounded attachment size accepted via `/v2/attachments/{hash}` GET response bypasses `attachments_max_size` before `AtlasDB` insertion - ([File: stackslib/src/net/atlas/download.rs])

### Summary
When a node fetches an `Attachment` from a peer via `GET /v2/attachments/{hash}`, the client-side decode path (`StacksHttpResponse::decode_atlas_get_attachment`) and the subsequent storage path (`AttachmentsDownloader::run` → `AtlasDB::insert_instantiated_attachment`) never call the size-enforcing check `AtlasDB::should_keep_attachment`. A malicious/misbehaving peer serving this response can therefore return an attachment whose `content` is arbitrarily larger than the configured `attachments_max_size` (default `ATTACHMENTS_MAX_SIZE_MIN` = 1 MiB), and it will be persisted into the requesting node's `AtlasDB` without rejection.

### Finding Description
The invariant the question asks about — `len(served_content) <= configured_max_attachment_size` — is enforced only in `AtlasDB::should_keep_attachment` [1](#0-0)  which checks both contract membership and `attachment.content.len() as u32 > self.atlas_config.attachments_max_size`. This function is used for attachments arriving via the "posted" path (on-chain contract calls), but it is never invoked anywhere in the peer-to-peer attachment download path.

The download path is:
1. `StacksHttpResponse::decode_atlas_get_attachment` parses the JSON body into `GetAttachmentResponse` with no length check at all: [2](#0-1) 
2. `GetAttachmentResponse`'s `Deserialize` impl hex-decodes the `content` string into an `Attachment` with no cap on its length: [3](#0-2) 
3. `AttachmentsBatchStateContext::extend_with_attachments` takes the decoded response and inserts `response.attachment` straight into an in-memory `HashSet<Attachment>` with no size validation: [4](#0-3) 
4. `AttachmentsDownloader::run` later drains that set and calls `atlasdb.insert_instantiated_attachment(&attachment)` directly — again with no call to `should_keep_attachment` or any size check: [5](#0-4) 
5. `AtlasDB::insert_instantiated_attachment` blindly writes the BLOB `content` column with no length constraint (SQLite `BLOB` has no size limit enforced here): [6](#0-5) 

The `AtlasConfig.attachments_max_size` field, whose minimum is `ATTACHMENTS_MAX_SIZE_MIN` (1,048,576 bytes), is defined and validated only at config-load time — it is never consulted during the attachment-fetch/insert flow: [7](#0-6) [8](#0-7) 

Any peer selected as an outbound-sync source (an ordinary, unprivileged remote peer connected over P2P that reports the requested content hash present in its `GetAttachmentsInvResponse` inventory) can respond to the follow-up `GET /v2/attachments/{hash}` with a JSON body containing a hex-encoded blob of arbitrary size (tens/hundreds of MB), and the victim will decode and persist it to its `AtlasDB` unconditionally.

### Impact Explanation
A malicious peer can cause the victim node to allocate and persist an arbitrarily large blob into `AtlasDB` for each attachment content-hash it advertises, since neither the HTTP response decoder nor the storage call re-validates the configured `attachments_max_size` bound. This is repeatable per distinct attachment instance the victim is trying to resolve (bounded by how many `AttachmentInstance`s exist, but each one can trigger one oversized store), leading to unbounded memory allocation during JSON/hex decode and unbounded on-disk SQLite growth — a resource-exhaustion condition on a read/gossip-driven code path, consistent with "bounded compute DoS on a read endpoint" escalating toward storage/memory exhaustion.

### Likelihood Explanation
No privileged role, secret, or signature is required — the attacker only needs to be a normal, connected P2P peer that the victim selects as an outbound sync peer and that claims (via a forged/valid `GetAttachmentsInvResponse`) to have the requested content, which is a reachable and low-cost precondition since attachment inventory responses are unauthenticated. The attacker just serves an oversized body once the victim issues the `GET /v2/attachments/{hash}` request; this is repeatable for every attachment instance resolved through the attacker's peer.

### Recommendation
Enforce `AtlasDB::should_keep_attachment` (or an equivalent length check against `self.atlas_config.attachments_max_size`) in `StacksHttpResponse::decode_atlas_get_attachment` before returning `GetAttachmentResponse`, and/or add an explicit size check in `AttachmentsBatchStateContext::extend_with_attachments` and in `AtlasDB::insert_instantiated_attachment` before the SQL insert, rejecting/discarding any attachment whose `content.len()` exceeds the configured maximum.

### Proof of Concept
In `stackslib/src/net/api/tests/getattachment.rs`, craft an HTTP response body for `GetAttachmentResponse` whose hex-encoded `content` decodes to a byte vector longer than `ATTACHMENTS_MAX_SIZE_MIN` (e.g., 2 MiB of `0xff`), feed it through `StacksHttpResponse::decode_atlas_get_attachment`, and assert it returns an error or truncates — currently it succeeds and returns the oversized `Attachment`. A second test should drive `AttachmentsBatchStateContext::extend_with_attachments` / `AttachmentsDownloader::run` with a mocked oversized response and assert `AtlasDB::find_attachment` shows the stored content length exceeds `atlas_config.attachments_max_size`, demonstrating the missing enforcement at storage time (`stackslib/src/net/atlas/db.rs::insert_instantiated_attachment`).

### Citations

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

**File:** stackslib/src/net/atlas/db.rs (L576-592)
```rust
    pub fn insert_instantiated_attachment(
        &mut self,
        attachment: &Attachment,
    ) -> Result<(), db_error> {
        let now = util::get_epoch_time_secs() as i64;
        let tx = self.tx_begin()?;
        tx.execute(
            "INSERT OR REPLACE INTO attachments (hash, content, was_instantiated, created_at) VALUES (?, ?, 1, ?)",
            params![attachment.hash(), attachment.content, now],
        )?;
        tx.execute(
            "UPDATE attachment_instances SET is_available = 1 WHERE content_hash = ?1 AND status = ?2",
            params![attachment.hash(), AttachmentInstanceStatus::Checked],
        )?;
        tx.commit()?;
        Ok(())
    }
```

**File:** stackslib/src/net/api/getattachment.rs (L158-165)
```rust
impl StacksHttpResponse {
    pub fn decode_atlas_get_attachment(self) -> Result<GetAttachmentResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: GetAttachmentResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
    }
```

**File:** stackslib/src/net/atlas/mod.rs (L52-52)
```rust
const ATTACHMENTS_MAX_SIZE_MIN: u32 = 1_048_576;
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

**File:** stackslib/src/net/atlas/mod.rs (L101-123)
```rust
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

    pub fn validate(&self) -> Result<(), String> {
        if self.attachments_max_size < ATTACHMENTS_MAX_SIZE_MIN {
            Err(format!(
                "Invalid value for `attachments_max_size`: {}. Expected {} or greater",
                self.attachments_max_size, ATTACHMENTS_MAX_SIZE_MIN
            ))
        } else if self.max_uninstantiated_attachments < MAX_UNINSTANTIATED_ATTACHMENTS_MIN {
            Err(format!(
```

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
