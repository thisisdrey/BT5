### Title
Fee bypass on NEP-245/IMT `TokenDiff` trades by splitting a single logical trade into unit-size (`|delta| == 1`) legs - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::token_fee` waives the protocol fee for `TokenIdType::Nep245`/`Imt` whenever the per-intent `amount` (the `|delta|` on that single token inside that single signed `TokenDiff`) is `<= 1`, and `TokenDiff::execute_intent` computes and collects the fee strictly per-intent, per-token, using only that intent's own `amount`. Because the fee decision is made at intent-leg granularity rather than on the netted trade volume for a `(signer, token)` pair, a trader can split a volume-`N` sell of a NEP-245/IMT `TokenId` into `N` separate signed `TokenDiff` intents (each `diff = {token_T: -1}`), submitted together with a matching `+N` credit elsewhere in the same `MultiPayload` batch, and pay `Pips::ZERO` fee on every leg instead of `Pips::fee_ceil(protocol_fee, N)`.

### Finding Description
Binding claimed broken: `sum(fees_collected[T] across the batch) == Pips::fee_ceil(protocol_fee, sum_of_|negative deltas of T|)`.

Code path: `TokenDiff::execute_intent` [1](#0-0)  loops over `self.diff` (a single signed intent's own map), and for every negative `delta` computes `let amount = delta.unsigned_abs(); let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);` — `amount` here is scoped to *this one intent's* delta on that token, not any batch-wide aggregate.

`Self::token_fee` explicitly special-cases NFTs/MTs: [2](#0-1)  — for `Nep245`/`Imt`, if `amount > 1` the normal `fee` is charged, otherwise (`amount <= 1`) it returns `Pips::ZERO` ("do not take fees on NFTs and MTs with |delta| <= 1"). This waiver was presumably intended for genuine one-off NFT transfers, but nothing ties it to the *aggregate* size of a trade — it is evaluated once per `TokenDiff` intent, per token.

`fee_ceil` rounds up any nonzero rate times nonzero amount to at least 1 unit [3](#0-2) , so `Pips::fee_ceil(protocol_fee, N) > 0` for any `protocol_fee > 0` and `N >= 1`, while `N` separate `fee_ceil(protocol_fee, 1)` calls each hit the `amount <= 1` branch and return `0`.

Batch-level accounting only enforces that total deltas for each token net to zero across the whole `MultiPayload` (via `TransferMatcher`/`Deltas::finalize`) [4](#0-3)  — this checks conservation of the underlying token balance, but it does **not** check or enforce that the *fee* charged corresponds to the netted trade size. So an attacker (or two colluding accounts, or the same signer using several signed messages plus one counter-leg) can present a volume-`N` MT/NFT trade as `N` separate `-1` legs (each in its own signed `TokenDiff`) matched by any counterparty leg(s) summing to `+N` for that `TokenId`, and every one of the `N` legs independently lands in the `amount <= 1` fee-free branch, while a single `-N` intent for the same trade would have incurred `Pips::fee_ceil(protocol_fee, N) > 0`.

None of the standard guards (`MultiPayload::verify`, nonce/salt checks, `Lock`, `TransferMatcher::finalize`, `assert_one_yocto`) address this because they validate signatures, replay-protection, and balance conservation — not fee-rate granularity. The root cause is purely in `token_fee`/`execute_intent`'s decision to evaluate the "≤1 no-fee" rule per intent-leg instead of per netted-token trade.

### Impact Explanation
Every trade of a NEP-245 (multi-token) or IMT asset with a fee configured (`protocol_fee = engine.state.fee() > Pips::ZERO`) can be executed with zero protocol fee by chunking it into unit legs, regardless of the true trade size `N`. This directly matches the Critical category "protocol fees bypassed or over-collected": the `fee_collector` is permanently underpaid by `Pips::fee_ceil(protocol_fee, N)` per trade, for every MT/NFT trade an attacker chooses to structure this way. This is repeatable across any account, any NEP-245/IMT `TokenId`, and any batch size — an attacker (or any pair of colluding/solver accounts) can apply it to every trade indefinitely, at the cost only of needing `N` separate signed messages instead of one (cheap, since signing is free/local and gas cost per extra intent is comparatively small versus the fee saved on large-volume MT trades).

### Likelihood Explanation
Preconditions are trivial and match the unprivileged attacker profile: an account holding a NEP-245/IMT balance inside the Verifier, and `protocol_fee > 0` (a normal deployment configuration, not admin-only in exploit terms). No privileged role, relayer key, or victim signature is required — the attacker only signs their own intents (plus needs a counterparty leg netting the token to zero across the batch, which can be another of the attacker's own accounts or a normal counterparty solver, without requiring that party's cooperation in the exploit itself since the counterparty leg's positive delta never triggers a fee anyway). This is a general design flaw in fee-per-leg computation, not an edge-case, so it applies to any legitimate high-volume MT/NFT trade — highly feasible and repeatable.

### Recommendation
Compute the “no fee for `|delta| <= 1`” exemption on the netted per-`(signer, token)` volume across the whole batch/execution context, not on each individual `TokenDiff` intent's own delta. E.g., accumulate all negative deltas per `(signer_id, token_id)` across all `TokenDiff` intents executed within the same `execute_signed_intents` call (or eliminate the per-leg exemption entirely and only waive the fee based on genuine single NFT semantics such as `Nep171`, since `Nep245`/`Imt` are fungible-like and splittable) before applying `token_fee`.

### Proof of Concept
`cargo test` (integration test under `tests/src/tests/defuse/intents/token_diff.rs`, following the existing `swap_many`/`solver_user_closure` test patterns):
1. Deploy Verifier with `fee = Pips::ONE_PERCENT` (or any `Pips > 0`), a NEP-245 mock MT contract, and two accounts `attacker` and `solver`.
2. Deposit `N` (e.g., `N = 10`) units of NEP-245 `token_T` to `attacker`'s Verifier balance, and enough of `token_X` to `solver`.
3. Have `attacker` sign `N` separate `TokenDiff` intents, each `diff = {token_T: -1, token_X: +k}` for appropriate `k` so the batch nets to zero on both tokens when matched against a single `solver`-signed `TokenDiff` with `diff = {token_T: +N, token_X: -N*k}`.
4. Call `execute_intents`/`execute_signed_intents` with this batch.
5. Assert: `engine.state` (or on-chain MT balance) of `fee_collector` for `token_T` == `0`.
6. Compute `Pips::fee_ceil(protocol_fee, N)` directly and assert it is `> 0`, demonstrating the binding `sum(fees_collected[T]) == Pips::fee_ceil(protocol_fee, N)` is violated (`0 != nonzero`).
7. Contrast with a single `attacker` intent `{token_T: -N, token_X: +...}` matched by a single `solver` intent, and assert the fee collected there **is** `Pips::fee_ceil(protocol_fee, N)` — proving the divergence is caused purely by leg-splitting.

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
