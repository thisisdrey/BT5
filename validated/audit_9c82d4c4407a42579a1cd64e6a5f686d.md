### Title
Unbounded attachment content size on the download path bypasses `attachments_max_size` before persisting to `AtlasDB` - ([File: stackslib/src/net/atlas/db.rs])

### Summary
`AtlasDB::insert_instantiated_attachment` writes `attachment.content` directly into the `attachments` BLOB column with no size check at all, and the only place in the codebase that enforces `AtlasConfig::attachments_max_size` (`AtlasDB::should_keep_attachment`) is wired into the `POST /v2/transactions` contract-call attachment path, not into the peer-attachment-download path (`stackslib/src/net/atlas/download.rs`). A malicious peer serving `/v2/attachments/{hash}` can therefore return content that correctly hashes to the requested `content_hash` but is arbitrarily larger than `attachments_max_size`, and the node will store it unbounded.

### Finding Description
`insert_instantiated_attachment` performs a raw `INSERT OR REPLACE INTO attachments (hash, content, ...)` with `attachment.content` as a bound parameter and no length check: [1](#0-0) 

The only size gate that exists, `should_keep_attachment`, checks `attachment.content.len() as u32 > self.atlas_config.attachments_max_size`: [2](#0-1) 

But this gate is only invoked from the `POST /v2/transactions` handler when an attachment accompanies a contract-call transaction: [3](#0-2) 

The peer-driven attachment download/resolution path in `stackslib/src/net/atlas/download.rs`, which fetches content via `GetAttachmentResponse` from `/v2/attachments/{hash}` and then calls `insert_instantiated_attachment` directly (both in `AttachmentsDownloader::run` and in `check_attachment_instances`), never calls `should_keep_attachment` or otherwise checks `attachments_max_size`: [4](#0-3) [5](#0-4) 

A repo-wide search for `attachments_max_size` confirms it is only referenced in `mod.rs` (definition/validate), `config/mod.rs`, `Stacks.toml`, `tests.rs`, and the single `should_keep_attachment` use in `db.rs` — it is absent from `download.rs`. The `GetAttachmentResponse` deserializer itself does no size bound either; it hex-decodes whatever content the peer sends into an `Attachment`: [6](#0-5) 

Since `Attachment::hash()` is just `Hash160::from_data(&self.content)`, a malicious peer can trivially serve any oversized blob whose hash matches a `content_hash` the victim node is looking for (the victim only asks for content matching a hash it already expects from an on-chain attachment instance, but the size of that content is attacker-controlled and unchecked before storage).

### Impact Explanation
A remote, unauthenticated peer that only needs to be selected as a download source for an `AttachmentInstance` (any peer offering the attachment in its inventory) can return oversized attachment content that will be persisted verbatim to the node's local `attachments` SQLite table via `insert_instantiated_attachment`, with no size cap enforced on this path. This is an unauthenticated write of oversized attacker-controlled data into node-local persistent state, and it can be repeated for every attachment instance the node is trying to resolve, leading to disk-storage exhaustion on the victim node from a single malicious/peer-controlled data source.

### Likelihood Explanation
Any node running Atlas attachment sync will fetch attachment instances it observes on-chain from whatever peer advertises them in its inventory; no special role, secret, or privileged connection is required — the attacker just needs to be selected as a download source (e.g., by being one of the peer's outbound sync peers advertising the relevant attachment in its inventory bitmap). The attacker's cost per oversized attachment is a single hex-encoded HTTP response body, remotely triggerable and repeatable for each attachment instance being resolved.

### Recommendation
Enforce `attachments_max_size` uniformly wherever attachment content is accepted for storage, not just in the mempool/contract-call path. Specifically, call `AtlasDB::should_keep_attachment` (or an equivalent size check against `atlas_config.attachments_max_size`) inside `AttachmentsDownloader::run` and `check_attachment_instances` in `stackslib/src/net/atlas/download.rs` before invoking `insert_instantiated_attachment`, and/or add a length assertion directly inside `AtlasDB::insert_instantiated_attachment` in `stackslib/src/net/atlas/db.rs` so the bound is enforced at the single point of persistence regardless of caller.

### Proof of Concept
Rust test plan (net test, in `stackslib/src/net/atlas/tests.rs` or a new download-focused test module):
1. Build an `AtlasConfig` with `attachments_max_size` set to the allowed minimum (`ATTACHMENTS_MAX_SIZE_MIN = 1_048_576`) or, for a unit-level test bypassing the `validate()` floor, construct `AtlasConfig` directly with a small custom value (as done in existing tests like `test_keep_uninstantiated_attachments`, which sets `attachments_max_size: 16`).
2. Construct an `Attachment` whose `content` is larger than `attachments_max_size` (e.g., a `Vec<u8>` of length 1000 when `attachments_max_size = 16`), compute `attachment.hash()`.
3. Insert a matching `AttachmentInstance` with `content_hash = attachment.hash()` via `insert_initial_attachment_instance`/`queue_attachment_instance`.
4. Simulate the download-resolution path: call `AtlasDB::insert_instantiated_attachment(&attachment)` directly (mirroring what `download.rs::run` and `check_attachment_instances` do) and assert it succeeds (`Ok(())`) even though `attachment.content.len() as u32 > atlas_config.attachments_max_size` — demonstrating the missing guard.
5. Contrast with `AtlasDB::should_keep_attachment(&contract_id, &attachment)` returning `false` for the same oversized attachment, proving the enforcement exists in one code path (`posttransaction.rs`) but is bypassed by `insert_instantiated_attachment`/`download.rs`.
6. Assertion for the fix: after remediation, `insert_instantiated_attachment` (or its caller in `download.rs`) should return an error / skip storage when `attachment.content.len() as u32 > self.atlas_config.attachments_max_size`, so the oversized BLOB is never written to `attachments`.

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

**File:** stackslib/src/net/api/posttransaction.rs (L230-250)
```rust
            // store attachment as well, if it's part of a contract-call
            if let Some(ref attachment) = attachment_opt {
                if let TransactionPayload::ContractCall(ref contract_call) = tx.payload {
                    if network
                        .get_atlasdb()
                        .should_keep_attachment(&contract_call.to_clarity_contract_id(), attachment)
                    {
                        network
                            .get_atlasdb_mut()
                            .insert_uninstantiated_attachment(attachment)
                            .map_err(|e| {
                                StacksHttpResponse::new_error(
                                    &preamble,
                                    &HttpServerError::new(format!(
                                        "Failed to store contract-call attachment: {:?}",
                                        &e
                                    )),
                                )
                            })?;
                    }
                }
```

**File:** stackslib/src/net/atlas/download.rs (L153-169)
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
                    context
                        .attachments_batch
                        .resolve_attachment(&attachment.hash())
                }
```

**File:** stackslib/src/net/atlas/download.rs (L246-263)
```rust
            } else if let Ok(Some(entry)) =
                atlas_db.find_attachment(&attachment_instance.content_hash)
            {
                // Do we already have a matching validated attachment
                do_if_found(atlas_db, &attachment_instance)?;
                debug!(
                    "Atlas: inserting and pairing new attachment instance to existing attachment"
                );
                resolved_attachments.push((attachment_instance, entry));
            } else if let Ok(Some(attachment)) =
                atlas_db.find_uninstantiated_attachment(&attachment_instance.content_hash)
            {
                // Do we already have a matching inboxed attachment
                atlas_db.insert_instantiated_attachment(&attachment)?;
                do_if_found(atlas_db, &attachment_instance)?;
                debug!("Atlas: inserting and pairing new attachment instance to inboxed attachment, now validated");
                resolved_attachments.push((attachment_instance, attachment));
            } else {
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
