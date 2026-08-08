### Title
Divide-before-multiply truncation in `CostModel::get_instructions_data_cost` under-charges the block-cost model for large instruction data, allowing QoS/cost-limit evasion - (File: cost-model/src/cost_model.rs)

### Summary
`CostModel::get_instructions_data_cost` computes the data-bytes cost component of a transaction's block cost via a single truncating integer division (`instruction_data_len / INSTRUCTION_DATA_BYTES_COST`), the same "divide-before-multiply/one-shot division truncation" pattern flagged in the referenced report's `_GeneralIntegrate()` finding. Because `INSTRUCTION_DATA_BYTES_COST` is not 1, every transaction's data-bytes cost is rounded down, and the error compounds across a block, letting transactions consume more real (serialization/heap) cost than what is charged against `MAX_BLOCK_UNITS` / per-writable-account limits.

### Finding Description
`get_instructions_data_cost` is defined as: [1](#0-0) 

with `INSTRUCTION_DATA_BYTES_COST` defined as a truncated constant itself: [2](#0-1) 

`140 /*bytes per us*/ / COMPUTE_UNIT_TO_US_RATIO` = `140 / 30` = `4` (Rust integer division truncates `4.666...` down to `4`), so this constant itself already loses ~14% of precision before it is even used as a divisor. Then `instruction_data_len() / 4` truncates again for any `instruction_data_len` not a multiple of 4. The result feeds directly, unmultiplied, into the transaction's total cost: [3](#0-2) 

This total (`TransactionCost::sum()`) is what banking-stage's cost tracker and the block cost limits (`MAX_BLOCK_UNITS`, `MAX_WRITABLE_ACCOUNT_UNITS`) use to admit/reject transactions and to bound the CU-cost accounted per block, so any systematic undercount here directly reduces the price an attacker pays (in "cost units") to occupy block space with large-instruction-data transactions.

### Impact Explanation
This is a "grossly underpriced pre-fee work" analog of the referenced report: because the division-before-any-multiplication pattern always rounds toward zero, transactions carrying large instruction data are cost-modeled cheaper than the compute/cost budget SIMDs intend. This lets an attacker pack more real instruction-data bytes into a block than the cost model accounts for, evading the per-block (`MAX_BLOCK_UNITS`) and per-account (`MAX_WRITABLE_ACCOUNT_UNITS`) cost limits enforced in banking stage / cost tracker — a QoS evasion vector reachable by any unprivileged transaction submitter, not requiring validator/operator privilege.

### Likelihood Explanation
Likelihood is difficult to establish as "high impact" without further modeling: the per-transaction rounding error is bounded (at most `INSTRUCTION_DATA_BYTES_COST - 1` = 3 compute units per transaction, i.e., a small absolute amount), and `data_bytes_cost` is only one of five additive cost components (`signature_cost`, `write_lock_cost`, `data_bytes_cost`, `programs_execution_cost`, `loaded_accounts_data_size_cost`) in `TransactionCost::sum()`, most of which dominate total cost for typical transactions. I could not verify from the available index whether this rounding is intentional/accepted by the Agave team (unlike the abracadabra report where the team acknowledged the issue) or whether it has already been evaluated and deemed negligible given `MAX_BLOCK_UNITS` is on the order of tens of millions of compute units. I was not able to find any test or comment in `cost-model/src/cost_model.rs` or `block_cost_limits.rs` discussing intentional-vs-unintentional truncation here, nor any git history/PR context confirming or denying this was a deliberate design tradeoff.

### Recommendation
If precision here is deemed material to the cost model's fairness guarantees, `get_instructions_data_cost` should use ceiling division (round up, i.e., `div_ceil`) so the cost model never under-charges data-byte cost, and `INSTRUCTION_DATA_BYTES_COST`'s definition should likewise avoid truncating the byte-cost ratio before storing it as a `u64` divisor (or should be expressed as a rational number applied via multiply-then-divide, analogous to the referenced report's fix of reordering `_GeneralIntegrate()`'s arithmetic to multiply before dividing).

### Proof of Concept
Given `COMPUTE_UNIT_TO_US_RATIO = 30`, `INSTRUCTION_DATA_BYTES_COST = 140 / 30 = 4` (true value ≈ 4.667, a 14.3% undercount baked into the constant itself): [4](#0-3) 

For a transaction with `instruction_data_len() = 15` bytes:
- Actual implementation: `15 / 4 = 3` (floor) compute units charged.
- If the constant were not pre-truncated and division were rounded up: `15 * 30 / 140 = 3.21 → 4` (ceil) compute units — a ~25% relative undercount for this transaction's data-bytes cost component, consistent in kind with the ~27.5% error demonstrated in the referenced report's `_GeneralIntegrate()` PoC.

I was unable to confirm within the indexed code whether existing unit tests (`cost-model/src/transaction_cost.rs` lines 281–394, e.g. `test_vote_transaction_cost`, `test_non_vote_transaction_cost`) already encode this truncation as expected/accepted behavior — they do reproduce the same truncating division (`vote_transaction.instruction_data_len() / (INSTRUCTION_DATA_BYTES_COST as u16)`), which suggests the truncation is currently treated as intended rather than a bug, weakening confidence that this rises to the level of a validated, previously-unknown vulnerability rather than accepted design behavior. [5](#0-4)

### Citations

**File:** cost-model/src/cost_model.rs (L180-183)
```rust
    /// Return the instruction data bytes cost.
    fn get_instructions_data_cost(transaction: &impl TransactionMeta) -> u16 {
        transaction.instruction_data_len() / (INSTRUCTION_DATA_BYTES_COST as u16)
    }
```

**File:** cost-model/src/block_cost_limits.rs (L8-20)
```rust
pub const COMPUTE_UNIT_TO_US_RATIO: u64 = 30;
/// Number of compute units for one signature verification.
pub const SIGNATURE_COST: u64 = COMPUTE_UNIT_TO_US_RATIO * 24;
/// Number of compute units for one secp256k1 signature verification.
pub const SECP256K1_VERIFY_COST: u64 = COMPUTE_UNIT_TO_US_RATIO * 223;
/// Number of compute units for one ed25519 strict signature verification.
pub const ED25519_VERIFY_STRICT_COST: u64 = COMPUTE_UNIT_TO_US_RATIO * 80;
/// Number of compute units for one secp256r1 signature verification.
pub const SECP256R1_VERIFY_COST: u64 = COMPUTE_UNIT_TO_US_RATIO * 160;
/// Number of compute units for one write lock
pub const WRITE_LOCK_UNITS: u64 = COMPUTE_UNIT_TO_US_RATIO * 10;
/// Number of data bytes per compute units
pub const INSTRUCTION_DATA_BYTES_COST: u64 = 140 /*bytes per us*/ / COMPUTE_UNIT_TO_US_RATIO;
```

**File:** cost-model/src/transaction_cost.rs (L18-25)
```rust
impl<'a, Tx> TransactionCost<'a, Tx> {
    pub fn sum(&self) -> u64 {
        self.signature_cost
            .saturating_add(self.write_lock_cost)
            .saturating_add(u64::from(self.data_bytes_cost))
            .saturating_add(self.programs_execution_cost)
            .saturating_add(self.loaded_accounts_data_size_cost)
    }
```

**File:** cost-model/src/transaction_cost.rs (L309-312)
```rust
            let signature_cost = 2 * block_cost_limits::SIGNATURE_COST;
            let write_lock_cost = 2 * block_cost_limits::WRITE_LOCK_UNITS;
            let data_bytes_cost =
                vote_transaction.instruction_data_len() / (INSTRUCTION_DATA_BYTES_COST as u16);
```
