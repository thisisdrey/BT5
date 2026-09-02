### Title
Fee bypass via delta-splitting on `Nep245`/`Nep171`/`Imt` `TokenDiff` withdrawals - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::execute_intent` computes the protocol fee independently for each intent, using only that intent's own `delta.unsigned_abs()` as `amount`. `TokenDiff::token_fee` waives the fee entirely for `Nep171`/`Nep245`/`Imt` token types whenever `amount <= 1`. A signer can therefore split a single large negative delta on a `Nep245` (or `Imt`) token into many separate `TokenDiff` intents each with `delta == -1`, causing the per-intent fee check to see `amount == 1` every time and return `Pips::ZERO`, whereas a single unsplit intent with the same aggregate magnitude would be charged `fee_ceil(M)` at the configured `protocol_fee`.

### Finding Description
Broken binding: `total_fee_collected(signer, T, batch) == Self::token_fee(T, M, protocol_fee).fee_ceil(M)` where `M` is the signer's aggregate negative delta magnitude for token `T` across the settlement batch. This should hold regardless of how the signer partitions that magnitude across intents, but it does not.

Code path:
- `TokenDiff::execute_intent` iterates the intent's own `diff: BTreeMap<TokenId, i128>` and, for each negative entry, computes `let amount = delta.unsigned_abs(); let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);` — [1](#0-0) 
- `TokenDiff::token_fee` explicitly waives fees for `Nep171 | Nep245 | Imt` when `amount <= 1`, only charging the configured `fee` when `amount > 1`: [2](#0-1) 
- The fee computation is entirely local to a single `execute_intent` call; there is no cross-intent, cross-payload, or per-signer aggregation of the negative delta magnitude for a given `TokenId` before deciding whether the `amount > 1` fee tier applies. `internal_apply_deltas` (which actually mutates balances) and the batch-wide `TransferMatcher`/`finalize()` invariant (which only enforces that per-token deposits equal withdrawals across the whole batch, via `contracts/defuse/core/src/engine/state/deltas.rs`) are both blind to fee accounting — they do not recompute or re-derive fees, so nothing corrects for the split.

Exploit flow: An attacker holding `10` units of `Nep245` token `T` in the Verifier wants to trade it away (e.g., for `ft2`). Instead of signing one `TokenDiff{T: -10, ft2: +X}` (which would incur `fee_ceil(10)` since `amount=10>1`), the attacker signs ten separate `TokenDiff` intents, each `{T: -1, ft2: +X/10}` (batched in one or more `MultiPayload`s executed via `execute_intents`), matched against a counterparty (a solver, or the attacker's own second account) supplying the offsetting positive `T` and negative `ft2` deltas needed to satisfy `TransferMatcher::finalize`'s net-zero-per-token invariant. Each of the ten intents independently evaluates `token_fee(T, 1, fee) == Pips::ZERO`, so total fee collected is `0` instead of `fee_ceil(10)`.

Existing guards do not prevent this: `MultiPayload::verify`, nonce/salt checks, and `TransferMatcher::finalize` only validate signatures, replay-protection, and that the batch's net token deltas sum to zero — none of them touch fee computation or require a signer's per-token negative deltas to be consolidated before fee tiering is applied.

### Impact Explanation
Protocol fees for `Nep245`/`Imt` (and trivially `Nep171`, though NFTs are inherently quantity-1 so this mainly matters for semi-fungible `Nep245`/`Imt` amounts) trades are fully bypassable by any signer who controls how their own `TokenDiff` intents are structured. This directly matches the Critical category "protocol fees bypassed or over-collected." The attacker loses nothing and gains the full fee amount that would otherwise be credited to `fee_collector`; this is repeatable for every trade, every signer, and every `Nep245`/`Imt` `TokenId`, with no bound on the number of times it can be exploited.

### Likelihood Explanation
The only precondition is that the attacker controls the structuring of their own signed `TokenDiff` intents (trivial, since they are the signer) and can find/construct a counterparty (a solver, or a second self-controlled account) to supply the matching offsetting deltas so the batch nets to zero, which is the normal intended usage pattern of `TokenDiff` (RFQ/solver matching) — no privileged role, relayer key, or victim signature is required. Cost is simply signing more, smaller intents instead of one; near-transaction/gas cost scales with the number of splits but that is a normal usable-batch-size range for meaningful fee amounts. Fully repeatable, unauthenticated, and requires no special contract state.

### Recommendation
Compute the fee tier (`amount > 1` threshold) based on the signer's aggregate negative delta magnitude per `TokenId` across the whole intents batch (or at minimum across a single signer's full `DefuseIntents` message), not per individual `TokenDiff::diff` map entry. Alternatively, remove/replace the `amount <= 1` fee-exemption heuristic for `Nep245`/`Imt` token types with a mechanism that does not depend on an attacker-controlled input, such as per-`TokenId` type flags (fungible vs. non-fungible within the same NEP-245 contract) rather than tx-supplied delta magnitude.

### Proof of Concept
`cargo test` plan (unit test in `contracts/defuse/core/src/intents/token_diff.rs` or an integration test alongside `tests/src/tests/defuse/intents/token_diff.rs`):
1. Set up a sandbox `Env` with a nonzero `fee` (e.g., `Pips::ONE_PERCENT`).
2. Create attacker account with `10` units of a `Nep245` `TokenId` `T` deposited into the Verifier, and a counterparty/solver account willing to trade `ft2` for `T`.
3. Path A ("consolidated"): sign one `TokenDiff{T: -10, ft2: +closure}` from the attacker plus the matching solver `TokenDiff`, execute via `execute_intents`, and record `fee_collector`'s balance of `T`. Assert it equals `Pips::ONE_PERCENT.fee_ceil(10)` (nonzero).
4. Path B ("split"): reset state; sign ten separate `TokenDiff{T: -1, ft2: +closure/10}` intents from the attacker (each in its own signed payload or intent), plus matching solver-side `TokenDiff` entries, execute via `execute_intents`, and record `fee_collector`'s balance of `T`.
5. Assert `fee_collector_balance_A != fee_collector_balance_B` and specifically `fee_collector_balance_B == 0`, demonstrating that identical net economic transfer (`10` units of `T` traded for the same `ft2` amount) yields zero fee when split versus the correct nonzero `fee_ceil(10)` when consolidated — proving the bypass.

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
