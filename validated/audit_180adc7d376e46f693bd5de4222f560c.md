### Title
`NakamotoTenureInv::merge_tenure_inv` accepts undersized `BitVec<2100>` inventories, causing `has_ith_tenure` to silently report "no tenure" for truncated bits - ([File: stackslib/src/net/inv/nakamoto.rs])

### Summary
`NakamotoTenureInv::merge_tenure_inv` stores a peer-supplied `BitVec<2100>` into `tenures_inv` for a reward cycle without ever validating that its bit length equals `reward_cycle_len`. `has_ith_tenure` then computes an index via `sortition_height % self.reward_cycle_len` and calls `rc_tenures.get(rc_height).unwrap_or(false)`, so any index beyond the (attacker-truncated) bitvec length silently resolves to `false` instead of erroring or reporting the true tenure status.

### Finding Description
The invariant the code relies on but never enforces is: `inv.tenures_inv.get(reward_cycle).len() == reward_cycle_len`. `merge_tenure_inv` only checks whether the *content* differs from the previously stored bitvec to compute the `learned` return value; it performs no length check before inserting: [1](#0-0) 

`has_ith_tenure` computes `rc_height` via modulo against `self.reward_cycle_len` (a locally configured constant, not the length of the stored bitvec) and does a bounds-unchecked `.get(rc_height).unwrap_or(false)`: [2](#0-1) 

Since a remote peer supplies the `NakamotoInv`/tenure-inventory bitvec on the wire, an attacker-controlled peer (running its own node, replying to `GetNakamotoInv`) can send a `BitVec<2100>` far shorter than the actual reward-cycle length for that cycle (e.g., 3 bits instead of ~2100). `merge_tenure_inv` accepts and stores it unconditionally. Any subsequent `has_ith_tenure` call for a sortition height whose `rc_height` falls beyond the truncated bitvec's length returns `false` via `unwrap_or(false)`, even though the peer might legitimately have (or the network canonically has) a tenure there. This makes the peer/inventory look like it lacks data for the tail of the reward cycle, which affects the download scheduler's decision to fetch tenures from that peer for that range.

I was not able to fully trace the exact call site that invokes `merge_tenure_inv` from the wire-message handler (in `net/inv/mod.rs`) within the available iterations, so I cannot confirm with full certainty whether an additional length-normalization/validation step exists between message deserialization and `merge_tenure_inv`. Based on the code inspected, `merge_tenure_inv` itself performs no such check, and `has_ith_tenure`'s modulo-indexed `.get(...).unwrap_or(false)` pattern is confirmed to silently degrade rather than fail loudly for undersized bitvecs.

### Impact Explanation
The consequence is a local, per-peer availability degradation: the requesting node undercounts an honest-but-truncated peer's tenure availability for the tail of a reward cycle, potentially causing it to skip requesting real tenure data from that specific peer for those sortition heights. This does not forge canonical state, crash the node, or corrupt data written to disk/StackerDB — the node simply believes one peer doesn't have some tenures it might have. Given the node normally has multiple peers and would fall back to others, the practical impact is a soft download-scheduling inefficiency rather than a hard stall of chain sync, and no data is served or written as canonical that wasn't validated elsewhere (block/tenure download still requires normal chainstate validation).

### Likelihood Explanation
Precondition is only that the attacker operate a peer that a victim node calls into its inventory sync with (achievable by any peer participating in the P2P network, no privileged role or secret needed). Crafting the truncated `BitVec<2100>` requires only controlling the reply bytes for a `GetNakamotoInv` exchange, which is a normal, cheap, repeatable action.

### Recommendation
Add an explicit length check in `merge_tenure_inv` (or in the code that constructs/validates the inbound `NakamotoInv` message) rejecting/normalizing bitvecs whose `.len()` does not equal `reward_cycle_len` for the given reward cycle, and change `has_ith_tenure` to distinguish "index out of range because peer sent short data" (treat as unknown/stale peer, potentially disconnect) from a genuine `false` bit.

### Proof of Concept
Rust unit test in `stackslib/src/net/inv/nakamoto.rs` test module:
1. Construct a `NakamotoTenureInv` with `reward_cycle_len = 2100` (or a smaller test value like 20) and `first_block_height` set appropriately.
2. Call `merge_tenure_inv(BitVec::<2100>::try_from(vec![true, true, true]).unwrap(), reward_cycle)` — a 3-bit bitvec far shorter than `reward_cycle_len`.
3. Assert `merge_tenure_inv` returns without error/panic (confirming no length validation).
4. Call `has_ith_tenure(burn_block_height)` for a sortition height whose `rc_height` (via `sortition_height % reward_cycle_len`) is `>= 3` — assert it returns `false` due to `unwrap_or(false)`, even though a correctly-sized inventory reporting `true` at that offset would have returned `true`.

### Citations

**File:** stackslib/src/net/inv/nakamoto.rs (L502-525)
```rust
    /// Does this remote neighbor have the ith tenure data for the given (absolute) burn block height?
    /// (note that block_height is the _absolute_ block height)
    pub fn has_ith_tenure(&self, burn_block_height: u64) -> bool {
        if burn_block_height < self.first_block_height {
            return false;
        }

        let Some(reward_cycle) = PoxConstants::static_block_height_to_reward_cycle(
            burn_block_height,
            self.first_block_height,
            self.reward_cycle_len,
        ) else {
            return false;
        };

        let Some(rc_tenures) = self.tenures_inv.get(&reward_cycle) else {
            return false;
        };

        let sortition_height = burn_block_height - self.first_block_height;
        let rc_height = u16::try_from(sortition_height % self.reward_cycle_len)
            .expect("FATAL: reward cycle length exceeds u16::MAX");
        rc_tenures.get(rc_height).unwrap_or(false)
    }
```

**File:** stackslib/src/net/inv/nakamoto.rs (L549-560)
```rust
    pub fn merge_tenure_inv(&mut self, tenure_inv: BitVec<2100>, reward_cycle: u64) -> bool {
        // populate the tenures bitmap to we can fit this tenures inv
        let learned = self
            .tenures_inv
            .get(&reward_cycle)
            .map(|cur_inv| cur_inv != &tenure_inv)
            .unwrap_or(true);

        self.tenures_inv.insert(reward_cycle, tenure_inv);
        self.last_updated_at = get_epoch_time_secs();
        learned
    }
```
