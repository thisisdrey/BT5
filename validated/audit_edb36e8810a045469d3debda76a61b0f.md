### Title
Fee bypass on NEP-245/IMT via nonce-splitting of a single logical transfer - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::execute_intent` computes the fee independently per intent via `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)`, and `token_fee` returns `Pips::ZERO` for `TokenIdType::Nep245`/`Imt` whenever `amount <= 1` [1](#0-0) . Because fee assessment has no cross-intent aggregation, a signer can split one logically atomic `-2` delta on such a token into two separately-signed, single-unit `-1` deltas (two nonces) inside one `MultiPayload`, causing the "amount > 1" fee branch to never trigger, while the final net effect on the signer's Verifier balance is identical to a single `-2` intent.

### Finding Description
The broken binding: for a fixed total withdrawal `amount == 2` of a NEP-245/IMT token by one signer within one settlement, `fee_collected` should equal `Pips::fee_ceil(protocol_fee, 2)` regardless of how the withdrawal is expressed across intents in the batch. In the actual code this binding does not hold:

- Single intent: `diff = {T: -2, T2: +X}}` → `token_fee(T, 2, fee)` takes the `amount > 1` branch → returns `fee` → `fee_ceil(2)` charged [2](#0-1) .
- Two intents, each `diff = {T: -1, T2: +X/2}}`, signed with two distinct nonces in the same `MultiPayload`: each call to `execute_intent` independently evaluates `token_fee(T, 1, fee)`, which for `Nep245`/`Imt` with `amount == 1` falls into the exempt arm (`TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO`) [3](#0-2) . Both calls charge zero fee, so `fees_collected` for the batch is `0` instead of `fee_ceil(2)`.

Balances end up net-equivalent for the signer (net `-2` of T either way), and the batch-level `TransferMatcher`/`Deltas` machinery only checks that deltas across accounts net to zero for solvency — it aggregates by `(token_id, owner_id)` for matching transfers but does **not** re-derive or check the fee that should have been charged on the aggregate delta [4](#0-3) . Existing guards (`Nonce`/`VersionedNonce` commit, signature verification, `TransferMatcher::finalize` invariant check) only prevent signature forgery, nonce replay, and balance-non-conservation — none of them re-evaluate `token_fee` on an aggregated basis, so nothing catches or corrects the fee shortfall.

Attacker's exact payload: two `DefusePayload`s from the same `signer_id`, each with a distinct valid `nonce`, each containing one `TokenDiff` intent with `diff: {T: -1, T2: +delta_i}` (T2 could be any counter-token with a matching counterparty in the batch), submitted together in one `MultiPayloadArgs` to `execute_intents`.

### Impact Explanation
Protocol fee revenue that should be credited to `fee_collector` (via `internal_add_balance` in `TokenDiff::execute_intent`, lines 96-101) is under-collected/bypassed for any NEP-245 or IMT token transfer whenever the withdrawn amount is at or slightly above the `> 1` threshold. This is repeatable per signer, per token, per batch — an attacker (or any relayer/solver aggregating many users' single-unit legs) can always express a multi-unit MT/IMT withdrawal as N separate 1-unit `TokenDiff` intents to zero out fees entirely, regardless of total volume, as long as counterparties exist to net the balances via `TransferMatcher`. This matches the "protocol fees bypassed" Critical impact category, since fee collector never receives value it is entitled to. [5](#0-4) 

### Likelihood Explanation
Preconditions are minimal for an unprivileged actor: the signer needs only their own signing key, ≥2 units of a NEP-245/IMT token they already own in the Verifier, and the ability to construct two `DefusePayload`s with distinct nonces (routine, always available) submitted in one `execute_intents`/`simulate_intents` batch, with a counterparty (also possibly controlled by the same attacker across two accounts, or another party in a normal swap) supplying the matching opposite deltas so `TransferMatcher::finalize` succeeds without `InvariantViolated`. No special role, relayer key, or timing race is required — the "ordering" aspect in the question is largely irrelevant since the exploit doesn't depend on which nonce executes first; both branches independently compute `Pips::ZERO` regardless of order. This is a straightforward, deterministic, and fully repeatable fee-avoidance technique costing the attacker nothing beyond normal transaction/signing overhead.

### Recommendation
Aggregate the fee-relevant `amount` per `(signer_id, token_id)` across the entire intent batch (or across the full set of `TokenDiff` intents processed within a single `execute_intents` call) before applying the `amount > 1` NEP-245/IMT exemption, rather than evaluating `token_fee` independently per `TokenDiff::execute_intent` invocation. Alternatively, remove the per-call `amount <= 1` exemption for NEP-245/IMT and instead charge fees on total signer balance deltas tracked centrally (e.g., inside `Deltas`/`TransferMatcher`) so that splitting a delta across nonces cannot change the total fee charged.

### Proof of Concept
```rust
// cargo test in contracts/defuse/core (or tests/ workspace, near-workspaces sandbox)
// 1. Setup: one NEP-245 token T with protocol_fee = Pips::ONE_PERCENT (or any nonzero fee).
//    Attacker/signer A holds balance_of(A, T) == 2, plus a counterparty B able to
//    receive T2 and send T2 back so TransferMatcher nets to zero.
//
// Case 1: single intent, delta = -2
let diff_single = TokenDiff { diff: TokenDeltas::from([(T.clone(), -2), (T2.clone(), X)]), .. };
// sign as nonce n1, execute alone (with matching counterparty intent for T2/T)
// assert: fee_collector balance_of(T) == Pips::fee_ceil(fee, 2)

