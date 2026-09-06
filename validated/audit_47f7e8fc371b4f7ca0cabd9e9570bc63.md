### Title
Stale StackerDB chunk signatures can be replayed after slot ownership reverts to a prior signer, overwriting reset state as canonical - (File: `stackslib/src/net/stackerdb/db.rs`)

### Summary

### Finding Description
A StackerDB chunk write is authenticated purely by a `SlotMetadata` signature over `(slot_id, slot_version, data_hash)` [1](#0-0) , and a chunk is accepted whenever it is signed by the slot's currently-configured `signer` **and** its `slot_version` is strictly greater than the version currently stored, per `try_replace_chunk`: [2](#0-1) 

The `slot_version` (a Lamport clock) is the only replay-prevention mechanism — there is no separate nonce, and the signed digest does not commit to any epoch/generation identifier for the slot. When a StackerDB is reconfigured (e.g. the signer set changes across reward cycles/contract updates), `reconfigure_stackerdb` resets any slot whose signer address changes back to `NO_VERSION` (empty data, version 0): [3](#0-2) 

Crucially, the reset is only skipped `if existing_validation.signer == *principal` (no change) — if the signer at that slot ever changes away and later changes back to the *same* address, the version is unconditionally reset to 0, even though that address may have previously signed and broadcast chunks with much higher `slot_version` values.

Because StackerDB chunks are ordinary P2P gossip data (`StackerDBPushChunkData`, `StackerDBChunkData`) that gets stored, relayed, and can be observed by any peer, an unprivileged remote party who has recorded one of that signer's previously valid, high-`slot_version` signed chunks can simply resubmit it (via the P2P push path or the `POST /stackerdb/.../chunks` RPC) after the slot has been reset. `validate_received_chunk` / `try_replace_chunk` only check "signed by current signer" and "version > current stored version" — both of which the old message still satisfies: [4](#0-3) 

This mirrors the reported EIP-712 issue exactly: the "seller" (slot) reverting to a state where the old signed message becomes valid again (analogous to Joe repurchasing the NFT) allows the old signature/version pair to be replayed and treated as the latest, canonical state.

### Impact Explanation
This lets a remote, unauthenticated attacker (who only needs to have observed one broadcast chunk in the past — no private key required) reintroduce stale data into a slot after it has been legitimately reset, and have it accepted and re-broadcast as the current/canonical chunk for that slot ID, since it passes both the signature check and the freshness ("newer version") check. This is a "non-canonical state served as canonical" condition (per the accepted High-impact category): stale application data (e.g. an old signer message, old block-inventory hint, or other StackerDB-carried payload) is resurrected and propagated network-wide as the latest state for that slot, and it also blocks the legitimate current signer from writing any version ≤ the replayed one until they bump past it.

### Likelihood Explanation
Requires (1) the attacker to have previously observed/stored a validly-signed chunk from a given slot signer, and (2) that signer's slot to later be reset via `reconfigure_stackerdb` and reassigned back to the same address. Signer set churn across StackerDB reconfiguration events (e.g., contract-driven signer rotation) makes condition (2) plausible over time; StackerDB chunks are routinely gossiped and logged, making condition (1) trivial to satisfy for any observant network participant. No node secret, admin role, or other party's key is needed — this is a pure remote replay against the write-acceptance logic.

### Recommendation
Bind replay prevention to the slot's *generation*, not just its version: when `reconfigure_stackerdb` resets a slot (whether reused by the same or a different signer), persist and monotonically bump a `generation`/`epoch` counter for that slot, and include it in the signed `auth_digest` (`SlotMetadata::auth_digest`) alongside `slot_id`, `slot_version`, and `data_hash`. Reject any chunk whose generation does not match the slot's current generation. This makes any previously signed chunk permanently unusable once the slot is reconfigured, regardless of whether the version counter is reset.

### Proof of Concept
1. Contract `X` configures slot `S` with signer `A`; `A` signs and broadcasts `StackerDBChunkData{slot_id: S, slot_version: 5, data: D}`. Attacker `M` observes this on the wire and stores a copy.
2. The StackerDB is reconfigured (e.g., new reward-cycle signer set) so slot `S`'s signer becomes `B`; `reconfigure_stackerdb` resets slot `S` to `NO_VERSION`/empty since `existing_validation.signer (A) != principal (B)` — see `stackslib/src/net/stackerdb/db.rs:319-346`.
3. The DB is reconfigured again so slot `S`'s signer reverts to `A` (again resetting to version 0 per the same code path, since it is treated as a signer change from `B` back to `A`).
4. `M` replays the original message from step 1 (`slot_version: 5`, signed by `A`) via the P2P push path or `POST /stackerdb/.../chunks`.
5. `try_replace_chunk` verifies the signature against the now-current signer `A` (passes), and checks `5 <= 0` (false, not stale) — the chunk is accepted and stored/broadcast as slot `S`'s latest chunk, even though it is stale data from before the reconfiguration. See acceptance logic at `stackslib/src/net/stackerdb/db.rs:411-429` and `stackslib/src/net/stackerdb/mod.rs:679-706`.

### Citations

**File:** libstackerdb/src/libstackerdb.rs (L159-166)
```rust
    /// Get the digest to sign that authenticates this chunk data and metadata
    fn auth_digest(&self) -> Sha512Trunc256Sum {
        let mut hasher = Sha512_256::new();
        hasher.update(self.slot_id.to_be_bytes());
        hasher.update(self.slot_version.to_be_bytes());
        hasher.update(self.data_hash.0);
        Sha512Trunc256Sum::from_hasher(hasher)
    }
```

**File:** stackslib/src/net/stackerdb/db.rs (L298-346)
```rust
    /// Update a database's storage slots, e.g. from new configuration state in its smart contract.
    /// Chunk data for slots that no longer exist will be dropped.
    /// Newly-created slots will be instantiated with empty data.
    /// If the address for a slot changes, then its data will be dropped.
    pub fn reconfigure_stackerdb(
        &self,
        smart_contract: &QualifiedContractIdentifier,
        slots: &[(StacksAddress, u32)],
    ) -> Result<(), net_error> {
        let stackerdb_id = self.get_stackerdb_id(smart_contract)?;
        let mut total_slots_read = 0u32;
        for (principal, slot_count) in slots.iter() {
            total_slots_read =
                total_slots_read
                    .checked_add(*slot_count)
                    .ok_or(net_error::OverflowError(
                        "Slot count exceeeds u32::MAX".to_string(),
                    ))?;
            let slots_before_principal = total_slots_read - slot_count;
            for cur_principal_slot in 0..*slot_count {
                let slot_id = slots_before_principal + cur_principal_slot;
                if let Some(existing_validation) =
                    self.get_slot_validation(smart_contract, slot_id)?
                {
                    // this slot already exists.
                    if existing_validation.signer == *principal {
                        // no change
                        continue;
                    }
                }

                debug!("Reset slot {} of {}", slot_id, smart_contract);

                // new slot, or existing slot with a different signer
                let qry = "INSERT OR REPLACE INTO chunks (stackerdb_id,signer,slot_id,version,write_time,data,data_hash,signature) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)";
                let mut stmt = self.sql_tx.prepare(qry)?;
                let args = params![
                    stackerdb_id,
                    principal.to_string(),
                    slot_id,
                    NO_VERSION,
                    0,
                    vec![],
                    Sha512Trunc256Sum([0u8; 32]),
                    MessageSignature::empty(),
                ];

                stmt.execute(args)?;
            }
```

**File:** stackslib/src/net/stackerdb/db.rs (L411-429)
```rust
        let slot_validation = self
            .get_slot_validation(smart_contract, slot_desc.slot_id)?
            .ok_or(net_error::NoSuchSlot(
                smart_contract.clone(),
                slot_desc.slot_id,
            ))?;

        if !slot_desc.verify(&slot_validation.signer)? {
            return Err(net_error::BadSlotSigner(
                slot_validation.signer,
                slot_desc.slot_id,
            ));
        }
        if slot_desc.slot_version <= slot_validation.version {
            return Err(net_error::StaleChunk {
                supplied_version: slot_desc.slot_version,
                latest_version: slot_validation.version,
            });
        }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L679-706)
```rust
        // validate -- must be signed by the expected author
        let addr = match self
            .stackerdbs
            .get_slot_signer(smart_contract_id, data.slot_id)?
        {
            Some(addr) => addr,
            None => {
                return Ok(false);
            }
        };

        let slot_metadata = data.get_slot_metadata();
        if !slot_metadata.verify(&addr)? {
            info!(
                "StackerDBChunk for {} ID {} is not signed by {}",
                smart_contract_id, data.slot_id, &addr
            );
            return Ok(false);
        }

        // validate -- must be the current or newer version
        if data.slot_version < *expected_version {
            info!(
                "Received StackerDBChunk for {} ID {} version {}, which is stale (expected {})",
                smart_contract_id, data.slot_id, data.slot_version, *expected_version
            );
            return Ok(false);
        }
```
