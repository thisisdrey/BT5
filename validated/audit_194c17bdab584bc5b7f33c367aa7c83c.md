### Title
`getnakamotoinv_try_finish()` merges a `NakamotoInv` reply under `self.reward_cycle()` re-read at receipt time instead of the reward cycle actually requested, allowing state drift/reset between send and receipt to misfile the bitvec - ([File: stackslib/src/net/inv/nakamoto.rs])

### Summary
`NakamotoTenureInv::getnakamotoinv_try_finish()` stores an inbound `NakamotoInv` bitvector under the key returned by `self.reward_cycle()` (i.e. `self.cur_reward_cycle`) evaluated *at reply-processing time*, rather than the reward cycle/consensus-hash that was actually embedded in the `GetNakamotoInvData` request sent out. Because `self.cur_reward_cycle` can be mutated by `try_reset_comms()`/`reset_comms()` on every `process_getnakamotoinv_begins()` poll — even while a request to that same peer is still in flight — a slow or strategically-timed reply can be merged into `tenures_inv` under a different reward cycle than the one the peer was actually asked about.

### Finding Description
The claimed equality is: *the reward-cycle key under which `merge_tenure_inv()` stores the bitvec must equal the reward cycle that was actually requested via `GetNakamotoInvData.consensus_hash`*.

