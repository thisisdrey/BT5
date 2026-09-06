### Title
Unchecked addition of attacker-controlled `write_freq` to a slot's write timestamp causes integer-overflow panic in `StackerDBSync::make_chunk_request_schedule` - (File: `stackslib/src/net/stackerdb/sync.rs`)

### Summary
This is a plausible analog of the reported "insufficient validation of `rebalanceInterval`" bug class: a numeric configuration parameter (`write-freq`) is validated only for its type bound (`<= u64::MAX`), not for safety of its use in arithmetic, and is later added directly to a live timestamp with plain `+` rather than `checked_add`/`saturating_add`.

### Finding Description
`StackerDBConfig::eval_config` reads `write-freq` from a StackerDB smart contract and only rejects it if it exceeds `u64::MAX`: [1](#0-0) 

This permits `write_freq` to be set to any value up to and including `u64::MAX`. The config is later copied verbatim into the sync state machine: [2](#0-1) 

`make_chunk_request_schedule` then uses this value in an unchecked addition against a stored per-slot write timestamp to decide whether a chunk was written "too frequently": [3](#0-2) 

`write_ts + self.write_freq` is a plain `u64` addition. If `write_freq` is close to `u64::MAX` and `write_ts` (a real epoch timestamp, non-zero) is added to it, the sum overflows `u64::MAX`. This exactly mirrors the reported Solidity issue: a smart-contract-supplied interval-like parameter is validated only superficially (type-range check) and is later summed with a live timestamp without checking that the sum stays within bounds, exactly like `_proposeParameters.lastRebalanceTimestamp.add(_proposeParameters.rebalanceInterval)` in the original report.

### Impact Explanation
In Rust, arithmetic overflow on unsigned addition triggers a `panic!` when overflow checks are enabled (debug builds and any build compiled with `overflow-checks = true`); in release builds without overflow checks, it silently wraps, causing `write_ts + write_freq` to become a small number, which then permanently disables the "too frequently written" skip and may cause the peer to endlessly attempt refetching a chunk it should be rate-limiting. In debug/overflow-checked builds this is a crash of the sync loop (bounded compute/availability impact on nodes that replicate this StackerDB), reachable simply by any principal who deploys (or already controls) a StackerDB-trait contract that a node is configured to replicate and setting `write-freq` near `u64::MAX`. This corresponds to a "bounded compute DoS on a read endpoint / state machine" class of issue (High, per the given severity mapping), not a Critical remote-crash-from-few-messages, since it requires the node operator to have opted the node into syncing that particular contract's StackerDB (StackerDB replication is per-contract and configured, not automatically applied to arbitrary contracts).

### Likelihood Explanation
Moderate. Any account can deploy a Clarity contract implementing the `stackerdb-trait` and set `write-freq` to a value near `u64::MAX`; the only precondition is that some node has configured itself to replicate that contract's StackerDB (common for third-party/app StackerDBs, not just the boot `.miners`/`.signers` contracts). The validation gap in `eval_config` is unconditional and applies to every StackerDB contract, not just the boot contracts.

### Recommendation
- In `StackerDBConfig::eval_config` (`stackslib/src/net/stackerdb/config.rs`), impose a sane practical upper bound on `write-freq` (e.g., some reasonable number of seconds, far below `u64::MAX`) rather than merely checking `<= u64::MAX`.
- In `make_chunk_request_schedule` (`stackslib/src/net/stackerdb/sync.rs:340`), replace `write_ts + self.write_freq > now` with a saturating/checked comparison, e.g. `write_ts.saturating_add(self.write_freq) > now`, to eliminate the overflow path regardless of upstream validation.

### Proof of Concept
1. Deploy a StackerDB-trait contract whose `stackerdb-get-config` returns `write-freq: u18446744073709551615` (i.e., `u64::MAX`) and reasonable values for the other fields.
2. Configure (or induce) a node to replicate this contract's StackerDB (as is done for any third-party StackerDB app in `stacks-node` config).
3. Once the node has stored at least one chunk for a slot in this DB (`write_ts` becomes a real, non-zero epoch-seconds value), the node's periodic `run_stacker_db_sync` → `make_chunk_request_schedule` path executes `write_ts + self.write_freq`, overflowing `u64` and panicking (on overflow-checked builds) or silently wrapping and corrupting the intended rate-limit behavior (on release builds).

### Citations

**File:** stackslib/src/net/stackerdb/config.rs (L422-437)
```rust
        let write_freq = config_tuple
            .get("write-freq")
            .expect("FATAL: missing 'write-freq'")
            .clone()
            .expect_u128()?;
        if write_freq > u64::MAX as u128 {
            let reason = format!(
                "Contract {} stipulates a write frequency beyond u64::MAX",
                contract_id
            );
            warn!("{}", &reason);
            return Err(NetError::InvalidStackerDBContract(
                contract_id.clone(),
                reason,
            ));
        }
```

**File:** stackslib/src/net/stackerdb/sync.rs (L264-266)
```rust
        // reload from config
        self.num_slots = config.num_slots() as usize;
        self.write_freq = config.write_freq;
```

**File:** stackslib/src/net/stackerdb/sync.rs (L332-352)
```rust
        let now = get_epoch_time_secs();

        // who has data we need?
        for ((i, local_version), write_ts) in local_slot_versions
            .iter()
            .enumerate()
            .zip(local_write_timestamps.iter())
        {
            if self.write_freq > 0 && write_ts + self.write_freq > now {
                debug!(
                    "{:?}: {}: Chunk {} was written too frequently ({} + {} > {}) in {}, so will not fetch chunk",
                    network.get_local_peer(),
                    &self.smart_contract_id,
                    i,
                    write_ts,
                    self.write_freq,
                    now,
                    &self.smart_contract_id,
                );
                continue;
            }
```
