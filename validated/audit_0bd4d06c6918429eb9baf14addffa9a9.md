### Title
Fee bypass on Nep245/Imt `TokenDiff` transfers via unit-delta intent splitting — (`contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` waives protocol fees whenever the *per-intent* absolute delta on a `Nep245`/`Imt` token is `<= 1`, but fee computation happens independently for each `TokenDiff` intent in a `DefuseIntents` batch rather than being aggregated per signer/token across the whole signed payload. An unprivileged signer can therefore split one intended transfer of `A` units of a `Nep245`/`Imt` token into `A` separate `TokenDiff` intents, each with `delta == -1`, inside a single signed `MultiPayload`, and pay zero protocol fee instead of `Pips::fee_ceil(fee, A)`.

### Finding Description
The broken binding: for a single signer moving `A` units of a fee-bearing `Nep245`/`Imt` `TokenId`, the protocol expects
`sum(fees_collected for that token) == Pips::fee_ceil(protocol_fee, A)`.

The actual code computes the fee independently, per `TokenDiff` intent, from that intent's own `|delta|`: [1](#0-0) 

and the fee-rate selector explicitly zeroes the fee for `Nep245`/`Imt` when the *local* amount is `<= 1`: [2](#0-1) 

`ExecutableIntent::execute_intent` is invoked once per `Intent::TokenDiff` entry in `DefuseIntents.intents` (`contracts/defuse/core/src/intents/mod.rs`), and each invocation starts a fresh local `fees_collected: Amounts` — there is no per-signer/per-token accumulator across intents in the same batch. So if the signer's `-A` diff is expressed as `A` separate intents each carrying `delta == -1` on the same `TokenId`, every single intent hits the `amount > 1` guard as false (since `amount == 1`), returns `Pips::ZERO` from `token_fee`, and contributes `0` to `fees_collected`. Summed over the whole batch, `sum(fees_collected) == 0`, while a single unsplit `TokenDiff{ delta: -A }` would compute `Self::token_fee(token_id, A, fee).fee_ceil(A) > 0` whenever `fee > Pips::ZERO` and `A > 1`.

Exploit flow: attacker (using either two of their own accounts, or an accomplice) constructs a `DefuseIntents` with `A` `TokenDiff` intents for the sender, `{token_id: -1}` each, matched by corresponding `+1` deposits credited elsewhere in the same batch (via `TransferMatcher::finalize`, `contracts/defuse/core/src/engine/state/deltas.rs`), so the batch's net deltas still balance to zero and `execute_intents`/`simulate_intents` accept it. None of the existing guards intercept this: `MultiPayload::verify`/nonce checks only bind the payload's authenticity and replay-protection, not fee accounting; `TransferMatcher::finalize` only checks that deltas net to zero across accounts, it has no notion of fees; `Pips::fee_ceil` itself is correct arithmetic but is simply never invoked with the true aggregate amount because `token_fee` is evaluated with `amount = 1` on every one of the `A` intents.

### Impact Explanation
The protocol's fee collector (`engine.state.fee_collector()`) is under-credited for every Nep245/Imt-denominated trade or transfer that a user chooses to slice into unit-delta intents — this is a direct, repeatable "protocol fees bypassed" condition, matching the explicitly listed Critical impact category. It is repeatable across any account, any Nep245 (multi-token) or Imt token, and any batch size, and it costs the attacker nothing beyond slightly larger payload size / gas for the additional intents.

### Likelihood Explanation
Preconditions are trivial and fully within an unprivileged attacker's normal capabilities: a deposited Nep245/Imt balance, `ContractState` fee `> Pips::ZERO` (default configuration for a fee-charging deployment), and the ability to sign an arbitrary `MultiPayload`/`DefuseIntents` containing many `TokenDiff` intents (no role, relayer key, or DAO permission required). The only "cost" is transaction size/gas for `A` intents instead of 1, which is explicitly out of scope to worry about as a DoS concern, but does not prevent the fee-evasion demonstration for realistic values of `A` (e.g., tens to low hundreds).

### Recommendation
Aggregate the absolute delta per `(signer, token_id)` across all `TokenDiff` intents within the same execution/batch (or across the whole `DefuseIntents`) before applying the `Nep245`/`Imt` `amount <= 1` fee-exemption, so that `token_fee` sees the true consolidated amount, e.g. accumulate negative deltas per token per signer at the `Engine`/`Deltas` level and compute `fee_ceil` once on the total, refunding/crediting per-intent proportionally, rather than evaluating the exemption intent-by-intent.

### Proof of Concept
`cargo test` plan (near-workspaces sandbox, `tests/src/tests/defuse/intents/token_diff.rs` style):
1. Deploy Defuse with `fee = Pips::ONE_PERCENT` (or any `> 0`), deploy an MT contract, mint/deposit `A = 100` units of a `Nep245` `TokenId` to `signer` and set up a `counterparty` able to receive/return them.
2. Case A (unsplit): sign one `MultiPayload` with a single `TokenDiff{ diff: {token: -100, other_token: +X} }` (matched by counterparty's opposite `TokenDiff`). Execute via `execute_intents`. Assert `mt_balance_of(fee_collector, token) == Pips::ONE_PERCENT.fee_ceil(100)` (`> 0`).
3. Case B (split): sign one `MultiPayload` containing 100 `TokenDiff` intents from `signer`, each `{token: -1, other_token: +(X/100)}` matched by 100 corresponding counterparty intents. Execute via `execute_intents`. Assert `mt_balance_of(fee_collector, token) == 0`.
4. Assert both cases produce identical net balance change for `signer` on `token` (`-100`) and on `other_token` (`+X`), proving `fee_collector` diverges (`0` vs `Pips::fee_ceil(fee, 100) > 0`) despite identical signer economics — breaking the FEES equality.

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
