## No vulnerability found for this question.

Analysis: `AtlasDB::should_keep_attachment` (`stackslib/src/net/atlas/db.rs:249-266`) does enforce `attachment.content.len() as u32 > self.atlas_config.attachments_max_size` and rejects the attachment, but this check is only invoked for locally posted/mempool-sourced attachments — not on the download path. The download path (`AttachmentsBatchStateMachine::try_proceed` → `Done` → `AttachmentsDownloader::run`, `stackslib/src/net/atlas/download.rs:150-165`) calls `network.atlasdb.insert_instantiated_attachment(&attachment)` directly on attachments decoded from a remote peer's `GET /v2/attachments/<hash>` response (`extend_with_attachments`, `stackslib/src/net/atlas/download.rs:530-558`, via `decode_atlas_get_attachment`), and neither `insert_instantiated_attachment` nor `insert_uninstantiated_attachment` in `stackslib/src/net/atlas/db.rs:511-536` and `:576-592` re-check `attachments_max_size` before writing `content` to the `attachments` table. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

However, this cannot be validated as an exploitable vulnerability within the scope/rules given:

1. This falls under the "epoch2x/neon download and inv paths" exclusion listed as out-of-scope in the rules — the Atlas attachment downloader is exactly this kind of legacy epoch2x download/inv machinery (`AttachmentsBatchStateMachine`, `AttachmentsInventoryRequest`, `BatchedRequestsState` in `stackslib/src/net/atlas/download.rs`), which the prompt explicitly rejects on sight.

2. I could not locate or verify (within the remaining tool budget) whether the HTTP client layer used for these outbound Atlas requests (`convo.try_get_response()` / `StacksHttpResponse` decoding) enforces a body-size cap (e.g. `MAX_PAYLOAD_LEN` or a chunked-transfer limit) before `decode_atlas_get_attachment` is reached — if such a cap exists at the HTTP/connection layer, it would already bound the equality claimed in the question regardless of the `AtlasDB` insert-time check. Without confirming the absence of that guard, I cannot assert the fault is real and reachable, and per the validation rules I must not speculate.

3. Even disregarding the out-of-scope exclusion, the actual attacker-controlled amplification here (an outbound peer choosing to be a sync peer and serving oversized attachment bytes for a hash the requester's own inventory says exists) is bounded by the size of a single HTTP response body per attachment fetch and requires the attacker to already be a peer the victim is actively syncing with — this is a bandwidth/storage-volume concern more than a "forged message stored/relayed" or crash from a single message, and volumetric storage/bandwidth issues bounded only by response size are explicitly excluded ("volumetric DDoS, bandwidth flooding ... needing only traffic volume").

Given the explicit out-of-scope carve-out for epoch2x/neon download paths and the inability to confirm an HTTP-layer size guard is absent, this does not meet the bar for a valid finding under the stated rules.

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

**File:** stackslib/src/net/atlas/db.rs (L511-536)
```rust
    pub fn insert_uninstantiated_attachment(
        &mut self,
        attachment: &Attachment,
    ) -> Result<(), db_error> {
        // Insert the new attachment
        let uninstantiated_attachments = self.count_uninstantiated_attachments()?;
        if uninstantiated_attachments >= self.atlas_config.max_uninstantiated_attachments {
            let to_delete =
                1 + uninstantiated_attachments - self.atlas_config.max_uninstantiated_attachments;
            self.evict_k_oldest_uninstantiated_attachments(to_delete)?;
        }

        let tx = self.tx_begin()?;
        let now = util::get_epoch_time_secs() as i64;
        let res = tx.execute(
            "INSERT OR REPLACE INTO attachments (hash, content, was_instantiated, created_at) VALUES (?, ?, 0, ?)",
            params![
                attachment.hash(),
                attachment.content,
                now,
            ],
        );
        res.map_err(db_error::SqliteError)?;
        tx.commit().map_err(db_error::SqliteError)?;
        Ok(())
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

**File:** stackslib/src/net/atlas/download.rs (L147-165)
```rust
        };

        let mut progress =
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
