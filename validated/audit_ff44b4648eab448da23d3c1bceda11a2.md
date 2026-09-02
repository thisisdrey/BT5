### Title
Fee bypass on multi-token/IMT `TokenDiff` intents via amount-splitting - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee()` deliberately waives the protocol fee whenever the transferred amount for a `Nep245`/`Imt` token entry is `≤ 1`, on the assumption these are indivisible NFT-like transfers. However, `Nep245`/`Imt` token IDs can represent fungible-style sub-tokens with large divisible balances, and the fee decision is made per `TokenDiff` intent entry rather than on the net amount moved for that token across a batch. This lets a user split a large multi-token transfer into many single-unit `TokenDiff` intents, each individually qualifying for the zero-fee carve-out, to move an arbitrarily large total amount of the same token while paying no protocol fee — exactly the "loop vs. single call not equivalent" pattern from the analog report, here breaking the *fees owed vs. fees collected* binding instead of a compounding-interest binding.

### Finding Description
The fee is computed independently for each `(token_id, delta)` entry of a signed `TokenDiff` in `execute_intent`: [1](#0-0) 

and the fee rate itself is selected by `token_fee()`: [2](#0-1) 

For `Nep245`/`Imt` token types, if the per-entry `amount` (i.e., `delta.unsigned_abs()` for that single `TokenDiff`) is `≤ 1`, the fee rate returned is `Pips::ZERO`; otherwise the configured protocol `fee` applies. This decision is scoped to a single intent's delta, not to the cumulative amount of that token moved by an account (or a colluding pair of accounts) across a batch of intents executed together (`execute_intents`/`MultiPayloadArgs` allow submitting many signed `TokenDiff` intents in one call, and the settlement invariant only requires that the sum of deltas across the whole batch net to zero — see `TransferMatcher::finalize` / `InvariantViolated::UnmatchedDeltas`): [3](#0-2) 

Because fee is assessed per intent entry rather than per aggregate movement, an attacker (or two colluding counterparties who need to construct a zero-sum swap anyway, as is required by the settlement invariant) can replace a single `TokenDiff{token_id: mt, delta: -N}` (which would incur `fee_ceil(N)` under the normal `fee`) with `N` separate `TokenDiff` intents each carrying `delta: -1` for the same `mt` token_id. Each individual entry has `amount == 1`, so `token_fee` returns `Pips::ZERO` for every one of them, and the total fee collected for moving `N` units becomes `0` instead of `fee.fee_ceil(N)`.

This is directly analogous to the RPL `inflationCalculate()` bug: computing a per-step (per-interval / per-intent) result and expecting it to equal the aggregate result of a single computation is only valid without truncation/threshold effects; here the "amount > 1" threshold check, applied per intent rather than per aggregate, is the source of the discrepancy, and it is exploitable in the *unfavorable-to-protocol* direction (fee evasion) rather than merely a rounding nuisance.

### Impact Explanation
This crosses the explicitly allowed "fees owed vs. fees collected" boundary. Any account (or solver/user pair settling a swap of a fungible-style multi-token asset) can bypass the protocol fee entirely on `Nep245`/`Imt` token transfers by decomposing the transfer into unit-sized intents, at the cost of needing to sign/submit `N` intents instead of one. This is a Critical-tier fee-bypass per the stated impact categories ("fees bypassed or over-collected"), since it results in the protocol/fee collector receiving strictly less than the intended fee for a real transfer of value.

### Likelihood Explanation
Likelihood is moderate: it requires (a) a token accounted as `Nep245`/`Imt` in the intents contract with meaningfully large, divisible balances (not a true 1-of-1 NFT), and (b) the attacker being willing/able to submit `N` intents (as multiple `TokenDiff` entries within one or several signed payloads passed to `execute_intents`) to move `N` units. For moderate `N` (bounded by gas/intent-count limits per transaction) this is practical to exploit for any fee percentage that makes the savings exceed the added transaction cost; for larger `N` it may require multiple transactions, but there is no on-chain limit preventing repetition.

### Recommendation
Compute the fee-eligibility threshold (and the fee itself) based on the net aggregate amount moved for a given `token_id` across the whole batch/settlement (i.e., after aggregating all `TokenDiff` deltas for that token, analogous to how `closure_deltas` already aggregates supply deltas per token before computing closures), rather than per individual `TokenDiff` entry. Alternatively, restrict the zero-fee carve-out strictly to token types that are provably non-divisible (true NFTs), and always apply the fee to `Nep245`/`Imt` transfers regardless of per-entry amount, closing the amount-splitting loophole.

### Proof of Concept
1. Protocol fee is configured to a nonzero value (e.g., `Pips::ONE_PERCENT`) and a `Nep245` token (fungible sub-token within a multi-token contract) has a balance of `N = 1000` held by user A, intended to be swapped with solver B.
2. Instead of signing one `TokenDiff` with `diff = {mt_token: -1000, other_token: +X}`, which would incur `fee.fee_ceil(1000)` on the `mt_token` leg (see `execute_intent`, lines 69–78), user A signs `1000` separate `TokenDiff` intents, each `diff = {mt_token: -1, other_token: +x_i}` (with `x_i` summing to `X`), matched by solver B's counter-intents so the batch nets to zero per the `TransferMatcher` settlement check.
3. For each of the 1000 intents, `token_fee(mt_token, 1, fee)` returns `Pips::ZERO` (per `TokenDiff::token_fee`, lines 206–216), so `fee_ceil(1)` computed at line 72 of `token_diff.rs` is `0` for every entry.
4. Total fee collected across the batch is `0`, whereas the economically equivalent single-intent transfer of `1000` units would have collected `fee.fee_ceil(1000) > 0`. The batch still satisfies the settlement invariant (`TransferMatcher::finalize` returns `Ok`), so `execute_intents` succeeds with the fee fully bypassed. [4](#0-3) [2](#0-1)

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
