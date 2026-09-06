### Title
Slot version reset on StackerDB reconfiguration enables replay of stale-but-validly-signed chunks by a former signer - (File: `stackslib/src/net/stackerdb/db.rs`)

### Summary
`StackerDBTx::reconfigure_stackerdb` resets a slot's version counter to `NO_VERSION` (0) whenever the signer assigned to that slot changes, and only skips the reset when the signer is unchanged. [1](#0-0)  Because the version baseline is reset independently for each principal change, a signer address that is removed from a slot and later reassigned to that same slot (e.g. across reward-cycle/config changes) causes the local replica's version counter to drop back to 0 even though that signer previously issued signed chunks at much higher versions. This mirrors the reported wallet-key-reuse bug class: a signature/state that was valid, then made stale (superseded) by newer state, can become "valid again" once a boundary value (nonce/version) is rolled back, allowing replay of a previously superseded but validly-signed payload.

### Finding Description
`try_replace_chunk` (and `validate_received_chunk` in `stackslib/src/net/stackerdb/mod.rs`) accept a submitted chunk only if `slot_desc.slot_version` is strictly greater than the currently stored `slot_validation.version`, and if `slot_desc.verify(&slot_validation.signer)` succeeds. [2](#0-1)  The signer/version pair recorded in the `chunks` table is the sole state used to reject stale or unauthorized chunks.

When a StackerDB is reconfigured (driven by the smart contract's signer-slot list), `reconfigure_stackerdb` walks each slot and only preserves the existing version/signature if `existing_validation.signer == *principal`; otherwise it overwrites the row with `NO_VERSION`, an empty signature, and empty data. [1](#0-0)  This reset happens per-slot based solely on whether the *current* principal differs from the *previous* principal in that slot — it does not track whether that principal was ever assigned to the slot before with a higher version.

Consequently, if address `A` is removed from slot `S` (version resets to 0) and later re-added to slot `S` (again resets to 0, since the immediately-prior signer was some other party), the local replica's minimum acceptable version for `A`'s signature on slot `S` is 0. Any old, previously-superseded, but validly-signed-by-`A` chunk (e.g., version 5, with content that was already overwritten and is now stale) again satisfies `slot_version > slot_validation.version` (5 > 0) and passes `verify()`, since `A`'s key and signature scheme did not change. That old chunk can be pushed or synced back in via `validate_received_chunk`/`try_replace_chunk`, overwriting the current legitimate chunk with stale content.

### Impact Explanation
This breaks the append-only "Lamport clock" invariant the StackerDB design relies on: the same signer's earlier, already-superseded chunk can be re-accepted as "newer" purely because of local reconfiguration bookkeeping, not because it is actually newer. This allows an unprivileged remote peer (anyone who saved an old signed chunk from a legitimate signer) to overwrite current StackerDB replica state with stale data after a slot reassignment round-trips back to the same address — a form of forged/stale-data propagation into the network's StackerDB replication path (`stackslib/src/net/stackerdb/mod.rs`'s `validate_received_chunk`/push-chunk handling). This matches the "serving non-canonical state as canonical" / unauthenticated write-to-state impact class.

### Likelihood Explanation
This requires a StackerDB reconfiguration event where the same signer address is removed from and later reinstated to the same slot index — plausible in signer-set churn between reward cycles or contract-driven config changes — combined with an attacker retaining an old, previously valid, superseded chunk signed by that address. It does not require any privileged access, node secret, or another party's key; only patience and access to previously-broadcast (now stale) signed data. It is a genuine, if situational, path, distinct from the general append-only assumption StackerDB documentation states.

### Recommendation
Track a monotonic per-slot version watermark that is never decreased on reconfiguration (i.e., persist the historical maximum version ever observed per slot, independent of signer identity, or maintain a strictly increasing global slot version regardless of signer reassignment). Alternatively, when a signer is reassigned to a slot it previously held, do not reset the version below the highest version previously recorded for that slot. Add regression tests that reassign a signer back to a previously-held slot and confirm old chunks from that signer cannot be replayed.

### Proof of Concept
1. Create a StackerDB with slot 0 owned by signer `A`; write chunk version 5, signed by `A` (`try_replace_chunk` succeeds).
2. Reconfigure the StackerDB so slot 0 is now owned by signer `B` — `reconfigure_stackerdb` resets slot 0 to `NO_VERSION`/empty (signer differs). [1](#0-0) 
3. Reconfigure again so slot 0 is owned by `A` once more — since the immediately-prior signer (`B`) differs from `A`, the row is reset again to `NO_VERSION`. [1](#0-0) 
4. Replay the original chunk (version 5, signed by `A`) via `try_replace_chunk`/the push-chunk RPC path — it passes both the version check (`5 > 0`) and `verify(&A)`, and is accepted, overwriting whatever legitimate chunk `A` may have written after being reinstated. [2](#0-1)

### Citations

**File:** stackslib/src/net/stackerdb/db.rs (L317-345)
```rust
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
