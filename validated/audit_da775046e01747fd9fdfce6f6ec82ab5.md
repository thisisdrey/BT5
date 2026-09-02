### Title
Fee bypass on NEP-245/IMT `TokenDiff` via unit-splitting (`amount == 1` legs) - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::token_fee` returns `Pips::ZERO` for any `Nep245`/`Imt` leg whose absolute delta is `1`, regardless of `protocol_fee`. Because a signer can pack an arbitrary number of `TokenDiff` intents (or unit-sized diffs) into a single signed `DefuseIntents` message, they can split one `-N` leg on a fungible NEP-245/IMT token into `N` separate `{token_id: -1}` legs, each independently evaluated by `token_fee`, driving total collected fees for that swap to `0`.

### Finding Description
Binding claimed and verified as broken: `sum(fee credited to fee_collector for token T across a batch) == Pips::fee_ceil(protocol_fee, sum(|delta| of T in batch))`. 

The root cause is in `TokenDiff::token_fee`:
```
contracts/defuse/core/src/intents/token_diff.rs:206-216
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
``` [1](#0-0) 

This function is called per-leg, per `TokenDiff::execute_intent` invocation, with the `amount` being the single leg's `|delta|`, not the aggregate delta across the whole batch/message:
```
contracts/defuse/core/src/intents/token_diff.rs:59-78
for (token_id, delta) in &self.diff {
    ...
    if *delta < 0 {
        let amount = delta.unsigned_abs();
        let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
        fees_collected.add(token_id.clone(), fee)...
    }
}
``` [2](#0-1) 

`DefuseIntents` allows an arbitrary vector of `Intent`s under a single signature/nonce: [3](#0-2) 

The engine executes each intent in order, invoking `TokenDiff::execute_intent` independently for each `TokenDiff` intent, with `fees_collected` scoped per-intent-execution (reset to `Amounts::default()` each call): [4](#0-3) [5](#0-4) 

**Attacker payload**: a single `MultiPayload` (one signature, one nonce) whose `DefuseIntents.intents` contains `N` separate `Intent::TokenDiff` entries, each with `diff = {token_id: -1}` for the same NEP-245/IMT `token_id`, instead of one `TokenDiff` with `diff = {token_id: -N}`. Each leg independently satisfies `amount == 1`, so `token_fee` returns `Pips::ZERO` for every leg, and `fee_ceil(0, 1) == 0`. Summed over the batch, `fees_collected` for that token is `0`, whereas a single `-N` leg would yield `Pips::fee_ceil(protocol_fee, N) > 0` whenever `protocol_fee > 0`.

**Why guards don't catch this**: `MultiPayload::verify`, `has_public_key`, `verify_intent_nonce`, and nonce commitment operate on the whole signed message/nonce, not on individual `TokenDiff` legs, so splitting into many intents inside one valid signed message passes all of them unchanged. There is no aggregation of `|delta|` per token across intents within a message or across a batch before computing the fee - the check operates purely on the single `amount` argument passed at each call site.

The comment "`do not take fees on NFTs and MTs with |delta| <= 1`" reflects a design assumption that NEP-245/IMT deltas of magnitude 1 represent non-fungible items (where taking a "sub-unit" fee is meaningless), but NEP-245 (multi-token) and the internal IMT token types can also represent fungible balances with large supplies, so this assumption does not hold generally and creates a fee-evasion primitive.

### Impact Explanation
The `fee_collector` account under-collects protocol fees on the entire NEP-245/IMT-denominated leg of any swap: fees due are `Pips::fee_ceil(protocol_fee, N)` but actually collected is `0`. This is repeatable by any unprivileged signer for any amount `N` and any NEP-245/IMT token they hold inside the Verifier, on every swap they perform, with no cap other than message size/gas limits. This matches the Critical category "protocol fees bypassed" - value (fee) that should flow to `fee_collector` never leaves the signer's balance, effectively letting the signer keep 100% of what should be fee revenue on these token types.

### Likelihood Explanation
Preconditions: the signer only needs to hold NEP-245/IMT token balance inside the Verifier (attacker's own funds) and `protocol_fee > 0` (deployment-config, not attacker controlled but typical for production). Cost is trivial - constructing `N` `TokenDiff` intents inside one signed `DefuseIntents` message (or across a `Vec<MultiPayload>` batch) requires no privileged role, no relayer key, and no victim key. This is fully feasible with only standard `execute_intents`/`simulate_intents` calls and is repeatable indefinitely across tokens, accounts, and batches.

### Recommendation
Aggregate `|delta|` per `TokenId` across the entire `DefuseIntents.intents` sequence (and ideally across the whole batch of `MultiPayload`s executed together) before applying the `amount > 1` fee-exemption threshold for `Nep245`/`Imt`, rather than evaluating the threshold per individual `TokenDiff` leg. Alternatively, remove or rework the `amount <= 1` fee exemption for `Nep245`/`Imt` token types so it only exempts genuinely non-fungible items (e.g., gated by a token-level flag rather than the instantaneous delta magnitude), since NEP-245 tokens can be fungible with large per-unit-priced supply.

### Proof of Concept
```rust
// tests/src/tests/defuse/intents/token_diff.rs (new test)
// 1. Setup: deploy NEP-245 multi-token contract `mt`, mint token "ft1" with amount N (e.g. 1000)
//    to `user`, deposit into Verifier (Defuse) so user's MT balance in Defuse == N.
// 2. Set protocol_fee = Pips::ONE_PERCENT (or any > 0) via fee manager role in env setup.
// 3. Baseline (single-leg) expectation:
//    let expected_fee = Pips::fee_ceil(protocol_fee, N); // > 0
// 4. Exploit payload: sign ONE DefuseIntents message containing N intents:
//    intents: (0..N).map(|_| TokenDiff { diff: {mt_token_id: -1}, memo: None, referral: None })
//    (paired with equal +N credit elsewhere in the same message or a counterparty payload to keep the batch net-zero, e.g. swap into a NEP-141 token the attacker also controls).
// 5. env.defuse_execute_intents(..., [payload]).await.unwrap();
// 6. assert_eq!(
//        mt_balance_of(fee_collector, mt_token_id).await.unwrap().0,
//        0 // actual: fee bypassed
//    );
//    assert_ne!(0, expected_fee); // demonstrates fee_ceil(protocol_fee, N) > 0 was owed
// This shows sum(fee credited) == 0 while Pips::fee_ceil(protocol_fee, N) > 0, violating the binding.
```

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L56-57)
```rust
        let protocol_fee = engine.state.fee();
        let mut fees_collected: Amounts = Amounts::default();
```

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

**File:** contracts/defuse/core/src/engine/mod.rs (L76-80)
```rust
        self.verify_intent_nonce(nonce, deadline)?;
        self.state.commit_nonce(signer_id.clone(), nonce)?;

        intents.execute_intent(&signer_id, self, hash)?;
        self.inspector.on_intent_executed(&signer_id, hash, nonce);
```
