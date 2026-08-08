### Title
Hard-coded signature-verification cost constants in the cost model can become stale, enabling underpriced/QoS-evading signature-heavy transactions - (File: `cost-model/src/block_cost_limits.rs`)

### Summary
Analogous to the Cooler `maxLTC` bug (a hard-coded, non-oracle-fed exchange rate that becomes stale and lets under-collateralized loans through), Agave's cost model hard-codes the compute-unit (CU) price of expensive cryptographic operations — signature verification and secp256k1/ed25519/secp256r1 precompile verification — as fixed constants derived from a one-time cluster benchmark rather than any live, re-calibrated measurement. If real verification cost on validator hardware diverges upward from these constants (new/faster attack techniques to construct cheap-to-submit-but-expensive-to-verify transactions, added heavier precompiles, or simply CPU cost drift), an unprivileged user can craft transactions that are charged far less CU than they actually cost the network to sigverify and account for, evading the very budget these constants are meant to enforce.

### Finding Description
`cost-model/src/block_cost_limits.rs` defines: [1](#0-0) 

These are literal constants ("Cluster averaged compute unit to micro-sec conversion rate... Dashboard: https://metrics.solana.com/...") calibrated once from historical cluster telemetry, not from any live oracle, per-slot recalculation, or hardware-specific benchmark. `CostModel::get_signature_cost` uses them directly, unconditionally, to price every transaction's signature-verification work for cost-tracking/QoS purposes: [2](#0-1) 

This computed `signature_cost` feeds directly into `TransactionCost::sum()`, which is enforced against `MAX_BLOCK_UNITS` / `MAX_WRITABLE_ACCOUNT_UNITS` in the `CostTracker` that gates how many transactions/how much "cost" the banking stage/scheduler will admit into a block: [3](#0-2) 

Just as `maxLTC` hard-codes a DAI/gOHM rate that no longer reflects reality once gOHM's price moves, `SIGNATURE_COST`, `SECP256K1_VERIFY_COST`, `ED25519_VERIFY_STRICT_COST`, and `SECP256R1_VERIFY_COST` hard-code a "CU-per-verification" rate that no longer reflects reality once actual verification cost (CPU time to run these signature schemes, including any newly discovered expensive edge cases, e.g. secp256r1/ed25519 batch or malformed-input paths) diverges from the value baked in at `cost-model` design time. There is no mechanism analogous to a Chainlink oracle (i.e., no periodic re-benchmarking/feature-gated update tied to measured validator CPU cost) to correct this drift outside of a manual SIMD/hard-fork change.

### Impact Explanation
Because these constants directly determine how much "block budget" a signature-heavy transaction consumes, an attacker who can construct transactions whose real verification cost exceeds the hard-coded per-signature price can pack proportionally more real CPU work into a block than the cost model believes it is admitting. This is a QoS-evasion / grossly-underpriced-pre-fee-work condition: the fee/QoS system is supposed to bound the total real computational cost imposed on the leader and downstream replaying validators, but a stale constant lets that bound be silently violated, degrading CPU headroom in the banking stage/sigverify-adjacent packet path for legitimate unprivileged traffic without a compensating increase in fees paid.

### Likelihood Explanation
This requires the hard-coded constants to actually be out of sync with real verification cost on current validator hardware/algorithms — the same "hint, not proof" caveat as the original report. It is a systemic, config/hard-fork-level risk rather than a demonstrated exploit against a specific current value, similar to how the original finding was ultimately deemed valid on the reasoning that a fixed rate "renders the contract useless" once the market (here, real CPU cost) moves, without needing to show today's exact numeric mismatch.

### Recommendation
Periodically re-benchmark and update `SIGNATURE_COST`, `SECP256K1_VERIFY_COST`, `ED25519_VERIFY_STRICT_COST`, and `SECP256R1_VERIFY_COST` (e.g., via a SIMD process tied to measured validator CPU cost, similar to how `MAX_BLOCK_UNITS_SIMD_0256`/`MAX_BLOCK_UNITS_SIMD_0286` are versioned via feature gates) rather than leaving them as static constants indefinitely, and add monitoring/alerting that compares real measured sigverify CPU time against the modeled CU cost to detect drift before it can be exploited.

### Proof of Concept
Not directly exploitable without hardware-level CPU-cost benchmarking data showing current drift between `cost-model/src/block_cost_limits.rs` constants and actual measured verification cost on validator hardware; the vulnerability is structural (staleness risk of hard-coded pricing constants), analogous in kind — not in exact mechanism — to the referenced `maxLTC` issue.

### Citations

**File:** cost-model/src/block_cost_limits.rs (L7-20)
```rust
/// Cluster averaged compute unit to micro-sec conversion rate
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

**File:** cost-model/src/cost_model.rs (L129-151)
```rust
    /// Returns signature details and the total signature cost
    fn get_signature_cost(transaction: &impl TransactionMeta) -> u64 {
        let signatures_count_detail = transaction.signature_details();

        signatures_count_detail
            .num_transaction_signatures()
            .saturating_mul(SIGNATURE_COST)
            .saturating_add(
                signatures_count_detail
                    .num_secp256k1_instruction_signatures()
                    .saturating_mul(SECP256K1_VERIFY_COST),
            )
            .saturating_add(
                signatures_count_detail
                    .num_ed25519_instruction_signatures()
                    .saturating_mul(ED25519_VERIFY_STRICT_COST),
            )
            .saturating_add(
                signatures_count_detail
                    .num_secp256r1_instruction_signatures()
                    .saturating_mul(SECP256R1_VERIFY_COST),
            )
    }
```

**File:** cost-model/src/cost_tracker.rs (L87-96)
```rust
impl Default for CostTrackerLimits {
    fn default() -> Self {
        const _: () = assert!(MAX_WRITABLE_ACCOUNT_UNITS <= MAX_BLOCK_UNITS);
        Self {
            account_cost: MAX_WRITABLE_ACCOUNT_UNITS,
            block_cost: MAX_BLOCK_UNITS,
            allocated_data_size: MAX_BLOCK_ACCOUNTS_DATA_SIZE_DELTA,
        }
    }
}
```
