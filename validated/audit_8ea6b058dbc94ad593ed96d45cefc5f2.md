### Title
Reconfiguring a StackerDB preserves a slot's stale version/data when a signer keeps the same slot index, allowing non-canonical prior-cycle state to be served as canonical - (File: stackslib/src/net/stackerdb/db.rs)

### Summary
`StackerDBTx::reconfigure_stackerdb` only resets a slot's version, signature and chunk data when the slot's owning principal *changes*. When the same signer keeps the same `slot_id` across a reconfiguration, the branch is skipped entirely, leaving the old Lamport `slot_version` and chunk bytes in place. Because the `.signers-{0,1}-{message_id}` StackerDB contracts are permanently reused every two reward cycles (`reward_cycle % 2`), a signer that lands in the same slot index two cycles apart will have its stale, previous-cycle chunk (e.g. a `StateMachineUpdate`) served as if it were the current, canonical value for the new cycle, until that signer gets around to writing a fresh chunk.

### Finding Description
`reconfigure_stackerdb` walks the new slot assignment and only wipes a slot when the owning address differs from what's on file: [1](#0-0) 

```
if let Some(existing_validation) =
    self.get_slot_validation(smart_contract, slot_id)?
{
    // this slot already exists.
    if existing_validation.signer == *principal {
        // no change
        continue;
    }
}
// new slot, or existing slot with a different signer
... INSERT OR REPLACE ... version = NO_VERSION, data = vec![], data_hash = zero, signature = empty
```

This equality check (`existing_validation.signer == *principal`) is the exact analog of the reported bug's broken invariant: a config-driven re-binding of a stateful slot does not reset the "epoch-scoped" accumulator (here, the slot's Lamport version and chunk bytes) when the identity superficially looks unchanged.

The `.signers-*` StackerDB contracts do not encode the reward cycle in their identifier — only the cycle's parity and the message type: [2](#0-1) 

