Based on the investigation, the closest legitimate structural analog to the C4 finding (fees not knowable/enforced consistently at settlement) is not the DAO-controlled `set_fee`/`set_fee_collector` path (excluded per rules, since it requires a `Role::DAO`/`Role::FeesManager` holder), but a genuine **unprivileged fee-bypass** in the `TokenDiff` intent's fee-exemption logic.

### Title
Protocol Fee Bypass on Divisible `Nep245`/`Imt` Tokens via Unit-Sized `TokenDiff` Splitting - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee()` unconditionally waives the protocol fee whenever the per-token delta magnitude is `<= 1`, regardless of the token's actual divisibility. While this exemption is documented as intended for NFTs (`Nep171`), it is also applied to `Nep245` (multi-token) and `Imt` (intent-minted token) token types, which can represent arbitrarily divisible, fungible-like value. Any unprivileged user can therefore split a large trade into many unit (`|delta| == 1`) `TokenDiff` intents, each independently evaluated as fee-exempt, and fully bypass the protocol fee that would otherwise apply to an equivalent single larger-delta intent.

### Finding Description
In `TokenDiff::execute_intent`, for every negative delta (token being paid in) the fee is computed as: [1](#0-0) 

via `Self::token_fee(token_id, amount, protocol_fee)`, which is defined as: [2](#0-1) 

The comment "do not take fees on NFTs and MTs with `|delta| <= 1`" conflates two different semantics of the `Nep245`/`Imt` token types: as an NFT-like unique token (supply exactly 1, where charging a fee makes no sense) and as a divisible, fungible-like multi-token (where a delta of exactly `1` raw unit is just the smallest denomination of a much larger balance). The fee function only inspects the *magnitude of a single intent's delta*, not the token's actual supply/divisibility, and fees are computed **per `TokenDiff` intent**, not aggregated across a batch of intents submitted together.

Since `execute_intents` accepts a `Vec<MultiPayload>` and each signed payload can carry its own `TokenDiff` intent, a solver/user can sign `N` separate `TokenDiff` intents, each moving `delta = ±1` of the same `Nep245`/`Imt` token (with unique nonces), and submit them all in one `execute_intents` call: [3](#0-2) 

Each of the `N` intents independently computes `token_fee(token_id, 1, protocol_fee) == Pips::ZERO`, so the aggregate transfer of `N` raw units incurs **zero** protocol fee, whereas an equivalent single intent with `delta = -N` would have incurred `protocol_fee.fee_ceil(N)`.

The equality that should hold — "fees owed" (based on the true economic size of the trade) versus "fees collected" (summed across however the trade is partitioned into intents) — is broken: fees owed at `amount = N` are effectively reduced to fees owed at `amount = 1` repeated `N` times, i.e., zero, purely by restructuring the same net economic transfer into unit-sized intents.

### Impact Explanation
This is a **Critical** impact per the rubric: "fees bypassed or over-collected." Any unprivileged user trading fungible-like `Nep245`/`Imt` tokens can avoid the protocol fee entirely on arbitrarily large trades by decomposing them into many `delta = 1` `TokenDiff` intents bundled into a single `execute_intents` transaction (or across several), directly reducing protocol revenue with no cooperation from any privileged role.

### Likelihood Explanation
The attack requires no special permissions, only that the traded asset is represented as a `Nep245`/`Imt` `TokenId` (multi-token standard, which is actively used for wrapping/derivative assets in this system) and that the fee value is non-zero. Constructing and signing `N` unit intents and bundling them in one call is mechanically straightforward for any user or solver capable of signing multiple `MultiPayload`s, making likelihood high wherever such tokens carry meaningful per-unit value.

### Recommendation
Compute and enforce protocol fees based on the true economic size of a transfer rather than the size of an individual `TokenDiff` delta:
- Restrict the `|delta| <= 1` fee exemption to token types that are provably non-divisible (e.g., only `Nep171`, or `Nep245`/`Imt` token ids whose registered total supply/decimals indicate NFT-like semantics), not blanket-applied to any `Nep245`/`Imt` id.
- Alternatively/additionally, aggregate fee calculation across all `TokenDiff` intents for the same signer/token within a single `execute_intents` call (or even across a time window) before applying the exemption threshold, so that splitting into unit deltas cannot zero out the aggregate fee.

### Proof of Concept
1. Assume `protocol_fee > 0` and a `Nep245` token `T` (e.g., a wrapped fungible asset) with meaningful value per raw unit.
2. Attacker Alice wants to swap `1,000,000` units of `T` for another asset, which would normally incur `fee = protocol_fee.fee_ceil(1_000_000)` per `Self::token_fee`/`fee_ceil` logic: [4](#0-3) 
3. Instead, Alice signs `1,000,000` separate `TokenDiff` intents, each with `diff = {T: -1, U: +price}` (matched by a counterparty solver's own unit `TokenDiff`), each with a distinct nonce.
4. All intents are submitted together via `execute_intents(signed: Vec<MultiPayload>)`: [5](#0-4) 
5. For every one of the `1,000,000` intents, `token_fee(T, 1, protocol_fee)` returns `Pips::ZERO` per the match arm on `Nep245`/`Imt` with `amount <= 1`: [2](#0-1) 
6. Net result: the full `1,000,000`-unit trade completes with `fees_collected == 0`, versus the `fee_ceil(1_000_000)` that would have been collected had it been submitted as one intent — a complete fee bypass achievable by any unprivileged user.

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

**File:** contracts/defuse/src/contract/intents/mod.rs (L24-42)
```rust
#[near]
impl Intents for Contract {
    #[pause(name = "intents")]
    fn execute_intents(&mut self, signed: Vec<MultiPayload>) {
        if let Some(event) = Engine::new(self, ExecuteInspector::default())
            .execute_signed_intents(signed)
            .unwrap_or_else(|e| e.panic())
            .as_mt_event()
        {
            // NOTE: Not all `mt_transfer` events are refundable, but it's safe to check them
            // all at once since non-refundable transfers only increase the potential refund
            // log size without affecting correctness. This can actually prevent resolve transfer
            // from failing due to too long event log !!!
            event
                .check_refund()
                .unwrap_or_else(|err| err.panic())
                .emit();
        }
    }
```
