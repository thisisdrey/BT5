### Title
Protocol fee bypass on `Nep245`/`Imt` `TokenDiff` intents via unit-sized (`|delta|<=1`) chunking - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` waives the protocol fee whenever `amount <= 1` for `TokenIdType::Nep245` and `TokenIdType::Imt`, a carve-out intended for true non-fungible items (which can only ever move in units of 1). Because `Imt` and `Nep245` token balances can hold arbitrary quantities (e.g. `ImtMint` minted amounts of `1000` in existing tests), an attacker can split any large transfer into a sequence of unit-sized (`delta == ±1`) `TokenDiff` intents to move an unbounded cumulative quantity while every single intent falls into the `amount == 1` no-fee branch.

### Finding Description
The broken binding: cumulative `fees_collected` for token `T` over `N` sequential unit `TokenDiff` executions moving a total volume `V = N` should equal `Pips::fee_ceil(V, protocol_fee)` — the amount a single `TokenDiff` with `delta = -V` would pay. Instead cumulative fee is `0`.

In `contracts/defuse/core/src/intents/token_diff.rs`:
```
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

and in `execute_intent`:
```
if *delta < 0 {
    let amount = delta.unsigned_abs();
    let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
    fees_collected.add(token_id.clone(), fee)...
}
``` [2](#0-1) 

The comment "do not take fees on NFTs and MTs with `|delta| <= 1`" was written for `Nep171` (true NFTs, always amount `1`), but the same exemption is reused verbatim for `Nep245` and `Imt`, both of which represent divisible/fungible-like quantities (`ImtMint`/`ImtBurn` operate on arbitrary `u128` amounts, e.g. `amount = 1000` in `tests/src/tests/defuse/intents/imt_mint.rs`) [3](#0-2) . Since the fee decision is made purely from the `|delta|` of a single `TokenDiff` intent (no cumulative or per-signer/per-token tracking exists anywhere in `execute_intent`), an attacker holding a large `Imt`/`Nep245` balance can move volume `V` fee-free by issuing `V` intents each with `delta == -1` (paired each time with a matching `+1` delta from a counterpart account they also control, to satisfy the batch's zero-sum invariant), instead of one intent with `delta == -V` which would pay `fee_ceil(V)`.

No existing guard blocks this: nonces only prevent *replay* of an identical signed intent, not the signing of many distinct unit-sized intents; there is no minimum-amount enforcement anywhere else in the `TokenDiff` execution path.

### Impact Explanation
Protocol fee revenue on `Imt`/`Nep245` `TokenDiff` swaps/transfers is bypassed entirely regardless of the cumulative value moved, as long as each individual signed intent's `|delta|` is `1`. This directly matches the Critical category "protocol fees bypassed or over-collected." The blast radius covers all `Imt` and `Nep245` token types under the fee-charging `TokenDiff` intent, and is repeatable indefinitely by any unprivileged signer for any counterpart account they control, with no cap on cumulative volume.

### Likelihood Explanation
Preconditions are minimal and fully within an unprivileged attacker's control: hold an `Imt`/`Nep245` balance (attacker can mint their own via `ImtMint`, which anyone can call, or use any `Nep245` MT they legitimately hold), have a second account (or self-signed pair) to receive the offsetting `+1` delta, and a nonzero protocol fee configured. The only cost is transaction/gas overhead for signing and submitting `N` intents instead of `1` — this is feasible for any attacker willing to spend proportionally more gas to save fee, and scales linearly, making it economically attractive whenever `fee_ceil(V) * fee_rate` exceeds the marginal gas cost of `V` unit-sized intents.

### Recommendation
Do not exempt `Nep245`/`Imt` from fees based on `amount <= 1` unless it can be proven the underlying token instance is truly non-fungible (single-supply, like `Nep171`). At minimum, restrict the exemption to `Nep171`, or track and charge fee based on cumulative per-token-id volume across a nonce/time window rather than per single intent `|delta|`.

### Proof of Concept
`cargo test` in `contracts/defuse/core` (or `defuse` integration tests) exercising `TokenDiff::execute_intent` directly against a mock `Engine`/`State`:
1. Configure `protocol_fee = Pips::ONE_PERCENT` (nonzero).
2. Mint (`ImtMint`) an `Imt` balance of `1000` to account `A`, and set up account `B` to receive matching `+1` deltas.
3. Execute 1000 separate `TokenDiff` intents: `A: {imt_token: -1}` paired each time with `B: {imt_token: +1}`, asserting after each call `fees_collected.is_empty()` (per `TokenDiffEvent`/state) — confirm cumulative fee collected on the fee collector's balance for `imt_token` is `0` after all 1000 calls.
4. In a separate scenario, execute a single `TokenDiff` pair: `A: {imt_token: -1000}`, `B: {imt_token: +1000}`, and assert `fees_collected` for `imt_token` equals `Pips::ONE_PERCENT.fee_ceil(1000)` (nonzero).
5. Assert the two cumulative fee totals differ (`0 != fee_ceil(1000)`), proving the binding is broken.

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

**File:** tests/src/tests/defuse/intents/imt_mint.rs (L40-53)
```rust
    let token = "sometoken.near".to_string();
    let memo = "Some memo";
    let amount = 1000;

    let intent = ImtMint {
        tokens: Amounts::new(std::iter::once((token.clone(), amount)).collect()),
        memo: Some(memo.to_string()),
        receiver_id: user.account_id().clone(),
        notification: None,
    };
    let mint_payload = user
        .sign_defuse_payload_default(&env.defuse, [intent.clone()])
        .await
        .unwrap();
```