Tracing the code:
- The request is built in `make_getnakamotoinv(reward_cycle)` using `self.reward_cycle_consensus_hashes.get(&reward_cycle)`, and sent for `inv_rc = inv.reward_cycle()` at send time. [1](#0-0) 
- Crucially, `process_getnakamotoinv_begins()` calls `inv.getnakamotoinv_begin(network, max_reward_cycle)` for every peer on *every poll*, regardless of whether a request to that peer is already in flight (`has_inflight` is only checked afterward, to decide whether to skip *sending*, not to skip the `getnakamotoinv_begin`/reset logic). [2](#0-1) 
- `getnakamotoinv_begin()` unconditionally calls `try_reset_comms()`, which resets `self.cur_reward_cycle = start_rc` when `self.cur_reward_cycle >= max_rc || !self.online` and the sync interval has elapsed — this can fire while an earlier request for the *old* `cur_reward_cycle` is still outstanding. [3](#0-2) 
- When the delayed reply eventually arrives, `getnakamotoinv_try_finish()` does not use the `inv_rc`/consensus hash that was pinned at send time; it re-reads `self.reward_cycle()` live and merges the peer's bitvec under whatever `cur_reward_cycle` happens to be *now*: [4](#0-3) 
- `merge_tenure_inv()` blindly inserts the bitvec at the given key with no validation against what was requested: [5](#0-4) 

The `NakamotoInv` wire message itself carries only the bitvector (`inv_data.tenures`) and does not echo back the `consensus_hash`/reward cycle it is answering, so there is no defense-in-depth check tying the payload to the original request content — the code relies entirely on `self.cur_reward_cycle` staying stable between send and receipt, which is not guaranteed given the always-invoked reset path in `process_getnakamotoinv_begins()`.

A remote peer that legitimately received a `GetNakamotoInv` request can hold back its reply (or a peer that has gone `!online`/timed out per `inv_sync_interval`) until the node's next `process_getnakamotoinv_begins()` poll advances/resets `cur_reward_cycle` for that peer's `NakamotoTenureInv`, then deliver its (possibly honest, unmodified) `NakamotoInv` payload. That payload gets stored under the new, different reward-cycle key.

### Impact Explanation
`NakamotoTenureInv.tenures_inv` is used by `find_available_tenures()`/`has_ith_tenure()` to decide which peers have which tenures for scheduling tenure downloads. A misfiled bitvec associates real tenure-availability bits with the wrong reward cycle, causing the download/inventory state machine to believe a peer has (or lacks) tenures for a reward cycle it never actually reported on. This can steer the tenure-download scheduler into stalling (peers appear to lack tenures they have) or wasting requests against peers who don't have the tenures believed to be indicated — matching the "High: steering a node off the tip via false inventory / bounded compute misdirection" category. It does not corrupt consensus-critical chainstate directly; it corrupts local peer-inventory bookkeeping.

### Likelihood Explanation
The precondition is state-machine timing rather than a single crafted byte sequence: the attacking peer must be a real, already-connected, already-authenticated outbound peer that the node is inventory-syncing with (no privileged role needed — this is normal p2p behavior), and it must delay or arrange its reply to land after the node's `try_reset_comms()` conditions (`inv_sync_interval` elapsed and `cur_reward_cycle >= max_rc` or `online == false`) have been met for that peer entry. This is achievable by a patient/slow-responding peer without needing to forge any signature or bypass a specific check, but it does depend on hitting particular internal timing windows of the victim node's inventory-sync polling loop, and I was not able to fully verify (given the iteration budget) whether the underlying `PeerNetworkComms`/conversation-layer request/response sequence-number matching (in `stackslib/src/net/neighbors/comms.rs` / `stackslib/src/net/chat.rs`) provides any additional binding that would close this race in practice.

### Recommendation
Pin the requested reward cycle (or the `consensus_hash` sent in `GetNakamotoInvData`) alongside the outstanding request (e.g., store it in the comms "in-flight" bookkeeping keyed by neighbor address), and have `getnakamotoinv_try_finish()` merge the reply using that pinned value instead of re-reading `self.reward_cycle()` at receipt time. Additionally, consider having `NakamotoInv` echo back the `consensus_hash`/reward cycle it answers so the merge can be validated against it, and avoid invoking `try_reset_comms()` while a request to that peer is still `has_inflight`.

### Proof of Concept
Rust net test plan in `stackslib/src/net/tests/inv/nakamoto.rs`:
1. Construct a `NakamotoTenureInv` and a mock `PeerNetwork`/comms harness; call `getnakamotoinv_begin` to pin `inv_rc = inv.reward_cycle()` and record the consensus hash `ch0` from `reward_cycle_consensus_hashes.get(&inv_rc)`.
2. Simulate the request being sent (mark `has_inflight`), then mutate the peer's `NakamotoTenureInv` (e.g., call `try_reset_comms`/`reset_comms` directly, or drive enough time/`inv_sync_interval` and re-invoke `process_getnakamotoinv_begins`) so `self.cur_reward_cycle` changes to `inv_rc2 != inv_rc`.
3. Deliver a crafted `StacksMessage` with `StacksMessageType::NakamotoInv(NakamotoInvData { tenures: <bitvec B> })` (the payload that would have legitimately answered the request for `ch0`) into `getnakamotoinv_try_finish`.
4. Assert: `inv.tenures_inv.get(&inv_rc2) == Some(&B)` while `inv_rc2 != inv_rc`, i.e., the bitvec that was semantically about `ch0`/`inv_rc` is now stored under `inv_rc2`, demonstrating the broken equality between the requested reward cycle and the stored key.

### Citations

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

**File:** stackslib/src/net/inv/nakamoto.rs (L573-589)
```rust
    pub fn try_reset_comms(&mut self, inv_sync_interval: u64, start_rc: u64, max_rc: u64) {
        let now = get_epoch_time_secs();
        if self.start_sync_time + inv_sync_interval <= now
            && (self.cur_reward_cycle >= max_rc || !self.online)
        {
            self.reset_comms(start_rc);
        }
    }

    /// Reset synchronization state for this peer in the last reward cycle.
    /// Called as part of processing a new burnchain block
    pub fn reset_comms(&mut self, start_rc: u64) {
        debug!("Reset inv comms for {}", &self.neighbor_address);
        self.online = true;
        self.start_sync_time = get_epoch_time_secs();
        self.cur_reward_cycle = start_rc;
    }
```

**File:** stackslib/src/net/inv/nakamoto.rs (L655-672)
```rust
    pub fn getnakamotoinv_try_finish(
        &mut self,
        network: &mut PeerNetwork,
        reply: StacksMessage,
    ) -> Result<bool, NetError> {
        match reply.payload {
            StacksMessageType::NakamotoInv(inv_data) => {
                debug!(
                    "{:?}: got NakamotoInv from {:?}: {:?}",
                    network.get_local_peer(),
                    &self.neighbor_address,
                    &inv_data
                );

                let ret = self.merge_tenure_inv(inv_data.tenures, self.reward_cycle());
                self.next_reward_cycle();
                return Ok(ret);
            }
```

**File:** stackslib/src/net/inv/nakamoto.rs (L826-833)
```rust
    fn make_getnakamotoinv(&self, reward_cycle: u64) -> Option<StacksMessageType> {
        let Some(ch) = self.reward_cycle_consensus_hashes.get(&reward_cycle) else {
            return None;
        };
        Some(StacksMessageType::GetNakamotoInv(GetNakamotoInvData {
            consensus_hash: ch.clone(),
        }))
    }
```

**File:** stackslib/src/net/inv/nakamoto.rs (L921-936)
```rust
            let proceed = inv.getnakamotoinv_begin(network, max_reward_cycle);
            let inv_rc = inv.reward_cycle();
            new_inventories.insert(naddr.clone(), inv);

            if self.comms.has_inflight(&naddr) {
                debug!(
                    "{:?}: still waiting for reply from {}",
                    network.get_local_peer(),
                    &naddr
                );
                continue;
            }

            if !proceed {
                continue;
            }
```