// Case 2: same net effect for A, split into two -1 intents with distinct nonces n2, n3
let diff_a = TokenDiff { diff: TokenDeltas::from([(T.clone(), -1), (T2.clone(), X/2)]), .. };
let diff_b = TokenDiff { diff: TokenDeltas::from([(T.clone(), -1), (T2.clone(), X/2)]), .. };
// sign diff_a with n2, diff_b with n3, submit both (+ matching counterparty legs) in ONE MultiPayload
// execute_intents(...)
// assert: fee_collector balance_of(T) == 0   // vs fee_ceil(fee, 2) in Case 1

assert_ne!(
    fee_collector_balance_case_1,   // Pips::fee_ceil(fee, 2), > 0
    fee_collector_balance_case_2,   // 0
);
```
This demonstrates that for an identical net `-2` delta on the same signer/token, the fee collected diverges from `fee_ceil(fee, 2)` down to `0` purely based on how the signer chooses to split the delta across signed intents in the same batch.

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L59-78)
```rust
        for (token_id, delta) in &self.diff {
            if *delta == 0 {
                return Err(DefuseError::InvalidIntent);
            }

            // add delta to signer's account
            engine
                .state
                .internal_apply_deltas(signer_id, [(token_id.clone(), *delta)])?;

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

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L260-284)
```rust
    #[inline]
    pub fn add_delta(&mut self, owner_id: AccountId, token_id: TokenId, delta: i128) -> bool {
        self.0.entry_or_default(token_id).add_delta(owner_id, delta)
    }

    // Finalizes all transfers, or returns unmatched deltas.
    // If unmatched deltas overflow, then Err(None) is returned.
    pub fn finalize(self) -> Result<Transfers, InvariantViolated> {
        let mut transfers = Transfers::default();
        let mut deltas = TokenDeltas::default();
        for (token_id, transfer_matcher) in self.0 {
            if let Err(unmatched) = transfer_matcher.finalize_into(&token_id, &mut transfers)
                && (unmatched == 0 || deltas.apply_delta(token_id, unmatched).is_none())
            {
                return Err(InvariantViolated::Overflow);
            }
        }
        if !deltas.is_empty() {
            return Err(InvariantViolated::UnmatchedDeltas {
                unmatched_deltas: deltas,
            });
        }
        Ok(transfers)
    }
}
```
