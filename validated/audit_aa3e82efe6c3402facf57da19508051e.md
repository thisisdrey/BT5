### Title
Cheap `max_writes` bounds check runs after expensive ECDSA signature recovery in `validate_received_chunk` - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
In `PeerNetwork::validate_received_chunk`, the check `data.slot_version > config.max_writes` (a trivial integer comparison) is performed *after* `slot_metadata.verify(&addr)`, which does a full secp256k1 signature recovery. A legitimate slot owner can therefore force the node to pay the cost of ECDSA recovery on every push chunk, even though the chunk is guaranteed to be rejected by the cheap version-bound check.

### Finding Description
`validate_received_chunk` performs its checks in this order: [1](#0-0) 
1. `data.data.len() as u64 > config.chunk_size` (cheap)
2. `expected_versions.get(data.slot_id as usize)` bound check (cheap)
3. `get_slot_signer(...)` (DB lookup)
4. `slot_metadata.verify(&addr)` — expensive secp256k1 recovery [2](#0-1) 
5. `data.slot_version < *expected_version` stale check
6. `data.slot_version > config.max_writes` — cheap comparison, done LAST [3](#0-2) 

This function is invoked directly from the unsolicited P2P push path via `handle_unsolicited_StackerDBPushChunk`, which is reached whenever a peer sends a `StackerDBPushChunk` message. [4](#0-3)  A legitimate slot owner (holder of a valid private key for a slot they are authorized to write) can craft a small, well-formed, correctly-signed `StackerDBChunkData` with `slot_version` set arbitrarily high (e.g. far beyond `config.max_writes`), guaranteeing rejection at step 6 — but only after the node has already paid the cost of step 4's signature recovery. Repeating this arbitrarily often forces the node to spend CPU on cryptographic verification for chunks that can never be stored.

### Impact Explanation
Each malicious push costs the receiving node one ECDSA-recovery operation for zero possibility of a stored write, since rejection is deterministic and known in advance to the attacker. This is a bounded, repeatable, per-connection CPU cost imposed on the node by a party who holds only a legitimately-owned slot key (no privileged role required). It does not cause a crash, does not forge or store any state, and does not affect chain-tip resolution — it is purely a resource-ordering inefficiency that lets an authenticated-but-adversarial slot owner waste CPU cycles the node otherwise would have saved via a cheap early-exit.

### Likelihood Explanation
Preconditions: attacker must be a legitimate signer/owner of at least one slot in a StackerDB the target node replicates (satisfies the "unprivileged" definition in scope, since owning a slot is allowed). The attacker needs no special config or admin access, and reaches the code purely by sending `StackerDBPushChunk` messages over an established P2P connection. Sending such chunks is cheap for the attacker (just sign a small payload), and the check ordering makes every push cost the victim a full signature verification regardless of outcome. This is repeatable as long as the connection remains open and bandwidth/backpressure limits permit sending frames.

### Recommendation
Reorder the checks in `validate_received_chunk` so that all cheap, purely-local bounds checks (`data.data.len() > config.chunk_size`, `expected_versions` bound check, `data.slot_version < *expected_version`, and `data.slot_version > config.max_writes`) are performed *before* the expensive `slot_metadata.verify(&addr)` call. Since `max_writes` and `expected_version` do not depend on the signature, checking them first allows immediate rejection of any push chunk whose version is out of range without incurring the cost of the secp256k1 recovery.

### Proof of Concept
Add a benchmark-style test in `stackslib/src/net/stackerdb/tests` (or a new test module) that:
1. Sets up a `StackerDBConfig` with a small `max_writes` (e.g. `10`) and a single signer/slot with a known keypair.
2. Constructs N `StackerDBChunkData` instances signed correctly by the slot owner's key, each with `slot_version = u32::MAX` (or any value `> max_writes`) and a small payload.
3. Calls `PeerNetwork::validate_received_chunk` N times, timing the total wall-clock/CPU time.
4. Compares against a variant where the `max_writes` check is hoisted before `slot_metadata.verify`, showing that the reordered version rejects each chunk in negligible time (no signature recovery), while the current ordering spends time proportional to N secp256k1 recoveries — asserting `Ok(false)` is returned in both cases but with materially different CPU cost profiles (e.g. via `std::time::Instant` deltas or a criterion benchmark).

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L656-717)
```rust
        // validate -- must not exceed this replica's configured chunk size.
        if (data.data.len() as u64) > config.chunk_size {
            info!(
                "Received StackerDBChunk for {} ID {}, which is oversized: {} bytes (max {} bytes)",
                smart_contract_id,
                data.slot_id,
                data.data.len(),
                config.chunk_size
            );
            return Ok(false);
        }

        // validate -- must be a valid chunk
        let Some(expected_version) = expected_versions.get(data.slot_id as usize) else {
            info!(
                "Received StackerDBChunk for {} ID {}, which is too big ({})",
                smart_contract_id,
                data.slot_id,
                expected_versions.len()
            );
            return Ok(false);
        };

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

        // validate -- must not exceed max writes
        if data.slot_version > config.max_writes {
            info!(
                "Write count exceeded for StackerDBChunk for {} ID {} version {} (max is {})",
                smart_contract_id, data.slot_id, data.slot_version, config.max_writes
            );
            return Ok(false);
        }

        Ok(true)
```

**File:** stackslib/src/net/stackerdb/mod.rs (L784-792)
```rust
                // sanity check
                if !self.validate_received_chunk(
                    &chunk_data.contract_id,
                    stackerdb_config,
                    &chunk_data.chunk_data,
                    &data.slot_versions,
                )? {
                    return Ok((false, false));
                }
```
