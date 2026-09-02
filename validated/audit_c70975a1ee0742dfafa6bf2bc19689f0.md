### Title
Protocol fee bypass on `TokenDiff` for `Nep245`/`Imt` tokens via unit-amount intent splitting - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` decides whether to charge the protocol fee based solely on the `amount` of a single `TokenDiff` intent's delta for a token, exempting `Nep245`/`Imt` tokens whenever that single intent's `|delta| <= 1`. Because fees are computed and collected independently per `TokenDiff::execute_intent` call rather than on the aggregate amount moved per token per signer across a batch, a signer can split any bulk transfer of a `Nep245`/`Imt` token into N intents of `delta = -1` each and pay zero protocol fee in total, instead of `protocol_fee.fee_ceil(N)` that a single `delta = -N` intent would incur.

### Finding Description
Binding claimed broken: `sum_{i=1..N}(fee credited to fee_collector for T in call i) == Pips::fee_ceil(protocol_fee, N)`.

The fee logic lives in `TokenDiff::execute_intent`: [1](#0-0) 
and the threshold check in `TokenDiff::token_fee`: [2](#0-1) 

For `TokenIdType::Nep245 | TokenIdType::Imt`, if `amount > 1` the branch falls through to `fee`; otherwise (`amount <= 1`) it returns `Pips::ZERO`. This `amount` is `delta.unsigned_abs()` taken from a *single* `TokenDiff` struct's diff map entry — it has no memory of, or aggregation with, other `TokenDiff` intents executed earlier or later in the same `execute_intents`/`simulate_intents` batch, even if they touch the same `TokenId` and same `signer_id`.

The only cross-intent bookkeeping in a batch is `TransferMatcher`, which enforces that all deltas across all intents in the batch net to zero per token (via `internal_add_balance`/`internal_sub_balance` recording into `TransferMatcher::deposit`/`withdraw`, finalized by `TransferMatcher::finalize`): [3](#0-2) [4](#0-3) 
This mechanism only checks conservation of token amounts between senders and receivers; it does not recompute or validate fees, and it does not prevent the same signer from submitting the same net delta as many 1-unit legs instead of one N-unit leg.

Exploit flow: attacker controls two of their own accounts (or one attacker account and one cooperating counterparty/solver account they arrange). Instead of submitting one `TokenDiff` `{T: -N, B: +M}` (which would call `token_fee(T, N, protocol_fee)` returning `protocol_fee` since `N>1`, and collect `protocol_fee.fee_ceil(N)` from `T`), the attacker signs (or batches) N separate `TokenDiff` intents, each `{T: -1, B: +m}` where `m` is 1/N-th (rounded) of the closure amount, matched by N corresponding `+1 T` legs on the counterparty side. Each of the N `execute_intent` calls independently computes `token_fee(T, 1, protocol_fee)`, hits the `amount <= 1` branch, and returns `Pips::ZERO`, so `fee_ceil(1) == 0` every time. `TransferMatcher::finalize` still succeeds because the aggregate deltas per token net to zero across the batch, so nothing blocks execution. No existing guard (`MultiPayload::verify`, nonce checks, `TransferMatcher::finalize`, `assert_one_yocto`) inspects aggregate per-token amounts for fee purposes — they only verify signatures/nonces and delta conservation, not fee correctness.

### Impact Explanation
Value that should have been credited to `fee_collector` (Critical category: "protocol fees bypassed") never leaves the trading accounts. The attacker/counterparty pair keeps `protocol_fee.fee_ceil(N)` worth of token `T` that would otherwise go to the fee collector. This is repeatable for any `Nep245`/`Imt` token, any volume `N`, and any pair of accounts (including two accounts owned by the same attacker), across as many batches as desired, with no privileged role required.

### Likelihood Explanation
Preconditions are trivial for an unprivileged actor: hold balances of a `Nep245`/`Imt` token inside the Verifier, have `protocol_fee > Pips::ZERO` configured (default deployment condition), and be able to construct/sign `MultiPayload`s with multiple `TokenDiff` intents (a normal, unrestricted user capability). Cost is only extra transaction/gas overhead for N intents instead of one; no lock or nonce restriction prevents this, since nonces are per-payload and plentiful.

### Recommendation
Compute and validate protocol fees for `Nep245`/`Imt` tokens on the aggregate amount transacted per `(signer_id, token_id)` across the whole intents batch (i.e., inside the engine/`TransferMatcher` finalize step, or by accumulating per-token per-signer negative deltas before applying the `amount > 1` exemption), rather than per individual `TokenDiff` intent's local delta.

### Proof of Concept
`cargo test` plan (workspace/sandbox test, e.g. added to `tests/src/tests/defuse/intents/token_diff.rs`):
1. Deploy Defuse with `protocol_fee = Pips::ONE_PERCENT` (or any `> 0`) and a distinct `fee_collector`.
2. Create an `Nep245` (or `Imt`) token `T` inside the Verifier and deposit `N` units (e.g. `N = 100`) to attacker account `A`; ensure counterparty account `B` has enough of asset `X` to close the trade.
3. **Single-leg baseline**: Sign one `TokenDiff` payload from `A`: `{T: -N, X: +closure}` matched by `B`'s `{T: +N, X: -closure}`. Execute via `execute_intents`. Assert `fee_collector`'s balance of `T` equals `protocol_fee.fee_ceil(N) > 0`.
4. **Split-leg attack**: Reset state (fresh accounts/token or new run). Sign `N` separate `TokenDiff` payloads from `A`, each `{T: -1, X: +closure_i}`, matched by `N` corresponding `{T: +1, X: -closure_i}` payloads from `B`. Execute all via one `execute_intents` batch (or `N` sequential calls).
5. Assert `fee_collector`'s post-batch balance of `T` equals `0`, while the aggregate `T` moved from `A` to `B` equals `N`, proving `sum(fee legs) = 0 != protocol_fee.fee_ceil(N)` from step 3 — confirming the FEES binding is broken.

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
