### Title
NEP-245 / IMT `TokenDiff` fee exemption for `|delta| <= 1` allows unlimited fee-free transfers via unit-sized diff splitting - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` unconditionally returns `Pips::ZERO` whenever a `Nep245` or `Imt` token's per-intent delta amount is `<= 1`, and the fee is computed independently for each `TokenDiff` intent rather than against any account- or batch-level cumulative transfer size. Because MT (NEP-245) balances are fungible-like (unlike true NFTs where balance is always 1), an attacker controlling two accounts can move an arbitrarily large NEP-245 balance from one to the other by signing N sequential single-unit `TokenDiff` intents (in one or many `MultiPayload` batches), each carrying delta `-1`/`+1`, and pay zero protocol fee in total, whereas moving the same amount in a single `TokenDiff` would incur `Pips::fee_ceil(amount)`.

### Finding Description
The broken binding: for a given token `T` and a set of `TokenDiff` intents transferring a net amount `A` of `T` between two attacker-controlled accounts, the protocol should collect `fee_collector_credit(T) == Pips::fee_ceil(A)` (the fee owed on the true economic amount moved), but the actual credited fee is `sum_i Pips::fee_ceil(|delta_i|)` computed per-intent via `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` in `TokenDiff::execute_intent` [1](#0-0) .

`TokenDiff::token_fee` explicitly zeroes the fee for `Nep245`/`Imt` (and always for `Nep171`) whenever the single-intent `amount <= 1`:
```
TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}
// do not take fees on NFTs and MTs with |delta| <= 1
TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO,
``` [2](#0-1) 

This carve-out makes sense for `Nep171` (true NFTs, where a token's balance is always exactly 1, so there is no way to "split" a transfer), but `Nep245` (multi-token) and `Imt` token balances are fungible quantities that can be arbitrarily subdivided. Since the fee is evaluated purely on the per-`TokenDiff` `amount = delta.unsigned_abs()` with no tracking of cumulative deltas across intents/batches/time for the same `(signer, token)` pair, an attacker can:

1. Deploy/control two Verifier accounts `A` and `B` (both under their control).
2. Deposit or otherwise hold `N` units of an NEP-245 token in `A`.
3. Sign `N` separate `MultiPayload`s (or intents within a batch), each containing a matching pair of `TokenDiff`s: `A: {T: -1}` and `B: {T: +1}` (net-zero per intent-set, satisfying `TransferMatcher::finalize`).
4. Call `execute_intents` (or batch multiple such intent-pairs in one `MultiPayload`) N times.
5. Each individual `TokenDiff` execution independently satisfies `amount <= 1`, so `token_fee` returns `Pips::ZERO` and no fee is ever added to `fees_collected` in `execute_intent`.

No existing guard intervenes: `MultiPayload::verify`, nonce/signature checks, and `TransferMatcher::finalize` only ensure each intent-set nets to zero and is properly authorized — none of them aggregate or rate-limit per-token transfer volume for fee purposes. The result: the entire `N`-unit balance moves from `A` to `B` with zero cumulative fee credited to `fee_collector`, versus `Pips::fee_ceil(N)` that a single `TokenDiff{T: -N}` would have incurred.

### Impact Explanation
This is a protocol-fee-bypass issue matching the Critical category "protocol fees bypassed or over-collected." Any user can avoid all `Nep245`/`Imt` swap/transfer fees by chunking a trade or self-transfer into unit-sized `TokenDiff`s across accounts they control. The blast radius covers every `Nep245`/`Imt` token type used for value that is not a genuine 1-unit NFT (i.e., any MT contract used to represent fungible-style balances), with lost fee revenue scaling linearly with transfer size and chunk count. The Verifier's own balance invariant is not broken (funds don't get created or destroyed, and this doesn't move third-party funds without authorization) — the damage is solely `fee_collector` under-collection, i.e., a fee bypass rather than balance-invariant violation.

### Likelihood Explanation
Preconditions are trivial: the attacker needs only two accounts they control (or even self-transfers on one account combined with a colluding counterparty) and any balance of an NEP-245/IMT token already in or depositable to the Verifier. No privileged role, relayer key, or victim signature is required — this is fully reachable by an unprivileged signer through ordinary `execute_intents` calls. The only cost is gas/transaction overhead for `N` separate calls (or intents within fewer batches, since a single `MultiPayload` can bundle many intents each with their own `TokenDiff` at 1-unit granularity), which is linear and does not require any exotic setup; this remains straightforwardly repeatable for arbitrary tokens and arbitrary amounts.

### Recommendation
Base the `Nep245`/`Imt` fee-exemption decision on whether the underlying token is truly non-fungible (e.g., a per-`token_id` supply cap enforced by the MT contract, or a separate flag/registry marking a `Nep245` sub-token as "NFT-like") rather than solely on the delta magnitude of an individual `TokenDiff`. Alternatively, remove the blanket `amount <= 1` exemption for `Nep245`/`Imt` and instead special-case only tokens whose contract-level maximum supply per `token_id` is 1, or track and aggregate transferred amounts per `(signer_id, token_id)` within some accounting window before applying the exemption.

### Proof of Concept
```rust
// cargo test in tests/src/tests/defuse/intents/token_diff.rs (or a new test module)
// Sandbox setup: create attacker accounts A and B, deploy an NEP-245 contract,
// deposit 1000 units of token T into A's Verifier balance, set fee = Pips::ONE_PERCENT.

// Baseline (single TokenDiff transferring all 1000 units at once):
// sign TokenDiff{A: {T: -1000}} / TokenDiff{B: {T: +<closure amount>}}
// assert fee_collector's mt_balance_of(T) == Pips::ONE_PERCENT.fee_ceil(1000) (nonzero)

// Attack (1000 sequential 1-unit TokenDiffs):
for _ in 0..1000 {
    let signed = try_join_all([
        a.sign_defuse_payload_default(&env.defuse, [TokenDiff {
            diff: TokenDeltas::default().with_apply_deltas([(t.clone(), -1)]).unwrap(),
            memo: None, referral: None,
        }]),
        b.sign_defuse_payload_default(&env.defuse, [TokenDiff {
            diff: TokenDeltas::default().with_apply_deltas([(t.clone(), 1)]).unwrap(),
            memo: None, referral: None,
        }]),
    ]).await.unwrap();
    env.defuse_execute_intents(env.defuse.contract_id(), signed).await.unwrap();
}

// assert fee_collector's mt_balance_of(T) == 0 (or unchanged from before loop)
// assert A's balance decreased by 1000, B's balance increased by 1000
// This demonstrates fee_collector credit == 0 despite net transfer of 1000 units,
// versus Pips::ONE_PERCENT.fee_ceil(1000) owed if done as a single TokenDiff.
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
