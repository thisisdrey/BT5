### Title
Expensive ECDSA signature recovery is performed before cheap, unauthenticated staleness/max-writes checks in `validate_received_chunk`, enabling bounded compute-DoS via crafted StackerDB chunk pushes - (File: `stackslib/src/net/stackerdb/mod.rs`)

### Summary
`PeerNetwork::validate_received_chunk` performs `slot_metadata.verify(&addr)` (an ECDSA/secp256k1 signature recovery) before checking `data.slot_version` against the expected version (staleness) and `config.max_writes` (overflow), even though both of those checks only use unauthenticated, attacker-controlled fields already present in the wire message. Any remote peer that can reach the push-chunk unsolicited-message handler can therefore force the node to run signature recovery on every message it sends, even for messages that are trivially rejectable by the two cheap integer comparisons that come afterward.

### Finding Description
In `validate_received_chunk` [1](#0-0) , the check order is:
1. Size check (cheap) [2](#0-1) 
2. `expected_versions.get(slot_id)` lookup (cheap) [3](#0-2) 
3. `get_slot_signer` + `slot_metadata.verify(&addr)` — **expensive ECDSA recovery** [4](#0-3) 
4. Staleness check `data.slot_version < *expected_version` (cheap, O(1) integer compare, uses only unauthenticated `data.slot_version`) [5](#0-4) 
5. Max-writes check `data.slot_version > config.max_writes` (cheap, same unauthenticated field) [6](#0-5) 

Because `data.slot_version` is a plaintext field on the wire message and is not itself gated by the signature check for the *purpose of steps 4/5* (those steps only compare the field's numeric value, not its authenticity), the staleness and max-writes checks could be evaluated before invoking `verify()` without any loss of correctness, rejecting stale/overflowing chunks for free. Instead, the current code always pays for a full signature recovery first.

This function is reachable from the unauthenticated P2P push path: `PeerNetwork::handle_unsolicited_StackerDBPushChunk` calls `validate_received_chunk` directly on chunk data taken from an incoming `StackerDBPushChunkData` message [7](#0-6) , and again on the `FutureView` Nack branch [8](#0-7) . Any connected peer can send such a push for any `contract_id`/`slot_id` pair that has a configured signer (`get_slot_signer` returns `Some`), with an arbitrary (even garbage) signature and a `slot_version` deliberately set below `expected_version` or above `config.max_writes`. The node will still execute `slot_metadata.verify()` (secp256k1 recovery) before it ever reaches the O(1) checks that would have rejected the message for free.

### Impact Explanation
Each malicious `StackerDBPushChunkData` message forces one full ECDSA public-key-recovery operation on the receiving node's P2P event-processing thread, which is otherwise avoidable via cheap integer comparisons. Since the attacker does not need to hold the actual signer's private key (the recovery executes regardless of whether the recovered address matches `addr`), and does not need to own a slot, this is a bounded compute-DoS: an unprivileged remote peer can repeatedly waste the victim's CPU on cryptographic recovery calls by sending distinct crafted chunks with stale or version-overflowing `slot_version` values. This matches the "bounded compute DoS on a read/write endpoint" High-impact category defined in scope.

### Likelihood Explanation
Preconditions are minimal: the attacker only needs an established P2P connection (handshake) to a node that replicates at least one StackerDB with a signer configured for some slot — a very common node configuration (e.g., `.signers`/`.miners` contracts). No secret, no privileged role, no slot ownership is required, since the check order runs the recovery unconditionally before the value it would otherwise cheaply reject on. The attacker's cost per message is essentially constructing arbitrary bytes (near-zero), while the defender's cost per message includes one signature recovery. This is trivially repeatable at whatever message rate the connection/bandwidth allows.

### Recommendation
Reorder the checks in `validate_received_chunk` so that the O(1) integer comparisons (staleness against `expected_version`, and `max_writes` bound) are performed immediately after the size and `expected_versions` lookup, and before `get_slot_signer`/`slot_metadata.verify()` is invoked. Only chunks that pass the cheap staleness/max-writes bounds should proceed to the expensive signature-verification step.

### Proof of Concept
Rust test plan (net integration test) for `stackslib/src/net/stackerdb/mod.rs`:
1. Construct a `StackerDBConfig` with a small `max_writes` (e.g., 5) and one signer for slot 0.
2. Build a `StackerDBChunkData` with `slot_id = 0`, `slot_version = max_writes + 1000` (or `slot_version` far below `expected_version`), arbitrary `data`, and an arbitrary/garbage `sig` field (not necessarily a real signature from the configured signer).
3. Call `PeerNetwork::validate_received_chunk(&contract_id, &config, &chunk, &expected_versions)` in a loop of N iterations with distinct (data, sig) pairs, instrumenting/timing the call to `slot_metadata.verify(&addr)` inside `validate_received_chunk` (e.g., via a counter or wall-clock wrapper around that call site at `stackslib/src/net/stackerdb/mod.rs:691`).
4. Assert that `verify()` is invoked N times despite every call ultimately returning `Ok(false)` at the staleness/max-writes checks (lines 700-715), demonstrating that N expensive recoveries were performed that could have been avoided by evaluating lines 700-715 before line 691.
5. Compare total CPU time against a patched version where the version checks are moved before signature verification, showing the avoided cost for the crafted (always-invalid) message stream.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L649-718)
```rust
    pub fn validate_received_chunk(
        &self,
        smart_contract_id: &QualifiedContractIdentifier,
        config: &StackerDBConfig,
        data: &StackerDBChunkData,
        expected_versions: &[u32],
    ) -> Result<bool, net_error> {
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
    }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L742-792)
```rust
    pub fn handle_unsolicited_StackerDBPushChunk(
        &mut self,
        chainstate: &mut StacksChainState,
        event_id: usize,
        preamble: &Preamble,
        chunk_data: &StackerDBPushChunkData,
        send_reply: bool,
    ) -> Result<(bool, bool), net_error> {
        let Some(naddr) = self
            .get_p2p_convo(event_id)
            .map(|convo| convo.to_neighbor_address())
        else {
            debug!(
                "Drop unsolicited StackerDBPushChunk: event ID {} is not connected",
                event_id
            );
            return Ok((false, false));
        };

        let mut payload = self.make_StackerDBChunksInv_or_Nack(
            naddr,
            chainstate,
            &chunk_data.contract_id,
            &chunk_data.rc_consensus_hash,
        );
        match payload {
            StacksMessageType::StackerDBChunkInv(ref mut data) => {
                // this message corresponds to an existing DB, and comes from the same view of the
                // stacks chain tip
                let stackerdb_config = if let Some(config) =
                    self.get_stacker_db_configs().get(&chunk_data.contract_id)
                {
                    config
                } else {
                    // not for this DB
                    info!(
                        "StackerDBChunk for {} ID {} is not available locally",
                        &chunk_data.contract_id, chunk_data.chunk_data.slot_id
                    );
                    return Ok((false, false));
                };

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

**File:** stackslib/src/net/stackerdb/mod.rs (L816-847)
```rust
            StacksMessageType::Nack(ref nack_data) => {
                if nack_data.error_code == NackErrorCodes::FutureView {
                    // Chunk corresponds to a known DB but the view of the sender is potentially in
                    // the future. We should buffer this in case it becomes storable, but don't store it yet.
                    // Also validate the chunk before buffering to prevent invalid data from being
                    // accepted (e.g. protect against big chunks with forged signatures).
                    let stackerdb_config = if let Some(config) =
                        self.get_stacker_db_configs().get(&chunk_data.contract_id)
                    {
                        config
                    } else {
                        return Ok((false, false));
                    };

                    let slot_versions =
                        match self.stackerdbs.get_slot_versions(&chunk_data.contract_id) {
                            Ok(versions) => versions,
                            Err(_) => {
                                return Ok((false, false));
                            }
                        };

                    if !self.validate_received_chunk(
                        &chunk_data.contract_id,
                        stackerdb_config,
                        &chunk_data.chunk_data,
                        &slot_versions,
                    )? {
                        return Ok((false, false));
                    }

                    return Ok((true, false));
```
