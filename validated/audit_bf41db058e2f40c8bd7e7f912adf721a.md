### Title
Write-frequency (`write_freq`) throttle is bypassed for unsolicited pushed chunks - ([File: stackslib/src/net/stackerdb/sync.rs])

### Finding Description
`StackerDBConfig::write_freq` is documented as "minimum wall-clock time between writes to the same slot" [1](#0-0) . The only place this throttle is actually enforced is in the sync scheduler, which skips *requesting* a chunk that was written too recently, so `StackerDBSync::validate_downloaded_chunk` explicitly does not re-check the timestamp, relying on the scheduler having already filtered it out: "no need to validate the timestamp, because we already skipped requesting it if it was written too recently" [2](#0-1) . That function calls `PeerNetwork::validate_received_chunk`, which performs signature/version/hash validation but is not shown (nor found via search) to consult `write_freq`/write timestamps at all — `write_freq` only appears in `sync.rs` (scheduling), `config.rs`, and the `StackerDBConfig` struct/`noop()` initializer in `mod.rs`; it does not appear in the storage layer (`db.rs`) or in any shared validation routine that both the pull (`validate_downloaded_chunk`) and push (`handle_unsolicited_StackerDBPushChunk`) paths would go through.

Because the pull path's timestamp-skip logic lives only in the scheduler (i.e., in code that decides which chunks to *fetch*), and the push path (`handle_unsolicited_StackerDBPushChunk` in `stackslib/src/net/unsolicited.rs`) never goes through that scheduler — it receives an already-signed `StackerDBPushChunkData` from a peer and calls the shared validation/storage routine directly — there is no `write_freq`/write-timestamp gate anywhere on the push path. A remote peer holding legitimate signing authority over a slot can therefore sign and push arbitrarily many chunk versions to that slot in rapid succession, each of which passes signature/hash/version validation and gets accepted and stored/rebroadcast, even though `config.write_freq > 0` should bound the rate of such writes.

### Impact Explanation
An attacker who legitimately owns one StackerDB slot (e.g., a signer's slot) can force the node to repeatedly re-validate, re-store (SQLite write), and rebroadcast new chunk versions for that slot at a rate far exceeding the smart-contract-configured `write_freq`. This causes unbounded state churn (repeated DB writes, replication computation, and network rebroadcast to peers) for a resource that the protocol intended to rate-limit, which peers propagate onward to other StackerDB replicators — an availability/resource-exhaustion issue bounded only by the attacker's own signing rate, not by the protocol's configured throttle.

### Likelihood Explanation
The attacker needs only: (1) an open P2P connection to a node (unprivileged, remotely reachable), and (2) legitimate ownership/signing key for at least one slot in a StackerDB the node replicates — both are within the stated unprivileged-attacker model (owning a slot they legitimately hold). No admin role, RPC secret, or other privileged access is required. The attack is trivially repeatable at whatever rate the attacker can sign and send `StackerDBPushChunkData` messages.

### Recommendation
Enforce `write_freq` (and any other timestamp-based throttling) inside the shared validation path used by both pull and push (i.e., inside `PeerNetwork::validate_received_chunk` or the storage layer in `db.rs`), comparing the slot's last-write timestamp against `write_freq` regardless of whether the chunk arrived via scheduled fetch or unsolicited push. Do not rely on the sync scheduler as the sole enforcement point, since it is bypassable by any path that doesn't go through scheduling (push, and potentially the HTTP POST chunk-upload path as well).

### Proof of Concept
Rust test plan in `stackslib/src/net/stackerdb/tests/sync.rs` or `net::tests::relay::nakamoto`:
1. Configure a `StackerDBConfig` with `write_freq = N` seconds (N > 0) for a slot owned by a test keypair.
2. Construct multiple `StackerDBPushChunkData` messages for the same slot with increasing `slot_version`, each signed by the owning key, sent back-to-back (well within `write_freq` seconds of each other).
3. Feed each message through `handle_unsolicited_StackerDBPushChunk` (as invoked from `stackslib/src/net/unsolicited.rs`).
4. Assert that all chunks are accepted/stored despite being written faster than `write_freq` allows — contrast with an equivalent scenario going through `StackerDBSync`'s pull scheduling, where the second request would be skipped/deferred due to the recent-write check, demonstrating the pull vs. push asymmetry.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L238-242)
```rust
    pub signers: Vec<(StacksAddress, u32)>,
    /// minimum wall-clock time between writes to the same slot.
    pub write_freq: u64,
    /// maximum number of times a slot may be written to during a reward cycle.
    pub max_writes: u32,
```

**File:** stackslib/src/net/stackerdb/sync.rs (L537-558)
```rust
    /// Validate a downloaded chunk
    pub fn validate_downloaded_chunk(
        &self,
        network: &PeerNetwork,
        config: &StackerDBConfig,
        data: &StackerDBChunkData,
    ) -> Result<bool, net_error> {
        // validate -- must be a valid chunk
        if !network.validate_received_chunk(
            &self.smart_contract_id,
            config,
            data,
            &self.expected_versions,
        )? {
            return Ok(false);
        }

        // no need to validate the timestamp, because we already skipped requesting it if it was
        // written too recently.

        Ok(true)
    }
```
