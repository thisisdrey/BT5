### Title
Attacker-controlled eviction ordering of uninstantiated attachments causes permanent loss of legitimate attachment content - (File: stackslib/src/net/atlas/db.rs)

### Summary
`insert_uninstantiated_attachment` calls `evict_k_oldest_uninstantiated_attachments` whenever the `attachments` table (rows with `was_instantiated = 0`) exceeds `max_uninstantiated_attachments`, evicting strictly by `created_at ASC` with no regard for which attachments are about to be referenced by a soon-to-be-confirmed `AttachmentInstance`. Any remote, unauthenticated caller of the `POST /v2/transactions` RPC endpoint with a `ContractCall` payload and an attachment can flood the node with attacker-chosen attachments to push the oldest (potentially legitimate) entries out of the bounded cache.

### Finding Description
`insert_uninstantiated_attachment` (stackslib/src/net/atlas/db.rs:511-536) checks `count_uninstantiated_attachments()` against `atlas_config.max_uninstantiated_attachments` and, if exceeded, calls `evict_k_oldest_uninstantiated_attachments` (db.rs:538-547), which runs:
`DELETE FROM attachments WHERE hash IN (SELECT hash FROM attachments WHERE was_instantiated = 0 ORDER BY created_at ASC LIMIT ?)`.
This is a pure FIFO eviction keyed only on insertion time, with no distinction between attacker-submitted content and content that legitimately corresponds to an attachment a node cares about (e.g., a BNS name registration zonefile that will soon be confirmed on-chain).

`insert_uninstantiated_attachment` is reachable by any remote peer via the `POST /v2/transactions` RPC handler (stackslib/src/net/api/posttransaction.rs:230-251), which, for any `ContractCall` transaction with an attached blob passing `should_keep_attachment`, stores the attachment as "uninstantiated" before any corresponding on-chain confirmation exists. This endpoint requires no privileged secret or key — it's a standard, remotely reachable RPC call open to any client that can submit a well-formed transaction + attachment pair.

When a real `AttachmentInstance` is later confirmed on-chain and processed, `check_attachment_instances` (download.rs:227-286) calls `atlas_db.find_uninstantiated_attachment(&attachment_instance.content_hash)` (db.rs:594-604) to see if the content is already "inboxed." If the legitimate attachment's row was evicted earlier by an attacker's flood of newer/older inserts, `find_uninstantiated_attachment` returns `None`, and the instance falls through to the "unknown" branch, causing the node to re-queue a full peer download for content it previously already had locally — and if that download also fails/expires, the attachment is treated as unavailable indefinitely.

Existing guards do not prevent this: there's no per-source rate limit or reservation scheme distinguishing attacker-submitted uninstantiated attachments from those about to be confirmed, and the eviction SQL has no knowledge of pending on-chain commitments.

### Impact Explanation
This lets a remote, unprivileged party degrade Atlas/BNS attachment availability for other users' legitimate content: a valid attachment that the node previously possessed is evicted from its "inboxed" cache purely due to attacker-controlled timing/volume of unrelated attachment submissions, forcing a redundant/failing peer-download cycle for content that should have been resolved locally. This matches the "attachment/BNS mismatch" / degraded-serving category — legitimate content is treated as missing even though the node once held it, an availability regression rather than a fabricated commitment. It's repeatable, since the attacker can continuously submit fresh attachments to keep evicting the FIFO queue.

### Likelihood Explanation
The attacker needs no privileges: any remote client can call `POST /v2/transactions` with distinct `ContractCall` transactions plus attachment payloads (subject to `should_keep_attachment` and `attachments_max_size`), at a cost of `max_uninstantiated_attachments` (default cache bound) submissions to fully evict the current cache contents. This does not require breaking any signature check, secret, or consensus rule — it only exploits the deterministic FIFO eviction policy. The main variable is timing: the attacker must race their submissions before the legitimate `AttachmentInstance` confirmation is processed by `check_attachment_instances`, which is plausible given on-chain confirmation delay versus RPC submission speed.

### Recommendation
Change the eviction policy in `evict_k_oldest_uninstantiated_attachments`/`insert_uninstantiated_attachment` to avoid pure insertion-time FIFO eviction of content that is about to be referenced: e.g., cross-reference the `attachment_instances` table to avoid evicting a hash for which an already-known (even if unresolved) `AttachmentInstance` exists, or rate-limit/quota `insert_uninstantiated_attachment` insertions per remote source, or increase `max_uninstantiated_attachments` handling to prioritize retention of attachments matching pending `attachment_instances`.

### Proof of Concept
```rust
// stackslib/src/net/atlas/tests.rs (net test plan)
#[test]
fn test_attacker_evicts_legitimate_uninstantiated_attachment() {
    let atlas_config = AtlasConfig {
        contracts: HashSet::new(),
        attachments_max_size: 1024,
        max_uninstantiated_attachments: 10,
        uninstantiated_attachments_expire_after: 3600,
        unresolved_attachment_instances_expire_after: 3600,
        genesis_attachments: None,
    };
    let mut atlas_db = AtlasDB::connect_memory(atlas_config).unwrap();

    // Legitimate attachment inserted first (e.g. via POST /v2/transactions)
    let legit = new_attachment_from("legitzonefile");
    atlas_db.insert_uninstantiated_attachment(&legit).unwrap();

    // Attacker floods with `max_uninstantiated_attachments` distinct attachments
    for i in 0..10 {
        let attacker_attachment = new_attachment_from(&format!("attacker{i}"));
        atlas_db.insert_uninstantiated_attachment(&attacker_attachment).unwrap();
    }

    // Simulate later on-chain confirmation resolving the legitimate content_hash
    let resolved = atlas_db.find_uninstantiated_attachment(&legit.hash()).unwrap();
    // Assert: legitimate attachment content is gone, forcing full peer re-download
    assert!(resolved.is_none());
}
```
This demonstrates that `find_uninstantiated_attachment` (db.rs:594) returns `None` for the legitimately-inserted attachment after attacker-controlled inserts trigger `evict_k_oldest_uninstantiated_attachments` (db.rs:538-547), matching the flow used in `check_attachment_instances` (download.rs:246-263) when resolving confirmed `AttachmentInstance`s. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** stackslib/src/net/atlas/db.rs (L511-547)
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

    pub fn evict_k_oldest_uninstantiated_attachments(&mut self, k: u32) -> Result<(), db_error> {
        let tx = self.tx_begin()?;
        let res = tx.execute(
            "DELETE FROM attachments WHERE hash IN (SELECT hash FROM attachments WHERE was_instantiated = 0 ORDER BY created_at ASC LIMIT ?)",
            params![k],
        );
        res.map_err(db_error::SqliteError)?;
        tx.commit().map_err(db_error::SqliteError)?;
        Ok(())
    }
```

**File:** stackslib/src/net/atlas/db.rs (L594-604)
```rust
    pub fn find_uninstantiated_attachment(
        &mut self,
        content_hash: &Hash160,
    ) -> Result<Option<Attachment>, db_error> {
        let hex_content_hash = to_hex(&content_hash.0[..]);
        let qry = "SELECT content, hash FROM attachments WHERE hash = ?1 AND was_instantiated = 0"
            .to_string();
        let args = params![hex_content_hash];
        let row = query_row::<Attachment, _>(&self.conn, &qry, args)?;
        Ok(row)
    }
```

**File:** stackslib/src/net/api/posttransaction.rs (L230-251)
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
