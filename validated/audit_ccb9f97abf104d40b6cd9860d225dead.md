### Title
Protocol fee bypass on NEP-245 token diffs via unit-size chunking - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` computes the fee independently for each `TokenDiff` leg and exempts NEP-245 (and NFT/IMT) legs whose `|delta| <= 1` from any fee. Because the fee is evaluated per‑intent rather than on the aggregate amount of a token moved in a batch, an unprivileged signer (together with any counterparty willing to trade) can split a large NEP-245 trade into N separate `delta = ±1` legs and pay zero protocol fee, whereas an equivalent single `delta = ±N` diff would incur `Pips::fee_ceil(protocol_fee, N)`.

### Finding Description
The broken binding is:

`fees_collected.amount_for(nep245_token_id)` after `execute_signed_intents` `== Pips::fee_ceil(protocol_fee, N)` for a total notional of `N` units transferred.

In `TokenDiff::execute_intent` [1](#0-0) , for every negative delta in a single `TokenDiff.diff` map, the fee is computed as:

```
let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
```

`TokenDiff::token_fee` explicitly zeroes the fee for `Nep245`/`Imt`/`Nep171` tokens when `amount <= 1`: [2](#0-1) 

This decision is made purely on the *size of a single leg's delta*, not on the total volume of the token moved across the whole `MultiPayload`/batch. Since `DefuseIntents` carries a `Vec<Intent>` under one signature/nonce, and `execute_signed_intents` can process many signed payloads in one call [3](#0-2) , an attacker (in cooperation with any counterparty account, which can be their own second account or a real trading partner) can express a trade of N units of a NEP-245 token as N separate `TokenDiff` intents, each `{nep245_token_id: -1}` matched by a `+1` counter-leg elsewhere in the batch. `TransferMatcher::finalize` only requires that, globally across the whole batch, deposits and withdrawals per token net to zero [4](#0-3)  — it does not aggregate per-leg amounts for fee purposes, so nothing forces the N unit-sized legs to be treated as a single N-unit transfer.

Each of the N legs hits `amount == 1`, so `token_fee` returns `Pips::ZERO` for every leg, and `fees_collected` accumulates to `0` for the whole batch, while a single `TokenDiff` with `delta = -N` (`N > 1`) would compute `token_fee(..) == protocol_fee` and charge `Pips::fee_ceil(protocol_fee, N) > 0`.

None of the existing guards prevent this: `MultiPayload::verify`, nonce/salt checks, and `TransferMatcher::finalize`'s zero-sum invariant all operate correctly and are unrelated to fee sizing; the fee computation itself is the root cause, evaluated strictly per-leg.

### Impact Explanation
The `fee_collector` under-collects protocol fees on NEP-245 (multi-token) trades of any size, for any pair of unprivileged accounts willing to structure their trade as many 1-unit legs instead of one N-unit leg. This is repeatable without limit across tokens, accounts, and batches, and requires no privileged role, matching the Critical category "protocol fees bypassed."

### Likelihood Explanation
No special preconditions beyond two unprivileged accounts (attacker's own second account suffices) each holding balances of the traded tokens and being able to sign `MultiPayload`s. The mechanism is trivial to construct (repeat the same `{token: -1}`/`{token: +1}` pattern N times) and costs only the price of constructing/submitting a larger transaction — well within reach of any caller of `execute_intents`/`simulate_intents`.

### Recommendation
Compute the NEP-245/IMT fee exemption based on the aggregated net amount of the token moved across the whole batch (post `TransferMatcher::finalize`), not on the per-leg `TokenDiff` delta, or remove the `amount <= 1` special-case for `Nep245`/`Imt` token types that can represent fungible-like balances, restricting the exemption to genuinely non-fungible token semantics only.

### Proof of Concept
```rust
// cargo test in contracts/defuse/core (or an engine-level integration test)
// 1. Construct N=1000 TokenDiff intents (can be packed in one signed DefuseIntents
//    message or across multiple signed MultiPayloads), each:
//    diff = { nep245_token_id: -1 } paired with a matching +1 leg from a
//    counterparty account (or a second attacker-controlled account) on the same
//    token_id, so that TransferMatcher::finalize nets to zero globally.
// 2. Call engine.execute_signed_intents(signed_payloads).
// 3. Assert TokenDiffEvent::fees_collected sums to 0 across all N intents.
// 4. Compare against Pips::ONE_PERCENT.fee_ceil(1000), which is > 0,
//    demonstrating the fee that would have been owed for an equivalent
//    single delta = -1000 TokenDiff intent, per TokenDiff::token_fee's
//    `amount > 1` branch.
```

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

**File:** contracts/defuse/core/src/engine/mod.rs (L32-40)
```rust
    pub fn execute_signed_intents(
        mut self,
        signed: impl IntoIterator<Item = MultiPayload>,
    ) -> Result<Transfers> {
        for signed in signed {
            self.execute_signed_intent(signed)?;
        }
        self.finalize()
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
