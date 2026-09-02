### Title
Fee bypass via attacker-controlled NEP-245 token wrapping with `amount == 1` legs - (`contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` returns `Pips::ZERO` for any `TokenIdType::Nep245` (or `Imt`) leg whose `amount <= 1`, regardless of the real economic value that unit represents. Because `Nep245TokenId::contract_id` and `mt_token_id` are fully attacker-controlled strings, an attacker can deploy their own MT contract that represents arbitrary (even large) notional value as a single opaque unit (`amount == 1`) per token_id, then route that value through `TokenDiff` intents completely fee-free, while the same notional value moved as a `Nep141` (or as an MT amount `> 1`) would be taxed at `protocol_fee`.

### Finding Description
The broken binding: for equal notional value `V` moved through the Verifier via a negative `TokenDiff` delta, the fee should equal `Pips::fee_ceil(protocol_fee, V)` regardless of token wrapper, i.e. `fee(Nep141, V) == fee(Nep245-wrapped(V), V)`. In the actual code this equality is violated: [1](#0-0) 

`token_fee` classifies purely by `TokenIdType` and raw `amount`, treating any NEP-245/IMT leg with `amount <= 1` as NFT-like and fee-exempt (comment: "do not take fees on NFTs and MTs with |delta| <= 1"). This heuristic assumes `amount == 1` implies an indivisible, low/no-value item. That assumption is not enforced anywhere: `Nep245TokenId` is `{contract_id: AccountId, mt_token_id: String}`, both fully chosen by whoever deploys the MT contract, and classification to `TokenIdType::Nep245` happens unconditionally for any such contract: [2](#0-1) 

An attacker deploys their own NEP-245 contract that wraps real value (e.g., NEP-141 stablecoins deposited into that MT contract) and always reports balances as a fresh `mt_token_id` with `amount == 1`, no matter how much value is wrapped inside a given position. Depositing this wrapped position into the Defuse Verifier via `mt_transfer_call` is not a fee event (fees are only assessed inside `TokenDiff::execute_intent`). The attacker (or a counterparty) then submits a `TokenDiff` intent giving away this `amount == 1` NEP-245 leg in exchange for real value. `execute_intent` hits the branch: [3](#0-2) 

with `Self::token_fee(token_id, 1, protocol_fee)` returning `Pips::ZERO`, so `fee == 0` on that leg no matter how large the wrapped notional is, whereas the same value moved as raw NEP-141 units would be taxed via `Pips::fee_ceil`. Withdrawal from the Verifier and unwrapping back through the attacker's MT contract is likewise not a fee event, so the full round trip (wrap → trade via `TokenDiff` → unwrap) moves arbitrary notional value while paying `protocol_fee` only on none of it (versus the intended fee on the whole amount).

None of the listed guards (`MultiPayload::verify`, nonce/salt checks, `Lock`, `TransferMatcher::finalize`) address this because the exploit is purely in fee-rate selection, not in authorization, replay, or balance-netting logic — the batch still nets to zero and every signature/nonce is valid; only the fee_collector's expected revenue is short-changed.

### Impact Explanation
The `fee_collector` under-collects `Pips::fee_ceil(protocol_fee, V)` on every trade an attacker routes through a self-issued NEP-245 wrapper with `amount == 1` legs, for arbitrarily large `V`. This is repeatable per trade, per attacker-deployed MT contract, and per token_id (attacker can mint unlimited distinct `mt_token_id` strings), so the blast radius covers all protocol fee revenue that would otherwise be collected on value routed this way — matching "protocol fees bypassed" under the Critical category.

### Likelihood Explanation
Preconditions are minimal and fully within an unprivileged attacker's control: deploy any NEP-245-compatible contract (no allowlisting is required for `Nep245TokenId.contract_id`), wrap real backing value as `amount == 1` positions, deposit via `mt_transfer_call`, and submit `TokenDiff` intents through `execute_intents`/`simulate_intents`. No relayer keys, DAO roles, or victim keys are needed, and the attack is trivially repeatable across accounts and token_ids at negligible cost (just gas and contract deployment).

### Recommendation
Base `token_fee` on the actual value/semantics negotiated by the intent rather than solely on `TokenIdType` and raw `amount`. At minimum, remove the blanket `amount <= 1` exemption for `Nep245`/`Imt` (which are fungible-capable multi-token standards, unlike true NFTs) and apply the standard `protocol_fee` there, reserving the exemption strictly for `Nep171` (single, truly non-fungible NFTs) where fee-ceiling would otherwise force 100% confiscation of an indivisible item.

### Proof of Concept
`cargo test` plan (unit test in `contracts/defuse/core/src/intents/token_diff.rs` or an integration test in `tests/src/tests/defuse/`):
1. Set `protocol_fee = Pips::ONE_PERCENT` (or any nonzero `Pips`).
2. Case A (baseline): construct a `TokenDiff` with a negative delta of `-1_000_000` on a `Nep141TokenId` token; assert `TokenDiff::token_fee(token_id, 1_000_000, protocol_fee).fee_ceil(1_000_000) == Pips::fee_ceil(protocol_fee, 1_000_000)` (nonzero).
3. Case B (exploit): construct a `TokenDiff` with a negative delta of `-1` on a `Nep245TokenId` token (attacker-controlled `contract_id`/`mt_token_id`) representing equal notional value `1_000_000`; assert `TokenDiff::token_fee(token_id, 1, protocol_fee) == Pips::ZERO`, i.e. `fees_collected` for this leg is `0`.
4. Assert the two fee outcomes diverge (`Case A fee > 0`, `Case B fee == 0`) for equal notional value, proving the fee-bypass equality violation.

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

**File:** crates/primitives/token-id/src/nep245.rs (L54-59)
```rust
impl From<&Nep245TokenId> for TokenIdType {
    #[inline]
    fn from(_: &Nep245TokenId) -> Self {
        Self::Nep245
    }
}
```
