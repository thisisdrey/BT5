### Title
Protocol fee bypass on `Nep245`/`Imt` `TokenDiff` intents via unit-delta splitting - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::token_fee` waives the protocol fee whenever `|delta| <= 1` for `Nep245`/`Imt` token types, treating them as NFT-like. Because fee is computed and collected per intent on the *individual* signed delta rather than on the net amount moved, a signer can split one large `Nep245`/`Imt` swap into many unit (`±1`) `TokenDiff` intents in the same signed payload/batch, causing `token_fee` to return `Pips::ZERO` for every chunk and driving total `fees_collected` to `0`, whereas the same volume executed as a single `TokenDiff` would owe `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount) > 0`.

### Finding Description
The broken binding: `fees_collected` credited to `fee_collector` after executing intents == `Self::token_fee(token_id, total_amount, protocol_fee).fee_ceil(total_amount)` for the true net delta a signer moves on a `Nep245`/`Imt` token.

Code path: [1](#0-0) 

`token_fee` exemption: [2](#0-1) 

Root cause: the fee-exemption threshold (`amount > 1` to charge fee, `amount <= 1` to waive it) was designed for atomic NFTs (`Nep171`, always amount 1), but is also applied to `Nep245` and `Imt` tokens, which are semi-fungible and can carry arbitrarily large quantities per token id. The `execute_intent` loop computes and accumulates `fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` independently for each `(token_id, delta)` pair in `self.diff`, and a single signed `DefusePayload`/`MultiPayload` batch can contain many `TokenDiff` intents from the same signer. An attacker can therefore submit N `TokenDiff` intents each with `delta = -1` (and matching counter-intents supplying `+1` each, e.g. from a cooperating solver or their own second account) instead of one intent with `delta = -N`. Each unit intent hits the `amount <= 1` branch and contributes `fee = 0`, while a single `delta = -N` intent (`N > 1`) would contribute `fee = ceil(N * protocol_fee)`. No existing guard (`internal_apply_deltas` overflow checks, `TransferMatcher::finalize` net-zero invariant, nonce/signature checks) inspects the *number* of intents or aggregates deltas across intents before applying `token_fee`, so nothing prevents this decomposition.

Note: for `Nep141` (fungible) tokens, `token_fee` always returns the flat `fee` regardless of amount, and `fee_ceil` is a ceiling function, which is superadditive (`ceil(a)+ceil(b) ≥ ceil(a+b)`), so splitting `Nep141` deltas can only ever equal or *increase* total fees paid by the signer — never decrease them. The exploitable divergence is specific to the `Nep245`/`Imt` `amount <= 1` exemption.

### Impact Explanation
This lets any signer moving `Nep245` or `Imt` balances through `TokenDiff` intents pay zero protocol fee on volumes of any size, by chunking the swap into unit transfers matched against a counterparty (which can be their own second controlled account, cooperating solver, or paired against a `Transfer`/other `TokenDiff`). This is a direct "protocol fees bypassed" outcome — fees that should have been credited to `fee_collector` are permanently lost, for every `Nep245`/`Imt` token in the system, repeatable indefinitely and across any signer, matching the Critical impact category.

### Likelihood Explanation
Preconditions: attacker needs any `Nep245` (multi-token) or `Imt` balance in the Verifier (trivial to obtain via deposit or, for `Imt`, self-minting via `ImtMint`) and a counterparty (can be themselves, using two of their own accounts, or a self-controlled solver) to supply the offsetting `+1` legs so `TransferMatcher::finalize` nets to zero. Cost is purely gas for N small intents packed into one or more signed payloads within one `execute_intents` call; no privileged role, relayer key, or victim key is required. This is fully within reach of an unprivileged actor and repeatable at will.

### Recommendation
Remove the `amount <= 1` fee exemption for `Nep245` and `Imt` token types in `TokenDiff::token_fee` (keep it only for `Nep171`, which is inherently atomic), or compute fees on the net aggregated delta per `(signer, token_id)` across all `TokenDiff` intents in a batch rather than per individual intent, so unit-splitting cannot reduce the effective fee-bearing amount below the true moved quantity.

### Proof of Concept
```rust
// contracts/defuse/core/src/intents/token_diff.rs (new #[cfg(test)] case)
// 1. Build a Nep245/Imt TokenId `t`.
// 2. Compute expected_fee = TokenDiff::token_fee(&t, 100, protocol_fee).fee_ceil(100)
//    for a single intent with delta = -100 (assert expected_fee > 0 for protocol_fee > 0).
// 3. Simulate/execute a batch of 100 TokenDiff intents from the same signer,
//    each with delta = -1 on `t`, matched by 100 counter-intents with delta = +1
//    from a second account (or self, using a Transfer to net to zero).
// 4. Sum `fees_collected` events (TokenDiffEvent::fees_collected) across all 100 intents.
// 5. Assert: sum_of_split_fees == 0  while expected_fee (single-intent case) > 0.
//    This demonstrates fees_collected (split) != token_fee applied once to net delta.
```
A `near-workspaces`/sandbox variant would deposit/mint an `Imt` or `Nep245` balance for `user1`, have `user2` supply matching `+1` legs, sign one `MultiPayload` containing the 100 unit `TokenDiff` intents plus 100 counter `TokenDiff` intents, call `execute_intents`, and assert the `fee_collector`'s post-balance is unchanged (`0` fee) versus the single-intent baseline where it increases by `expected_fee`.

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L56-78)
```rust
        let protocol_fee = engine.state.fee();
        let mut fees_collected: Amounts = Amounts::default();

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