So the identical contract (e.g. `signers-0-2`) is reused for reward cycle `N` and again for `N+2`, `N+4`, etc. `create_or_reconfigure_stackerdbs` only calls `reconfigure_stackerdb` (not `create_stackerdb`) for these already-existing contracts each cycle transition: [3](#0-2) 

Signer slot ordering is deterministic (sorted by public key bytes), so whenever the signer set for cycle `N` and cycle `N+2` happens to place the same signer address at the same `slot_id` (a very plausible occurrence for a signer set that hasn't fully turned over), `reconfigure_stackerdb`'s "no change" branch fires and the slot's version/chunk data from cycle `N` survives untouched into cycle `N+2`. `try_replace_chunk`'s freshness gate (`slot_desc.slot_version <= slot_validation.version`) then continues to treat that carried-over version as the current baseline: [4](#0-3) 

Meanwhile the `StateMachineUpdate` payload that gets stored in these slots carries no reward-cycle field to let a consumer detect staleness — only `burn_block`/`burn_block_height`/`current_miner`: [5](#0-4) [6](#0-5) 

Because `get_latest_chunk`/`get_latest_chunks` return whatever is on file with no cross-check against the current reward cycle's start, any downstream reader that trusts "the current chunk in this slot = the current state for this cycle" will observe the previous cycle's data as canonical until the signer overwrites it (this is illustrated by consumers such as `InitialChunksLoader::load_chunks`, which blindly loads chunks from the shared `StateMachineUpdate` contract at cycle start).

### Impact Explanation
This breaks the equality "served StackerDB chunk == the chunk generated for the current epoch/cycle it claims to represent," i.e. non-canonical (stale, prior-cycle) state is served as canonical for the new cycle. Any node or downstream consumer bootstrapping from the `.signers-*` StackerDB at the start of a new reward cycle can be fed the *previous* occupant cycle's `StateMachineUpdate`/other content instead of a freshly-reset value, matching the "serving non-canonical state as canonical" High-impact category.

### Likelihood Explanation
Low-to-moderate: no attacker action is required (it is analogous to the original report's "no malicious actor, just normal reconfiguration flow" scenario) — it simply requires that, across two-cycle-apart signer sets sharing the same contract parity, at least one signer's address sorts into the same `slot_id` it held two cycles prior. Given deterministic sort-by-pubkey-bytes assignment and typically overlapping signer sets between nearby cycles, this is a realistic, recurring condition rather than a contrived edge case.

### Recommendation
In `StackerDBTx::reconfigure_stackerdb` (`stackslib/src/net/stackerdb/db.rs`), do not treat "same signer, same slot_id" as a no-op across a full StackerDB config swap for cycle-scoped contracts (e.g. `.signers-*`). Either always reset `version`/`data`/`data_hash`/`signature` to their empty/`NO_VERSION` state when the contract's underlying config generation changes (not merely when the signer differs), or thread a cycle/config identifier into the reconfiguration call so the same-signer branch can distinguish "same slot, same logical epoch" from "same slot, new epoch." Consumers that treat `get_latest_chunk` as authoritative for "the current cycle" should also validate an embedded reward-cycle/epoch marker in the payload before trusting it, rather than relying solely on slot freshness.

### Proof of Concept
1. Reward cycle `N`, parity `p = N % 2`: `.signers-p-<message_id>` StackerDB is created via `create_stackerdb`/`reconfigure_stackerdb` with signer `A` assigned `slot_id = 3`. `A` submits a `StateMachineUpdate` chunk at version `7`.
2. Reward cycle `N+2` (same parity `p`) arrives. `create_or_reconfigure_stackerdbs` calls `reconfigure_stackerdb` on the same, already-existing `.signers-p-<message_id>` contract with the new signer set for cycle `N+2`. Deterministic pubkey-byte sorting again places `A` at `slot_id = 3`.
3. Inside `reconfigure_stackerdb`, `existing_validation.signer == *principal` is true for slot 3, so the branch `continue`s — version `7` and cycle-`N` chunk bytes remain in the `chunks` table, untouched, for cycle `N+2` (`stackslib/src/net/stackerdb/db.rs:319-327`).
4. Before `A` writes a fresh chunk for cycle `N+2`, any node calling `get_latest_chunk`/`get_latest_chunks` on this contract for slot 3 receives `A`'s stale cycle-`N` `StateMachineUpdate` data/version, indistinguishable from fresh cycle-`N+2` data since the payload carries no reward-cycle tag.

### Citations

**File:** stackslib/src/net/stackerdb/db.rs (L317-346)
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

**File:** stackslib/src/chainstate/nakamoto/signer_set.rs (L1060-1063)
```rust
    /// Make the contract name for a signers DB contract
    pub fn make_signers_db_name(reward_cycle: u64, message_id: u32) -> String {
        format!("{}-{}-{}", &SIGNERS_NAME, reward_cycle % 2, message_id)
    }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L406-428)
```rust
            // Create the StackerDB replica if it does not exist already
            if !existing_contract_ids.contains(&stackerdb_contract_id) {
                if let Err(e) = self.create_stackerdb(&stackerdb_contract_id, &new_config) {
                    warn!(
                        "Failed to create or reconfigure StackerDB {stackerdb_contract_id}: DB error {:?}",
                        &e
                    );
                }
            } else if (new_config != stackerdb_config && !new_config.signers.is_empty())
                || (new_config == stackerdb_config
                    && new_config.signers.len()
                        != self.get_slot_versions(&stackerdb_contract_id)?.len())
            {
                // only reconfigure if the config has changed
                // (that second check on the length is needed in case the node is a victim of
                // #5142, which was a bug whereby a stackerdb could never shrink)
                if let Err(e) = self.reconfigure_stackerdb(&stackerdb_contract_id, &new_config) {
                    warn!(
                        "Failed to create or reconfigure StackerDB {stackerdb_contract_id}: DB error {:?}",
                        &e
                    );
                }
            }
```

**File:** libsigner/src/v0/messages.rs (L556-567)
```rust
/// Message for updates to the Signer State machine
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct StateMachineUpdate {
    /// The active signing protocol version
    pub active_signer_protocol_version: u64,
    /// The highest supported signing protocol by the local signer
    pub local_supported_signer_protocol_version: u64,
    /// The actual content of the state machine update message (this is a versioned enum)
    pub content: StateMachineUpdateContent,
    // Prevent manual construction of this struct
    no_manual_construct: PhantomData<()>,
}
```

**File:** libsigner/src/v0/messages.rs (L569-591)
```rust
/// Versioning enum for StateMachineUpdate messages
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub enum StateMachineUpdateContent {
    /// Version 0
    V0 {
        /// The tip burn block (i.e., the latest bitcoin block) seen by this signer
        burn_block: ConsensusHash,
        /// The tip burn block height (i.e., the latest bitcoin block) seen by this signer
        burn_block_height: u64,
        /// The signer's view of who the current miner should be (and their tenure building info)
        current_miner: StateMachineUpdateMinerState,
    },
    /// Version 1
    V1 {
        /// The tip burn block (i.e., the latest bitcoin block) seen by this signer
        burn_block: ConsensusHash,
        /// The tip burn block height (i.e., the latest bitcoin block) seen by this signer
        burn_block_height: u64,
        /// The signer's view of who the current miner should be (and their tenure building info)
        current_miner: StateMachineUpdateMinerState,
        /// The replay transactions
        replay_transactions: Vec<StacksTransaction>,
    },
```
