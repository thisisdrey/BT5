### Title
Protocol fees bypassed on Nep245/IMT token volume by chunking swaps into amount=1 `TokenDiff` legs - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` exempts any negative delta of `amount <= 1` on a `Nep245` (multi-token) or `Imt` `TokenId` from protocol fees. Because the fee is computed independently per `TokenDiff` intent (and per token entry) rather than on the aggregate volume moved in a `MultiPayload` batch, an attacker can move an arbitrarily large Nep245/IMT balance fee-free by splitting it into many `-1`/`+1` legs that net out via `TransferMatcher`.

### Finding Description
The broken binding: for a signer moving total `N` units of a Nep245/IMT `TokenId` within one `execute_intents`/`simulate_intents` batch, the fee collector should receive `protocol_fee.fee_ceil(N)`, but it can instead be made to receive `0`.

Root cause is in `TokenDiff::token_fee`:
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
```
This `amount` is the per-`TokenDiff`-entry delta magnitude computed in `execute_intent` at [1](#0-0) , evaluated separately for each `(token_id, delta)` pair inside each signed `TokenDiff` intent, with no accumulation across multiple intents or across the whole `MultiPayload`.

`TransferMatcher::finalize` (in `contracts/defuse/core/src/engine/state/deltas.rs`) only enforces that the *sum* of all deltas for a token across the batch nets to zero; it performs no fee re-derivation based on aggregate volume — fees are already collected per-intent before matching happens. Consequently, an attacker who controls two accounts (or colludes with a counterparty) can express a transfer/swap of `N` MT units as `N` separate signed `TokenDiff` intents, each with `delta = -1` (sender) matched by `delta = +1` (receiver) on the same `TokenId`. Every one of these `N` calls to `token_fee` sees `amount == 1`, falls into the exempt arm, and returns `Pips::ZERO`, so `fee_ceil(1) == 0` for each leg regardless of `fee`. The batched sum moved is `N`, but total fees collected is `0`, instead of `fee_ceil(N)`.

Existing guards do not prevent this: `MultiPayload::verify`/nonce checks only ensure each signed payload is authentic and not replayed — they do not restrict how a signer partitions their intended transfer amount into intents; `TransferMatcher::finalize` checks conservation of the token, not fee correctness; there is no per-batch or per-account rate/size floor tying `token_fee`'s exemption to the cumulative amount moved for that `TokenId` in the transaction or session.

### Impact Explanation
Any unprivileged Verifier user holding a Nep245 (multi-token) or IMT balance can withdraw/swap/transfer their entire balance for that token while paying zero protocol fee, regardless of the token's true economic value per unit, by chunking the movement into unit-amount `TokenDiff` legs matched within the same `MultiPayload` (or across several transactions). This directly under-collects protocol fees — the `fee_collector` credit for that token diverges from `protocol_fee.fee_ceil(total negative delta)` — matching the "protocol fees bypassed" Critical category. The blast radius is limited to Nep245/IMT-denominated flows (Nep141 fungible tokens are unaffected since the exemption guard does not apply to them), but is repeatable indefinitely across accounts, tokens, and batches by any signer.

### Likelihood Explanation
Preconditions are modest: the attacker needs a Nep245/IMT balance in the Verifier and the ability to sign multiple `TokenDiff` intents (their own, or with a colluding/self-controlled counterparty account to supply the matching `+1` legs). No special role, relayer key, or victim key is required — this is fully reachable via `execute_intents`/`simulate_intents` with attacker-authored payloads. The only friction is transaction gas/size limits per batch, which just means the attacker splits the exploit across multiple ordinary transactions proportional to the amount, not a fundamental barrier.

### Recommendation
Base the Nep245/IMT fee exemption on genuine non-fungibility (e.g., only exempt token ids whose underlying supply/semantics are inherently `amount == 1`, such as true NFT-like MT sub-tokens), or track and aggregate per-`TokenId` negative-delta volume per signer within a batch (and ideally across a short window) before applying the `amount > 1` threshold, so fee liability is computed on total volume moved rather than on the size of the smallest chunk chosen by the attacker.

### Proof of Concept
`cargo test` (or `near-workspaces` sandbox) plan:
1. Deploy a NEP-245 (multi-token) contract and deposit a large balance (e.g., 10,000 units) of a single MT sub-token to attacker account A in the Verifier; create a second attacker-controlled account B.
2. Set a nonzero `protocol_fee` (e.g., `Pips::ONE_PERCENT`).
3. Construct 10,000 `TokenDiff` intents: 10,000 signed by A with `diff = {mt_token_id: -1}`, and 10,000 signed by B with `diff = {mt_token_id: +1}` (batched into one or more `MultiPayload`s / `execute_intents` calls).
4. Execute via `execute_intents`; call `TransferMatcher::finalize` succeeds (nets to zero).
5. Assert `fee_collector` balance for `mt_token_id` == `0`.
6. Compare against a control case: A signs a single `TokenDiff` with `delta = -10000` matched by B's `+10000`, and assert `fee_collector` balance == `protocol_fee.fee_ceil(10000)` (nonzero).
7. The divergence between step 5 (`0`) and step 6 (`fee_ceil(10000)`) for economically identical total volume demonstrates the fee bypass.

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
