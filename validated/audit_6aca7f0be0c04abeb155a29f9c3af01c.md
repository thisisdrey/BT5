### Title
Protocol fee bypass on NEP-245 fungible-style tokens via `|delta|==1` splitting - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` exempts a Nep245 (multi-token) leg from any protocol fee whenever the *per-intent* `|delta|` is `1`, regardless of how much of that same `TokenId` is actually being traded in the batch. Because fee eligibility is evaluated per `TokenDiff` intent rather than per aggregate signer/token amount in the `MultiPayload`, a signer can split one large negative delta into `N` separate `TokenDiff` intents each with `delta == -1`, driving total collected fees to `0` instead of `Pips::fee_ceil(protocol_fee, N)`.

### Finding Description
The broken binding is:

`sum_i fees_collected_i[T] == Pips::fee_ceil(protocol_fee, sum_i |delta_i|[T])` for all `TokenDiff` intents `i` executed by the same signer on the same Nep245 `TokenId` `T` within one batch.

In `TokenDiff::execute_intent` [1](#0-0) , for each negative delta the code computes `amount = delta.unsigned_abs()` **local to that single intent's diff entry**, then calls `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)`. `token_fee` is defined as: [2](#0-1) 

For `TokenIdType::Nep245`, if `amount > 1` the protocol `fee` applies; if `amount == 1` (or the delta type is `Nep171`), `Pips::ZERO` is returned — a deliberate exemption for NFT-style "amount ≤ 1" transfers, per the inline comment "do not take fees on NFTs and MTs with `|delta| <= 1`".

The flaw is that NEP-245 `TokenId`s can represent fungible balances (large amounts under one `token_id`), not just unique NFT-like units, yet the exemption is keyed purely off the magnitude of the delta inside a single `TokenDiff` intent, with no aggregation across the other `TokenDiff` intents that share the same signer/token in the same `MultiPayload` batch. Root cause: the fee model has no concept of "total amount transferred for this token in this batch" — only "amount in this one intent's delta".

Exploit flow: instead of signing one `TokenDiff` with `diff: {T: -N}` (which would compute `fee = Pips::fee_ceil(protocol_fee, N) > 0`), the attacker (and a normal trading counterparty, e.g. a solver or their own second account) sign `N` separate `TokenDiff` intents, each `diff: {T: -1, T_out: +m}`, packed into the same `execute_intents`/`simulate_intents` `MultiPayload` batch. Each execution independently hits the `amount == 1` branch, returning `Pips::ZERO`, so `fees_collected` is empty for every leg. The `TransferMatcher`/`finalize` invariant check (`contracts/defuse/core/src/engine/state/deltas.rs`) [3](#0-2)  only verifies that per-token withdrawals and deposits net to zero across the whole batch; it has no separate check that fees match what a single equivalent `TokenDiff` would have produced, so a batch of unit legs matched 1:1 by the counterparty passes cleanly with zero fee credited to `fee_collector`.

None of the existing guards (`MultiPayload::verify`, nonce/salt checks, `Lock`, `assert_one_yocto`, `access_control_any`) touch this fee-calculation logic; they authenticate/authorize the intents but do not constrain how a signer chooses to decompose their trade into multiple `TokenDiff` legs.

### Impact Explanation
Every unit of protocol fee that should be levied on Nep245 (multi-token, including fungible-style MT) trades can be avoided entirely by chunking the trade into `|delta|==1` legs, at no cost beyond issuing more intents in the same payload. This is value that should flow to `fee_collector` and instead stays with the trading parties — matching the Critical category "protocol fees bypassed... against the fee_collector." It is fully repeatable, across any account, any Nep245 token, and any batch size, with no dependency on privileged roles.

### Likelihood Explanation
Preconditions are trivial for an unprivileged actor: hold (or deposit) sufficient Nep245 balance in the Verifier, and find/be a normal trading counterparty willing to swap the same aggregate amount (a completely ordinary DEX/solver interaction). No role, relayer key, or upgrade access is required — only signing power over one's own `DefusePayload`s and the ability to submit a `MultiPayload` batch via `execute_intents`/`simulate_intents`. Cost scales with the number of legs (more intents = more payload/gas), but is otherwise unbounded and fully attacker-controlled.

### Recommendation
Compute the Nep245/Imt fee exemption based on the aggregate absolute amount moved for a given `(signer, TokenId)` pair across the whole batch (or track cumulative per-token negative delta across all `TokenDiff` intents in `Engine`/`Deltas` before applying `token_fee`), rather than the magnitude of a single intent's delta. Alternatively, remove or tighten the "amount ≤ 1 is fee-free" exemption so it only applies to token ids provably restricted to amount ≤ 1 (true NFTs), not general NEP-245 ids that can carry fungible balances.

### Proof of Concept
`cargo test` (workspace `tests` crate, sandbox-based, mirroring `tests/src/tests/defuse/intents/token_diff.rs`):
1. Deploy Defuse with `fee = Pips::ONE_PERCENT` (nonzero) and a Nep245 (MT) contract; deposit a signer with balance `N` (e.g. `N = 1000`) of a single Nep245 `TokenId` `T`, and a counterparty with sufficient balance of a Nep141 token `U` to swap against it.
2. **Baseline**: sign a single `TokenDiff { diff: {T: -1000, U: +closure} }` from the signer plus the matching counterparty `TokenDiff`; execute via `execute_intents`; assert `fee_collector` balance of `T` equals `Pips::ONE_PERCENT.fee_ceil(1000) > 0` (via `TokenDiffEvent::fees_collected` in logs or `mt_balance_of` on `fee_collector`).
3. **Exploit**: reset state; sign `1000` separate `TokenDiff` intents from the signer, each `{T: -1, U: +closure_per_unit}`, plus `1000` matching counterparty `TokenDiff` intents, all packed into one `MultiPayload` batch; execute via `execute_intents`.
4. Assert: batch executes successfully (`invariant_violated` is `None`), total `T` transferred equals `1000`, but summed `fees_collected[T]` across all `TokenDiffEvent`s (or `fee_collector`'s `T` balance) is `0`, while `Pips::ONE_PERCENT.fee_ceil(1000) > 0` — demonstrating `sum(fees_collected[T]) == 0 != Pips::fee_ceil(protocol_fee, 1000)`.

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

**File:** contracts/defuse/core/src/intents/token_diff.rs (L206-217)
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
