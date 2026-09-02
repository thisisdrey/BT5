### Title
Fee bypass for NEP-245/IMT tokens by splitting a large `TokenDiff` into unit-sized diffs - (`contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` returns `Pips::ZERO` for `TokenIdType::Nep245`/`TokenIdType::Imt` whenever the per-leg `amount <= 1`, and this check is applied independently per `TokenDiff` intent inside `TokenDiff::execute_intent`. A signer can convert one `TokenDiff` intent with `delta == -N` (which would owe `fee.fee_ceil(N)`) into `N` separate `TokenDiff` intents each with `delta == -1` on the same `token_id`, each of which owes `Pips::ZERO`, letting the whole `-N` transfer settle fee-free. The claimed treasury-logger attribution (`contracts/treasury-logger/src/state.rs`) is unrelated — that file only stores a `u128` nonce and has no fee logic.

### Finding Description
Binding claimed to hold: `fee_collector.token_balances[T] after N single-unit TokenDiff calls == fee_collector.token_balances[T] after one -N TokenDiff call`, i.e. `Σ_{i=1..N} fee_ceil(token_fee(T, 1, fee), 1) == fee_ceil(token_fee(T, N, fee), N)`.

Tracing the code:
- `TokenDiff::token_fee` explicitly special-cases MT/IMT legs: `TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}` else `return Pips::ZERO` for `Nep171 | Nep245 | Imt`. [1](#0-0) 
- `TokenDiff::execute_intent` computes the fee per leg independently, once per intent invocation: `let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);` where `amount = delta.unsigned_abs()` is the delta of *that single intent*, not any running total across intents. [2](#0-1) 
- The fee, if any, is credited to `fee_collector` via `internal_add_balance`. [3](#0-2) 
- `Pips::fee_ceil` is a plain ceiling of `amount * pips / MAX`, so for `N > 1` and `fee > 0`, `fee.fee_ceil(N) > 0` in general (e.g. `fee = 1%`, `N = 100` ⇒ `fee_ceil = 1`), while for `N` single-unit legs each `token_fee` returns `Pips::ZERO`, giving `fee_ceil = 0` every time. [4](#0-3) 

Root cause: the "no fee on NFT/MT amount ≤ 1" exemption is evaluated strictly per-intent-leg with no state tracking of cumulative amount transferred by the signer for that `token_id` across intents in the same batch or across separate `execute_intents` calls. There is nothing in `MultiPayload::verify`, nonce/salt checks, or `#[pause]`/ACL guards that constrains a signer from submitting many small, independently-nonced `TokenDiff` intents instead of one large one — those mechanisms police signature/replay validity, not fee-base granularity.

Attacker payload: a signer holding ≥N MT/IMT units of `token_id` in the Verifier signs (either within one `MultiPayload` batch, or as N separate transactions) `N` `TokenDiff` intents, each `diff = {token_id: -1}` (paired with any equal-and-opposite legs elsewhere to keep the batch balance-neutral, e.g. swapping into another token they also control, or simply withdrawing/transferring the token out via other legs of the same intents). Each call takes the `amount == 1` branch and contributes `0` to `fees_collected`, whereas a single `TokenDiff{diff: {token_id: -N}}` would have paid `fee.fee_ceil(N)`.

### Impact Explanation
`fee_collector` is under-credited by `fee.fee_ceil(N)` in NEAR-native token units of `token_id` for every large NEP-245/IMT transfer the signer chooses to fragment into unit legs, compared to doing it as a single intent. This is a protocol-fee bypass (listed Critical impact category: "protocol fees bypassed or over-collected"), directly reducing protocol revenue without any invalid signature or double-settlement; it is repeatable by any account, for any NEP-245/IMT `token_id`, and for arbitrarily large `N` at zero marginal fee cost (only NEAR gas cost per extra intent/transaction). It does not move any user's funds without authorization and does not desync the Verifier's custody vs. liabilities for the underlying asset itself — only the `fee_collector`'s expected fee revenue is diminished.

### Likelihood Explanation
Preconditions are trivial and fully within an unprivileged signer's control: hold ≥N units of a NEP-245/IMT `token_id` in the Verifier, and a nonzero protocol `fee`. No role, relayer key, or DAO action is required — the signer just batches or repeats ordinary `TokenDiff` intents with their own nonces. Cost scales only with the number of NEAR function calls/gas for N legs instead of one; for meaningful fee amounts this is economically attractive for any market maker/solver routing MT flows through the Verifier repeatedly.

### Recommendation
Compute the NFT/MT fee exemption based on intended semantics (e.g., only truly NFT-like non-fungible legs, or track/aggregate `amount` per `(signer, token_id)` across all `TokenDiff` legs within the same `execute_intents` call, or remove the `amount <= 1` exemption for `Nep245`/`Imt` amounts entirely and always apply `fee.fee_ceil(amount)`), so `Σ token_fee` over any decomposition of a diff into legs equals `token_fee` applied to the aggregate delta.

### Proof of Concept
```
#[tokio::test]
async fn fee_bypass_via_unit_split(...) {
    // env with fee = Pips::ONE_PERCENT, fee_collector = collector
    // mint/deposit N=100 units of an MT token_id to `user`

    // Case A: single TokenDiff with delta = -100 (paired with an equal +delta elsewhere to net-zero the batch)
    // -> fee_collector balance_A = fee.fee_ceil(100)  // > 0

    // Case B: 100 separate TokenDiff intents, each delta = -1 on same token_id
    // (paired similarly to net-zero)
    // -> fee_collector balance_B = 0

    assert_ne!(balance_A, balance_B); // demonstrates broken binding
    assert_eq!(balance_B, 0);
    assert_eq!(balance_A, Pips::ONE_PERCENT.fee_ceil(100));
}
```
Assertions target `fee_collector`'s `token_balances[token_id]` (via `mt_batch_balance_of` on the collector account) after executing Case A vs. Case B with identical starting balances and the same `fee`.

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

**File:** contracts/defuse/core/src/intents/token_diff.rs (L96-101)
```rust
        // deposit fees to collector
        if !fees_collected.is_empty() {
            engine
                .state
                .internal_add_balance(engine.state.fee_collector().into_owned(), fees_collected)?;
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

**File:** crates/primitives/fees/src/lib.rs (L116-121)
```rust
    #[inline]
    pub fn fee_ceil(self, amount: u128) -> u128 {
        amount
            .checked_mul_div_ceil(self.as_pips().into(), Self::MAX.as_pips().into())
            .unwrap_or_else(|| unreachable!())
    }
```
