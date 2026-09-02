### Title
Protocol fee bypassed by fragmenting NEP-245/IMT `TokenDiff` withdrawals into unit-sized intents - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::token_fee` waives fees on `TokenIdType::Nep245`/`TokenIdType::Imt` whenever the delta observed by a *single* `TokenDiff` intent has `amount <= 1`, but the fee decision is made per-intent, not on the aggregate negative delta a signer moves across a whole `MultiPayload`/`execute_intents` batch. Since `TransferMatcher::finalize` (contracts/defuse/core/src/engine/state/deltas.rs:267-283) only enforces that deposits/withdrawals sum to zero across the whole batch and does not aggregate per-signer, per-token amounts for fee purposes, an attacker can split what would be one large fee-liable NEP-245/IMT trade into many `TokenDiff` intents each carrying `amount == 1`, collecting `Pips::ZERO` fee on every fragment while still moving the full aggregate amount.

### Finding Description
The broken binding: for a signer moving aggregate negative delta `N` on a `TokenId::Nep245`/`Imt` token in one settlement, the fee actually collected should equal `Pips::fee_ceil(protocol_fee, N)` (as it would be if submitted in a single `TokenDiff{diff: {token: -N, ...}}`). Instead, the code computes fee independently per intent: [1](#0-0) 

with `token_fee` explicitly zeroing the fee whenever the *per-intent* `amount <= 1` for `Nep245`/`Imt`: [2](#0-1) 

By submitting `N` separate `TokenDiff` intents, each `{mt_token: -1, other_token: +k_i}`, in a single `execute_intents`/`simulate_intents` batch (or across multiple signed payloads batched together), each call to `token_fee` sees `amount == 1` and returns `Pips::ZERO`, so `fees_collected` for the `mt_token` leg is `0` for every fragment, summing to `0` total, whereas a single `TokenDiff{mt_token: -N, ...}` would have charged `Pips::fee_ceil(protocol_fee, N)`.

The batch-level invariant enforced by `TransferMatcher::finalize` (contracts/defuse/core/src/engine/state/deltas.rs:267-392) only requires that the sum of all deposits/withdrawals for each `TokenId` across the whole batch nets to zero (matching senders to receivers); it performs no fee aggregation and is orthogonal to `token_fee`. It does not re-derive or cross-check fees against the aggregate per-token delta, so it does nothing to prevent this fragmentation.

To realize the exploit, the attacker needs the aggregate negative `mt_token` delta and positive counter-token delta(s) to be balanced within the batch (as with any trade/swap represented via `TokenDiff`), which the attacker can trivially satisfy using two accounts under their own control (self-trade) signing both legs themselves — no cooperation or authorization from a third party is needed, and this fits the "unprivileged attacker, sign with their own keys" profile.

### Impact Explanation
This is a systematic protocol fee bypass: the fee owed to `fee_collector` on NEP-245/IMT trades is unilaterally reducible to zero by any signer simply by splitting a `TokenDiff` intent's negative leg on that token into unit-sized fragments, regardless of how large the real aggregate movement is. This directly matches the Critical category "protocol fees bypassed or over-collected." It is repeatable indefinitely by any account, for any NEP-245/IMT token, and scales with the number of fragments the attacker is willing to submit in one transaction (bounded only by gas, which is out of scope for this evaluation). The blast radius is protocol-wide revenue leakage on the NEP-245/IMT token class whenever `protocol_fee > 0`; underlying token custody / solvency invariants (`TransferMatcher`) are not violated, only the fee-collector's `Amounts` balance is under-credited relative to the true aggregate exchanged amount.

### Likelihood Explanation
Preconditions are minimal: the attacker needs (a) a nonzero `protocol_fee` configured (attacker-controlled precondition is just that fee > 0, which is the normal operating configuration), (b) an NEP-245/IMT balance in Defuse to trade/withdraw via `TokenDiff`, and (c) either a genuine counterparty or, more simply, a second self-controlled account to balance the batch. All of this is achievable by an ordinary, unprivileged user with no special role, at the cost of only extra intents/gas per batch (which per the rules is out of scope to penalize, but does not prevent the finding). The technique is fully deterministic and repeatable across accounts, tokens, and batches.

### Recommendation
Compute and apply the `Nep245`/`Imt` fee-exemption threshold on the aggregate per-signer, per-token negative delta across the whole `DefuseIntents`/batch execution (e.g., accumulate negative deltas per `(signer_id, token_id)` before evaluating `token_fee`'s `amount > 1` condition), rather than per individual `TokenDiff` intent. Alternatively, remove the per-intent exemption entirely for `Nep245`/`Imt` and only exempt truly NFT-semantic token ids (e.g., via `TokenIdType::Nep171` or an explicit "1-of-1" marker), since NEP-245/IMT can represent fungible-like tokens where the `amount <= 1` heuristic is trivially gameable through intent fragmentation.

### Proof of Concept
```
cargo test in tests/src/tests/defuse/intents/token_diff.rs (or a new test module):

1. Setup: deploy Defuse with protocol_fee = X% (X > 0), create an MT contract,
   deposit a large NEP-245 balance (e.g. 1000 units of token `mt1`) to user A,
   and set up a counterparty (user B or a second account controlled by the
   same signer key) able to supply the offsetting `ft` leg.

2. Path 1 (single intent): user A signs ONE TokenDiff{diff: {mt1: -1000, ft: +1000}},
   B (or matching counterparty) signs the offsetting TokenDiff{mt1: +1000, ft: -1000}.
   Execute via env.defuse_execute_intents(...). Read fee_collector's mt1 balance
   -> expect fee_ceil(protocol_fee, 1000) > 0.

3. Path 2 (fragmented): user A signs 1000 separate TokenDiff intents, each
   {mt1: -1, ft: +1}, and B signs 1000 matching {mt1: +1, ft: -1} intents,
   all batched into one `execute_intents` call (MultiPayloadArgs with all 2000
   signed payloads). Read fee_collector's mt1 balance after execution.

4. Assertion (binding under test):
   assert_eq!(fee_collector_mt1_balance_path1, fee_collector_mt1_balance_path2);
   Expected/actual: this assertion FAILS — path 1 yields fee_ceil(protocol_fee, 1000) > 0,
   path 2 yields 0, demonstrating the fee bypass via fragmentation.
```

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L69-78)
```rust
            // take fees only from negative deltas (i.e. token_in)
            if *delta < 0 {
                let amount = delta.unsigned_abs();
                let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);

                // collect fee
                fees_collected
                    .add(token_id.clone(), fee)
                    .ok_or(DefuseError::BalanceOverflow)?;
            }
```

**File:** contracts/defuse/core/src/intents/token_diff.rs (L206-216)
```rust
    #[inline]
    pub fn token_fee(token_id: impl Into<TokenIdType>, amount: u128, fee: Pips) -> Pips {
        let token_id = token_id.into();
        match token_id {
            TokenIdType::Nep141 => {}
            TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}
            // do not take fees on NFTs and MTs with |delta| <= 1
            TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO,
        }
        fee
    }
```
