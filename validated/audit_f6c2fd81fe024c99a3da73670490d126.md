### Title
Single malformed chunk signature aborts processing of all other legitimately-collected StackerDB chunk replies in a sync round - ([File: stackslib/src/net/stackerdb/sync.rs])

### Summary
`StackerDBSync::getchunks_try_finish` iterates over all replies collected in a round from potentially many different neighbors and calls `self.validate_downloaded_chunk(...)?` for each one [1](#0-0) . That `?` propagates any `Err` out of the entire function instead of just skipping the one bad reply, which is the same "one failing item aborts the whole batch" fault pattern as the external report's `claimRemovedTokens` loop.

### Finding Description
`validate_downloaded_chunk` calls `network.validate_received_chunk`, which in turn calls `slot_metadata.verify(&addr)` via `StackerDBChunkData::verify` [2](#0-1) . `SlotMetadata::verify` recovers a public key from the attacker-supplied `signature` field and can return `Err(Error::VerifyingError(..))` (not just `Ok(false)`) whenever the raw signature bytes are not a value the recovery routine accepts (e.g., an invalid recovery id or non-canonical encoding) [3](#0-2) .

Because `slot_desc.verify(...)`/`validate_received_chunk`/`validate_downloaded_chunk` all forward errors with `?` rather than converting a bad-signature error into `Ok(false)`, a single malformed `StackerDBChunk` reply from *any one* of the many peers being processed in `getchunks_try_finish`'s `for (naddr, message) in self.comms.collect_replies(network).into_iter()` loop causes the function to return `Err(..)` immediately [4](#0-3) . This discards processing of the remaining, otherwise-valid replies already collected in that same round (they are never validated, never added via `add_downloaded_chunk`, and the neighbors who sent them are not credited), and the error is further propagated up through the sync state machine call `self.getchunks_try_finish(network, config)?` [5](#0-4) , aborting the whole sync-round step for that StackerDB replica.

This breaks the equality that "per-reply validation should only affect that reply" (an authenticated-vs-forged check on one item should not gate acceptance of other, independently-authenticated items in the same batch) — mirroring the Solidity finding's use of a per-item transfer failure to revert claims for all other snapshot IDs.

### Impact Explanation
Any unprivileged remote peer that is a configured signer/participant for a StackerDB replica (or simply spoofs a `StackerDBChunk` reply to a request the node made) can send one chunk whose signature bytes trigger a recovery-library error. This wastes the round's already-downloaded, validly-signed chunks from other cooperating peers and forces the local node to discard progress and retry, repeatable every sync attempt at negligible attacker cost. Impact is bounded to degraded/failed StackerDB sync progress (a partial availability/DoS effect on the StackerDB sync subsystem, not memory corruption or unauthorized writes) — this only reaches the "bounded compute DoS" tier at best, and is weaker than the Critical/High categories in scope since no forged data is stored and no crash occurs.

### Likelihood Explanation
Likelihood is straightforward to trigger: the attacker only needs to respond to (or be selected as a target of) a `StackerDBGetChunk` request with a `StackerDBChunk` payload carrying a corrupt/invalid signature. No authentication beyond being a network peer is required, and repeating the attack every round keeps degrading sync throughput for the affected replica.

### Recommendation
In `validate_downloaded_chunk` / `validate_received_chunk`, treat a signature-recovery error the same as a verification failure — map `Err` from `verify()` to `Ok(false)` (log and continue) rather than propagating it with `?`, so `getchunks_try_finish` uses `continue` for the bad reply as it already does for `Nack`/unexpected messages, instead of aborting the whole batch [6](#0-5) .

### Proof of Concept
1. Node A requests chunks from several neighbors during a StackerDB sync round.
2. Malicious neighbor `M` (or a spoofed reply matching an in-flight request) responds with a `StackerDBChunk` whose `sig` field is a byte string that `StacksPublicKey::recover_to_pubkey_without_validating_low_s` rejects with an error (rather than simply producing a mismatching key) — e.g., an out-of-range recovery id byte.
3. `getchunks_try_finish` processes replies in whatever order `collect_replies` returns them; when it reaches `M`'s malformed reply, `validate_downloaded_chunk(...)?` returns `Err`, which is propagated immediately out of `getchunks_try_finish`.
4. Any valid replies from cooperating neighbors that were iterated after `M`'s in the same `HashMap`/collection are never validated or stored via `add_downloaded_chunk`, and the surrounding sync-state-machine step also errors out via `?` at `sync.rs:1495`, forcing the round to restart.

### Citations

**File:** stackslib/src/net/stackerdb/sync.rs (L1130-1174)
```rust
        for (naddr, message) in self.comms.collect_replies(network).into_iter() {
            let data = match message.payload {
                StacksMessageType::StackerDBChunk(data) => data,
                StacksMessageType::Nack(data) => {
                    debug!(
                        "{:?}: {}: remote peer {:?} NACK'ed our StackerDBGetChunk with code {}",
                        network.get_local_peer(),
                        &self.smart_contract_id,
                        &naddr,
                        data.error_code
                    );
                    if data.error_code == NackErrorCodes::StaleView
                        || data.error_code == NackErrorCodes::FutureView
                    {
                        self.stale_neighbors.insert(naddr);
                    } else if data.error_code == NackErrorCodes::StaleVersion {
                        // try again immediately, without throttling
                        self.stale_inv = true;
                    }
                    continue;
                }
                x => {
                    info!(
                        "{:?}: {}: Received unexpected message {:?}",
                        network.get_local_peer(),
                        &self.smart_contract_id,
                        &x
                    );
                    self.unpin_connected_replica(network, &naddr);
                    continue;
                }
            };

            // validate
            if !self.validate_downloaded_chunk(network, config, &data)? {
                info!(
                    "{:?}: {}: Remote neighbor {:?} served an invalid chunk for ID {}",
                    network.get_local_peer(),
                    &self.smart_contract_id,
                    &naddr,
                    data.slot_id
                );
                self.unpin_connected_replica(network, &naddr);
                continue;
            }
```

**File:** stackslib/src/net/stackerdb/sync.rs (L1494-1497)
```rust
                    let requests_finished = self.getchunks_begin(network)?;
                    let inflight_finished = self.getchunks_try_finish(network, config)?;
                    let done = requests_finished && inflight_finished;
                    if done {
```

**File:** stackslib/src/net/stackerdb/mod.rs (L690-697)
```rust
        let slot_metadata = data.get_slot_metadata();
        if !slot_metadata.verify(&addr)? {
            info!(
                "StackerDBChunk for {} ID {} is not signed by {}",
                smart_contract_id, data.slot_id, &addr
            );
            return Ok(false);
        }
```

**File:** libstackerdb/src/libstackerdb.rs (L183-193)
```rust
    pub fn verify(&self, principal: &StacksAddress) -> Result<bool, Error> {
        let sigh = self.auth_digest();
        let pubk = StacksPublicKey::recover_to_pubkey_without_validating_low_s(
            sigh.as_bytes(),
            &self.signature,
        )
        .map_err(|ve| Error::VerifyingError(ve.to_string()))?;

        let pubkh = Hash160::from_node_public_key(&pubk);
        Ok(pubkh == *principal.bytes())
    }
```
