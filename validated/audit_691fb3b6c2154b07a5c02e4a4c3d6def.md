### Title
`GetAttachmentResponse::deserialize` skips `attachments_max_size` enforcement on the attachment-download (pull) path - ([File: stackslib/src/net/atlas/mod.rs])

### Summary
`GetAttachmentResponse::deserialize` hex-decodes the JSON body of a `GET /v2/attachments/<hash>` response and constructs `Attachment::new(bytes)` with no comparison against `AtlasConfig.attachments_max_size` [1](#0-0) . This attachment is then fanned through `decode_atlas_get_attachment` and `extend_with_attachments` into `AtlasDB::insert_instantiated_attachment` without any size check being applied along that path [2](#0-1) [3](#0-2) [4](#0-3) . Any outbound sync peer a node fetches an attachment from can therefore return content whose size is bounded only by the generic HTTP response framing limits enforced in `httpcore.rs`, not by the `attachments_max_size` invariant that `should_keep_attachment` enforces on the push (`POST`) path.

### Finding Description
The invariant the question posits — `response.attachment.content.len() <= attachments_max_size` — is not checked anywhere on this decode path. `GetAttachmentResponse::deserialize` only performs `hex_bytes(&hex_encoded)` and wraps the result directly in `Attachment::new(bytes)` [5](#0-4) . This is invoked from `StacksHttpResponse::decode_atlas_get_attachment`, which parses the JSON payload and calls `serde_json::from_value` — again with no size gate [2](#0-1) . The `AttachmentsBatchStateContext::extend_with_attachments` method then inserts the decoded `Attachment` straight into an in-memory `HashSet<Attachment>` [3](#0-2) , and `AttachmentsDownloader::run`'s `Done` branch later drains that set and calls `atlasdb.insert_instantiated_attachment(&attachment)` for each one [6](#0-5) , persisting the oversized blob to the local AtlasDB and holding it (and the query result set copies via `.clone()`) in memory.

The `attachments_max_size` bound is defined only in `AtlasConfig` (`ATTACHMENTS_MAX_SIZE_MIN = 1_048_576`) and validated/used for the *push* path [7](#0-6) , but no corresponding check exists on the *pull* (attachment download) decode path in this file or in `download.rs`/`getattachment.rs`.

The actual size ceiling on this specific path is whatever general HTTP body/message-length limit is enforced earlier in `httpcore.rs` when parsing the response preamble/body (matches for message-length limiting logic exist there), not the Atlas-specific `attachments_max_size` value. I was not able to confirm the exact numeric value of that generic HTTP cap within the available tool budget, so I cannot state whether the achievable oversize is "arbitrarily large" (unbounded) or merely "larger than the configured 1 MB Atlas limit but still bounded by a coarser HTTP-layer cap".

### Impact Explanation
A malicious or compromised outbound-sync peer that a node queries for an attachment (identified only by content hash, discovered via legitimately gossiped `AttachmentInstance`s) can respond to a single `GET /v2/attachments/<hash>` request with a body whose decoded content size exceeds the node's configured `attachments_max_size`. This content is stored via `insert_instantiated_attachment` into the requester's AtlasDB and briefly held in an in-process `HashSet`, inflating memory and disk usage beyond the configured/expected bound on a read/sync path that has no per-request size gate matching the one used on the write (push) path. This is a bounded-but-uncapped-by-Atlas-policy compute/memory cost on a background sync mechanism rather than a crash, so it best matches the "High — bounded compute DoS on a read endpoint" category rather than "Critical — unauthenticated allocation of arbitrarily large content," since the outer HTTP layer still imposes some ceiling (unconfirmed exact value).

### Likelihood Explanation
The attacker only needs to be selected as one of the node's `outbound_sync_peers` for the relevant `AttachmentsBatch` and respond to the resulting `GET` request — no secret, signature, or privileged role is required [8](#0-7) . This is remotely reachable over the node's RPC/data URL and repeatable on every attachment fetch cycle. Preconditions: the node must have an outstanding `AttachmentInstance` referencing the attacker-controlled peer as a data source (achievable by gossiping a BNS-related transaction that creates such an instance, or the attacker simply being one of the peers queried for a legitimately-referenced hash).

### Recommendation
In `GetAttachmentResponse::deserialize` (or in `decode_atlas_get_attachment`/`extend_with_attachments`), enforce a hard length check on the hex-decoded byte length against `AtlasConfig.attachments_max_size` before constructing `Attachment::new(bytes)`, mirroring the check `should_keep_attachment` performs on the push path, and reject/drop responses (marking the peer as faulty) that exceed it.

### Proof of Concept
Rust net test plan:
1. Build a `StacksHttpResponse` (200 OK, JSON body) whose body is `{"content": "<hex string decoding to e.g. 4 MB of bytes>"}` matching the `GetAttachmentResponse` serialize format (a bare hex string per `Serialize for GetAttachmentResponse` at `stackslib/src/net/atlas/mod.rs:62-67`).
2. Call `response.decode_atlas_get_attachment()` (`stackslib/src/net/api/getattachment.rs:159`) and assert it succeeds with `resp.attachment.content.len() > atlas_config.attachments_max_size` (no `Err` is returned despite exceeding the configured max).
3. Feed the result through `AttachmentsBatchStateContext::extend_with_attachments` (`stackslib/src/net/atlas/download.rs:530-558`) and confirm the oversized `Attachment` lands in `context.attachments` unfiltered.
4. Drive `AttachmentsDownloader::run`'s `Done` branch (or directly call `atlasdb.insert_instantiated_attachment(&attachment)`) and assert the oversized row is persisted, with no error/rejection tied to `attachments_max_size`.

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

**File:** stackslib/src/net/atlas/mod.rs (L91-145)
```rust
#[derive(Debug, Clone, PartialEq)]
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
}
```

**File:** stackslib/src/net/api/getattachment.rs (L159-165)
```rust
    pub fn decode_atlas_get_attachment(self) -> Result<GetAttachmentResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: GetAttachmentResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
    }
```

**File:** stackslib/src/net/atlas/download.rs (L115-146)
```rust
                let mut peers = HashMap::new();
                for peer in network.get_outbound_sync_peers() {
                    if let Some(peer_url) = network.get_data_url(&peer) {
                        let report = match self.reliability_reports.get(&peer_url) {
                            Some(report) => report.clone(),
                            None => ReliabilityReport::empty(),
                        };
                        peers.insert(peer_url, report);
                    }
                }
                if peers.is_empty() {
                    warn!("Atlas: could not get a peer to sync with");
                    // Nothing can be done!
                    return Ok((vec![], vec![]));
                }

                let attachments_batch = match self.pop_next_ready_batch() {
                    Some(ready_batch) => ready_batch,
                    None => {
                        // unreachable
                        warn!("BUG: Atlas; no batch ready although logic checking for ready batches found one");
                        return Ok((vec![], vec![]));
                    }
                };

                let ctx = AttachmentsBatchStateContext::new(
                    attachments_batch,
                    peers,
                    &network.connection_opts,
                );
                AttachmentsBatchStateMachine::new(ctx)
            }
```

**File:** stackslib/src/net/atlas/download.rs (L153-165)
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
                    for attachment_instance in attachments_instances.into_iter() {
                        resolved_attachments.push((attachment_instance, attachment.clone()));
                    }
```

**File:** stackslib/src/net/atlas/download.rs (L547-548)
```rust
            if let Ok(response) = response.decode_atlas_get_attachment() {
                self.attachments.insert(response.attachment);
```
