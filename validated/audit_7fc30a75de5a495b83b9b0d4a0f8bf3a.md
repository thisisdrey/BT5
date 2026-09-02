This confirms MT tokens (NEP-245, wrapped `nep141` deposits) can represent arbitrary raw amounts — including amounts scaled by decimals so that a single raw unit (`amount == 1`) can correspond to significant real value, since MT `token_id`s here typically wrap NEP-141 balances 1:1 in raw units (e.g. `nep245:defuse.test.near:nep141:ft1.test.near`) with no forced "NFT-like" semantics.### Title
Protocol fee is fully bypassed on `TokenDiff` legs denominated in wrapped NEP-245/IMT tokens with unit (`amount == 1`) deltas - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee()` hard-codes `Pips::ZERO` for `Nep245`/`Imt` token legs whenever the transferred amount is `<= 1`, on the assumption that such tokens are NFT-like and indivisible. However, `Nep245` token IDs in this system are also used to wrap ordinary fungible balances (including deposited NEP-141 tokens, e.g. `nep245:<contract>:nep141:<ft_contract>`) 1:1 in raw units, and `Imt` tokens are freely mintable by any signer via `ImtMint` with an arbitrary decimal convention chosen by the minter. This decouples the "fee owed" determination from actual economic value transferred — mirroring the referenced Putty finding where fee was tied to a proxy (strike) instead of real value (premium).

### Finding Description
In `TokenDiff::execute_intent` (`contracts/defuse/core/src/intents/token_diff.rs:41-105`), fees are computed per-token on the negative ("token_in") delta:
```
let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
```
`token_fee` (lines 206-217) returns `Pips::ZERO` for `Nep245`/`Imt` tokens whenever `amount <= 1`, and always for `Nep171` (NFTs):
```rust
pub fn token_fee(token_id: impl Into<TokenIdType>, amount: u128, fee: Pips) -> Pips {
    let token_id = token_id.into();
    match token_id {
        TokenIdType::Nep141 => {}
        TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}
        TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO,
    }
    fee
}
```
This treats "amount == 1" as a reliable proxy for "indivisible NFT-like item of negligible/arbitrary value," but nothing in the `TokenId` scheme enforces that. A `Nep245TokenId` can wrap an arbitrary underlying NEP-141 balance at 1:1 raw-unit granularity (confirmed by `defuse2_mt`/`ft1_id` wrapping tests and `mt_on_transfer` deposit flow), and `Imt` tokens have their raw unit scale chosen entirely by the minter at `ImtMint` time (`contracts/defuse/core/src/intents/imt.rs`). An attacker can therefore engineer (or select) a `Nep245`/`Imt` token representation whose single raw unit corresponds to an arbitrarily large economic value, and route the "token_in" leg of a `TokenDiff` swap through that representation with `delta == -1`, guaranteeing `token_fee()` returns `Pips::ZERO` and no fee is collected on that leg — regardless of the trade's real notional value. This is the exact bug class from the external report: fee eligibility keyed to a manipulable proxy quantity (unit count / strike) rather than the actual value moved (premium / true swap notional), letting large-value trades escape the fee entirely by wrapping/minting the asset with a single indivisible unit.

### Impact Explanation
This matches the Critical impact category "fees bypassed or over-collected." Any solver/market maker structuring their `TokenDiff` intents to route the "token_in" side through a unit-quantity `Nep245`/`Imt` token wrapper avoids paying the protocol fee on that leg entirely, which directly reduces protocol fee revenue that should scale with the actual value transacted. Because `Nep245` wrapping and `Imt` minting are both permissionless/self-serve within the defuse core (any account can deposit through NEP-245 wrapping or mint an `Imt` token), this is reachable by any unprivileged user without requiring DAO/relayer/role privileges.

### Likelihood Explanation
Likelihood is limited to swap structures where at least one leg can be legitimately or artificially denominated as a `Nep245`/`Imt` token with a unit amount that represents non-trivial value — this requires deliberate structuring by a sophisticated user/solver (e.g., minting an `Imt` token with a coarse decimal convention, or wrapping/aggregating an NEP-245 balance to trade at `delta = ±1`), rather than occurring accidentally on ordinary trades. Given `Imt` minting is fully permissionless and denomination is minter-controlled, and `Nep245` wrapping is a normal deposit path in the system, the precondition is straightforward to satisfy for a motivated actor.

### Recommendation
Do not key fee eligibility off a fixed absolute unit threshold (`amount <= 1`) for `Nep245`/`Imt` token types. If the intent behind the exemption is genuinely to avoid non-sensical fee rounding on truly indivisible NFT-style multi-tokens, that distinction should be encoded in the `TokenId`/token metadata itself (e.g., a supply-cap-of-one / explicit NFT flag validated at mint/registration time) rather than inferred from the transferred quantity, since quantity is fully attacker-controlled and independent of value. At minimum, apply the fee uniformly to `Nep245`/`Imt` deltas the same way it's applied to `Nep141`, and reserve the zero-fee exemption strictly for `Nep171` (true NFTs, where token identity itself already enforces "1 of a kind").

### Proof of Concept
1. Attacker (as any user, no special privilege) mints an `Imt` token via `ImtMint` (`contracts/defuse/core/src/intents/imt.rs:19-40`) representing a synthetic asset, deliberately choosing to keep the entire tradable balance under 1 raw unit's granularity (i.e., agrees off-chain that "1 unit == full notional"), or alternatively deposits into an NEP-245 wrapper such that the counterparty's true economic exposure is captured in a single raw unit of a `Nep245` token id.
2. Attacker (or attacker's counterparty acting as solver) submits a `TokenDiff` intent (`contracts/defuse/core/src/intents/token_diff.rs`) where the "token_in" leg uses this `Imt`/`Nep245` token id with `delta = -1`, and the "token_out" leg is the real high-value asset (e.g., a `Nep141` stablecoin) they receive.
3. In `execute_intent`, `Self::token_fee(token_id, 1, protocol_fee)` hits the `TokenIdType::Nep245 | TokenIdType::Imt if amount > 1` guard with `amount == 1`, falling through to the `=> return Pips::ZERO` arm — no fee is added to `fees_collected` for this leg, even though the leg represents the full notional of the trade.
4. Compare to a functionally identical trade denominated purely in `Nep141` at the same real notional: the protocol fee (`engine.state.fee()`) is charged in full via `fee_ceil(amount)`. This demonstrates the same value of economic activity yields different (bypassable) protocol fee outcomes purely based on which `TokenId` variant/unit-scale is chosen for one leg — an equality break between "fee owed" (should scale with actual value moved) and "fee collected" (zero, due to the `amount <= 1` proxy check).