### Title
Unprivileged attackers can starve `AtlasDB`'s uninstantiated-attachment cache via `POST /v2/transactions`, evicting legitimate pending BNS zonefiles before their `AttachmentInstance` resolves - (File: stackslib/src/net/atlas/db.rs)

### Summary
`AtlasDB::insert_uninstantiated_attachment` evicts the oldest `was_instantiated=0` rows purely by `created_at` once the table hits `AtlasConfig.max_uninstantiated_attachments`, with no check for whether the evicted attachment has a matching, still-outstanding `attachment_instances` row. Any remote party can reach this path by POSTing contract-call transactions with attachments to `/v2/transactions`, letting them displace a legitimate, about-to-be-confirmed BNS zonefile from the cache.

### Finding Description
`RPCPostTransactionRequestHandler::try_handle_request` accepts a `ContractCall` transaction plus an optional `attachment`, and if `AtlasDB::should_keep_attachment` (contract in `AtlasConfig.contracts`, e.g. the `bns` contract, and size ≤ `attachments_max_size`) returns true, it calls `network.get_atlasdb_mut().insert_uninstantiated_attachment(attachment)` [1](#0-0) . This runs after the transaction merely passes `mempool.submit(...)` - a syntactic/fee/nonce check, not proof that the call corresponds to any real, committed name operation [2](#0-1) .

`insert_uninstantiated_attachment` computes `count_uninstantiated_attachments()` and, once `>= max_uninstantiated_attachments`, calls `evict_k_oldest_uninstantiated_attachments(to_delete)`, which deletes the oldest `was_instantiated = 0` rows ordered strictly by `created_at`, with **no join or check against `attachment_instances`** to see if a row is still referenced by a queued/pending name-registration commitment: [3](#0-2) 

The existing unit test confirms this behavior exactly: once the cache saturates, the *earliest-inserted* attachment is deleted regardless of its significance, purely by insertion order [4](#0-3) .

Because `should_keep_attachment` only gates on contract identity and size (not on whether a corresponding `AttachmentInstance` exists), an attacker can repeatedly submit distinct-hash, attacker-controlled attachments targeting the BNS contract, filling the table and forcing eviction of genuinely pending zonefiles that a real name-registration is relying on for correct BNS resolution once its `AttachmentInstance` is processed.

### Impact Explanation
This causes a legitimate, about-to-be-confirmed BNS zonefile attachment to be evicted from local storage before the matching `AttachmentInstance` (created from an actual on-chain name operation) is checked/serviced, producing an attachment/BNS mismatch on the victim node - matching the specified High-severity category ("attachment/BNS mismatch"). The affected node will be unable to serve that zonefile via the Atlas attachment-fetch protocol even though the corresponding name operation is legitimately confirmed on-chain, and this is repeatable as long as the attacker keeps submitting new attachments.

### Likelihood Explanation
The attacker needs no privileged role or secret - `/v2/transactions` is a public RPC endpoint. However, each attack transaction must pass `mempool.submit` (valid signature, sufficient fee, and a usable nonce, and must be a `ContractCall` to a contract configured in `AtlasConfig.contracts`, effectively the `bns` contract), which imposes a real STX cost and nonce-sequencing per transaction rather than being free/volumetric. `max_uninstantiated_attachments` defaults to a node-configured value (minimum enforced via `MAX_UNINSTANTIATED_ATTACHMENTS_MIN`, default example 10000 in `Stacks.toml`) [5](#0-4) , so filling it requires that many paid, validly-signed transactions - a moderate but non-trivial cost, not mere bandwidth flooding.

### Recommendation
Before evicting an uninstantiated attachment, `evict_k_oldest_uninstantiated_attachments` (or its caller) should exclude/deprioritize rows whose `hash` matches a `content_hash` in `attachment_instances` that is still unresolved (`is_available = 0`), e.g. by adding a `NOT IN (SELECT content_hash FROM attachment_instances WHERE is_available = 0)` predicate to the eviction query, or by tracking pending-instance hashes and skipping them during LRU eviction, falling back to evicting attachments with no matching instance first.

### Proof of Concept
Extend `stackslib/src/net/atlas/tests.rs::test_evict_k_oldest_uninstantiated_attachments`:
1. Configure `AtlasDB` with a small `max_uninstantiated_attachments` (e.g. 3).
2. Insert one legitimate attachment (`legit`) via `insert_uninstantiated_attachment`, and insert a matching row into `attachment_instances` with `is_available = 0` and `content_hash = legit.hash()` (simulating a queued, not-yet-resolved BNS name registration).
3. Call `insert_uninstantiated_attachment` with `max_uninstantiated_attachments` additional attacker-only attachments (distinct hashes, no matching `attachment_instances` row).
4. Assert `atlas_db.find_uninstantiated_attachment(&legit.hash()).unwrap().is_none()` while `atlas_db.find_unresolved_attachment_instances().unwrap()` still contains the `legit` instance - demonstrating the legitimate pending attachment was evicted purely by FIFO order despite an outstanding on-chain commitment, per the eviction logic at `stackslib/src/net/atlas/db.rs:538-547`.

### Citations

**File:** stackslib/src/net/api/posttransaction.rs (L211-228)
```rust
            let stacks_tip = self.get_stacks_chain_tip(&preamble, sortdb, chainstate)?;

            // accept to mempool
            if let Err(e) = mempool.submit(
                chainstate,
                sortdb,
                &stacks_tip.consensus_hash,
                &stacks_tip.anchored_header.block_hash(),
                &tx,
                event_observer,
                &stacks_epoch.block_limit,
                &stacks_epoch.epoch_id,
            ) {
                return Err(StacksHttpResponse::new_error(
                    &preamble,
                    &HttpBadRequest::new_json(e.into_json(&txid)),
                ));
            };
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

**File:** stackslib/src/net/atlas/tests.rs (L889-976)
```rust
#[test]
fn test_evict_k_oldest_uninstantiated_attachments() {
    let atlas_config = AtlasConfig {
        contracts: HashSet::new(),
        attachments_max_size: 1024,
        max_uninstantiated_attachments: 10,
        uninstantiated_attachments_expire_after: 0,
        unresolved_attachment_instances_expire_after: 10,
        genesis_attachments: None,
    };

    let mut atlas_db = AtlasDB::connect_memory(atlas_config).unwrap();

    atlas_db
        .insert_uninstantiated_attachment(&new_attachment_from("facade00"))
        .unwrap();
    assert_eq!(atlas_db.count_uninstantiated_attachments().unwrap(), 1);

    atlas_db
        .insert_uninstantiated_attachment(&new_attachment_from("facade01"))
        .unwrap();
    assert_eq!(atlas_db.count_uninstantiated_attachments().unwrap(), 2);

    atlas_db
        .insert_uninstantiated_attachment(&new_attachment_from("facade02"))
        .unwrap();
    assert_eq!(atlas_db.count_uninstantiated_attachments().unwrap(), 3);

    atlas_db
        .insert_uninstantiated_attachment(&new_attachment_from("facade02"))
        .unwrap();
    assert_eq!(atlas_db.count_uninstantiated_attachments().unwrap(), 3);

    atlas_db
        .insert_uninstantiated_attachment(&new_attachment_from("facade03"))
        .unwrap();
    assert_eq!(atlas_db.count_uninstantiated_attachments().unwrap(), 4);

    atlas_db
        .insert_uninstantiated_attachment(&new_attachment_from("facade04"))
        .unwrap();
    assert_eq!(atlas_db.count_uninstantiated_attachments().unwrap(), 5);

    atlas_db
        .insert_uninstantiated_attachment(&new_attachment_from("facade05"))
        .unwrap();
    assert_eq!(atlas_db.count_uninstantiated_attachments().unwrap(), 6);

    atlas_db
        .insert_uninstantiated_attachment(&new_attachment_from("facade06"))
        .unwrap();
    assert_eq!(atlas_db.count_uninstantiated_attachments().unwrap(), 7);

    atlas_db
        .insert_uninstantiated_attachment(&new_attachment_from("facade07"))
        .unwrap();
    assert_eq!(atlas_db.count_uninstantiated_attachments().unwrap(), 8);

    atlas_db
        .insert_uninstantiated_attachment(&new_attachment_from("facade08"))
        .unwrap();
    assert_eq!(atlas_db.count_uninstantiated_attachments().unwrap(), 9);

    atlas_db
        .insert_uninstantiated_attachment(&new_attachment_from("facade09"))
        .unwrap();
    assert_eq!(atlas_db.count_uninstantiated_attachments().unwrap(), 10);

    atlas_db
        .insert_uninstantiated_attachment(&new_attachment_from("facade10"))
        .unwrap();
    // We reached `max_uninstantiated_attachments`. Eviction should start kicking in
    assert_eq!(atlas_db.count_uninstantiated_attachments().unwrap(), 10);
    // The latest attachment inserted should be available
    assert!(atlas_db
        .find_uninstantiated_attachment(&new_attachment_from("facade10").hash())
        .unwrap()
        .is_some());
    // The first attachment inserted should be gone
    assert!(atlas_db
        .find_uninstantiated_attachment(&new_attachment_from("facade00").hash())
        .unwrap()
        .is_none());
    // The second attachment inserted should be available
    assert!(atlas_db
        .find_uninstantiated_attachment(&new_attachment_from("facade01").hash())
        .unwrap()
        .is_some());
```

**File:** stacks-node/Stacks.toml (L93-96)
```text
#[atlas]
#attachments_max_size = 1048576
#max_uninstantiated_attachments = 10000
#uninstantiated_attachments_expire_after = 3600
```
