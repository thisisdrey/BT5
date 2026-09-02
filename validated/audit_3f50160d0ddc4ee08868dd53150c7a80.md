### Title
Protocol fee bypass on `Nep245`/`Imt` legs via unit-delta `TokenDiff` splitting - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` waives fees on `Nep245`/`Imt` (and `Nep171`) legs whenever the per-intent `|delta| <= 1`, but this exemption is evaluated per individual `TokenDiff` intent rather than per aggregate traded volume. An unprivileged signer can split one logical M-unit `Nep245`/`Imt` leg into M separate `TokenDiff` intents inside a single signed payload's `intents` vector, each carrying a delta of exactly ±1 on the same `TokenId`, making `token_fee` return `Pips::ZERO` for every leg and fully bypassing the protocol fee that would apply to a single M-magnitude intent.

### Finding Description
The broken binding: for a single logical trade of magnitude `M` on a `Nep245`/`Imt` token, the protocol is supposed to credit `fee_collector` with `Pips::fee_ceil(M)` (nonzero for nonzero `engine.state.fee()`). Actual credited amount when the trade is split into M unit legs == `0`.

Root cause is in `TokenDiff::execute_intent` and `TokenDiff::token_fee`: [1](#0-0) [2](#0-1) 

`token_fee` explicitly waives fees "on NFTs and MTs with `|delta| <= 1`" — a per-call check on `amount = delta.unsigned_abs()` for that single intent, with no aggregation across intents in the same batch/payload. `DefuseIntents.intents` is a `Vec<Intent>` executed sequentially within one signed payload, requiring only a single signature/nonce: [3](#0-2) 

An attacker holding balance `M` of a `Nep245`/`Imt` token constructs one `MultiPayload` (or even one signed `DefuseIntents`) containing M `TokenDiff` intents, each with `diff = {token_id: -1, other_token: +v_i}`, matched by a counterparty's (possibly attacker-controlled second account) M intents supplying `{token_id: +1, other_token: -v_i}`. Each of the M `TokenDiff::execute_intent` calls independently computes `Self::token_fee(token_id, 1, protocol_fee).fee_ceil(1) == 0` because `amount = 1`, so `fees_collected` stays empty and `internal_add_balance(fee_collector, ...)` is never invoked for that token.

The batch-level invariant enforced by `TransferMatcher::finalize` (via `Deltas<S>` intercepting `internal_add_balance`/`internal_sub_balance`) only requires that the sum of raw deposits/withdrawals recorded across the whole batch nets to zero per token: [4](#0-3) [5](#0-4) 

Since no fee is levied, no extra "slack" deposit needs to be matched by an additional withdrawal, so the M unit-legs trivially balance 1:1 against the counterparty's M unit legs, and the invariant passes with zero fee collected — unlike a single M-magnitude `TokenDiff`, where `token_fee` would apply `protocol_fee` and require the counterparty (via `closure_delta`/`closure_supply_delta`) to supply the fee slack.

None of the existing guards catch this: `MultiPayload::verify`/nonce checks only ensure signature/replay validity, not fee aggregation; `TransferMatcher::finalize` only checks net-zero balance flow, not that a "fair" fee was paid; there is no cross-intent aggregation of `TokenId` deltas before computing `token_fee`.

### Impact Explanation
Protocol fees are bypassed for any `Nep245`/`Imt` volume the attacker chooses to route through unit-magnitude `TokenDiff` legs, regardless of the total amount traded. This directly shorts `fee_collector`'s expected revenue (Critical: "protocol fees bypassed") and is fully repeatable — any account, any `Nep245`/`Imt` token, any batch, any number of times, at no cost beyond ordinary gas for the extra intents. Blast radius scales with the volume of MT/IMT trading routed through Defuse; a high-volume solver or market maker could permanently avoid fees on all such legs.

### Likelihood Explanation
Preconditions are trivial and fully within the unprivileged attacker's control: an account holding balance ≥ M of a `Nep245`/`Imt` token, a nonzero `Pips` fee configured, and a counterparty (which can be the same attacker's second controlled account) willing to accept unit-leg trades. No special role, relayer key, or upgrade is required — only the ability to sign and submit a `MultiPayload`/`execute_intents` call, which is available to anyone. The only cost is marginally higher gas from extra intents/log lines, which is negligible against the avoided fee for any nontrivial M.

### Recommendation
Aggregate `TokenDiff` deltas per `TokenId` across the entire batch (all intents/payloads in a single `execute_intents`/`simulate_intents` call) before applying the `amount <= 1` fee-waiver check in `token_fee`, rather than evaluating it per individual intent. Alternatively, remove or tighten the exemption so it only applies to genuinely atomic NFT-like transfers (`Nep171`, or `Nep245`/`Imt` tokens whose per-unit supply is provably indivisible), not to fungible-like `Nep245`/`Imt` balances that can be freely chunked into unit legs.

### Proof of Concept
Using `defuse-sandbox`'s `Env::builder().fee(...)` harness (mirroring `tests/src/tests/defuse/intents/token_diff.rs`):
1. Deploy/mint a `Nep245` (or `Imt`) token with `amount = M` (M > 1) to `user1`, and matching counter-token balance to `user2`.
2. Set nonzero `Pips` fee via `Env::builder().fee(fee)`.
3. Baseline: sign a single `TokenDiff` for `user1` with `diff = {mt_token: -M, other_token: +V}` matched by `user2`'s single `TokenDiff`; execute via `execute_intents`; assert `fee_collector` balance on `mt_token` increases by `Pips::fee_ceil(M)` (nonzero).
4. Exploit: sign a payload for `user1` containing M `TokenDiff` intents, each `diff = {mt_token: -1, other_token: +v_i}` (`sum(v_i) = V`), matched by `user2`'s M `TokenDiff` intents each `{mt_token: +1, other_token: -v_i}`; execute via `execute_intents`.
5. Assert `fee_collector`'s balance delta on `mt_token` is `0` in step 4, versus `Pips::fee_ceil(M) > 0` expected/observed in step 3 — proving the fee is bypassed purely by restructuring the same net trade into unit legs.

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

**File:** contracts/defuse/core/src/intents/mod.rs (L30-37)
```rust
pub struct DefuseIntents {
    /// Sequence of intents to execute in given order. Empty list is also
    /// a valid sequence, i.e. it doesn't do anything, but still invalidates
    /// the `nonce` for the signer
    /// WARNING: Promises created by different intents are executed concurrently and does not rely on the order of the intents in this structure
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub intents: Vec<Intent>,
}
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L136-164)
```rust
    fn internal_add_balance(
        &mut self,
        owner_id: AccountId,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        for (token_id, amount) in tokens {
            self.state
                .internal_add_balance(owner_id.clone(), [(token_id.clone(), amount)])?;
            if !self.deltas.deposit(owner_id.clone(), token_id, amount) {
                return Err(DefuseError::BalanceOverflow);
            }
        }
        Ok(())
    }

    fn internal_sub_balance(
        &mut self,
        owner_id: &AccountIdRef,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        for (token_id, amount) in tokens {
            self.state
                .internal_sub_balance(owner_id, [(token_id.clone(), amount)])?;
            if !self.deltas.withdraw(owner_id.to_owned(), token_id, amount) {
                return Err(DefuseError::BalanceOverflow);
            }
        }
        Ok(())
    }
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L265-283)
```rust
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
```
