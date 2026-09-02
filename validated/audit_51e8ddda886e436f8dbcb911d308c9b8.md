### Title
Protocol fee bypass on `TokenDiff` swaps via `Nep245`/`Imt` token_in legs with amount ≤ 1 - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
The `TokenDiff` intent charges the protocol fee only on the negative delta ("token_in") leg of a swap, and the fee rate applied to that leg is determined by `TokenDiff::token_fee()`. That function unconditionally zeroes the fee for `TokenIdType::Nep245` and `TokenIdType::Imt` tokens whenever the traded amount is `≤ 1`, regardless of how much real value that single unit represents. Because both `Nep245` (multi-token/cross-instance wrapped assets) and `Imt` (user-mintable "Intents Multi Token") token types can represent arbitrarily large notional value per unit, a user can structure any swap so that the leg they give up (`token_in`) is exactly `1` unit of such a token, permanently skirting the protocol fee on that leg — exactly the same "future fees may be skirted by choosing a token type not covered by the fee mechanism" bug class as the external report, but expressed through the fee-exempt token-type/amount heuristic instead of a non-ETH reward token.

### Finding Description
`TokenDiff::execute_intent` collects fees only from negative deltas (`token_in`): [1](#0-0) 

The fee rate itself is computed by `token_fee()`: [2](#0-1) 

This logic assumes `Nep245`/`Imt` tokens with `amount ≤ 1` are always negligible-value NFT-like transfers, mirroring how `Nep171` (true NFTs) are always fee-exempt. However:
- `Imt` tokens are minted arbitrarily by any signer via `ImtMint`, with amounts and semantics fully controlled by the minter — there is no constraint tying "1 unit" to negligible value: [3](#0-2) [4](#0-3) 
- `Nep245` covers general multi-token assets, including tokens bridged/wrapped from external multi-token contracts or other `defuse` instances, whose "amount" granularity is defined by the external contract, not by this protocol. A multi-token with coarse-grained units (e.g., 0-decimal share/vault tokens) can have `amount = 1` represent large real value.

Since the fee exemption is keyed purely on the raw integer `amount` field rather than on real economic value, any user can pick (or mint, in the `Imt` case) a token whose native unit size lets them always trade with `delta == ±1` on the `token_in` side, thereby making `token_fee()` return `Pips::ZERO` for that leg no matter how much value is actually being moved.

### Impact Explanation
This crosses the "fees owed versus fees collected" boundary described in scope: the protocol fee is deterministically bypassable for any swap where the fee-payer structures their `token_in` as a `Nep245`/`Imt` token with `amount = 1`. Because `Imt` tokens are freely mintable with attacker-chosen semantics, and `Nep245` covers externally-defined multi-tokens, this is a broadly reachable, systemic underpayment of protocol fees — not a one-off edge case. This matches the Critical impact bucket ("fees bypassed or over-collected").

### Likelihood Explanation
High. No privileged role, no misconfiguration, and no assumption about a deployment ignoring documented configuration is required — the exemption is hard-coded in `token_fee()` for all `defuse` deployments that enable `Nep245` trading or the `imt` feature (which is exercised extensively throughout the test suite, indicating it is an expected, shipped feature). Any two mutually-consenting counterparties (or a single actor swapping against their own second account/instance) can trivially structure a `TokenDiff` where the `token_in` leg uses an `Imt`/`Nep245` token with `amount = 1`.

### Recommendation
Do not exempt `Nep245`/`Imt` tokens from fees based purely on the raw `amount` being `≤ 1`. Either:
- Remove the amount-based exemption entirely for `Nep245`/`Imt` and charge protocol fees identically to `Nep141`, or
- Restrict the "amount ≤ 1 is fee-exempt" heuristic to token types that are provably non-fungible (e.g., only `Nep171`, which is guaranteed NFT semantics), and require any `Nep245`/`Imt` token wanting NFT-like fee exemption to declare/verify that its granularity is truly 1-of-a-kind (e.g., via an explicit supply-cap or decimals field checked on mint/registration).

### Proof of Concept
1. Attacker (as their own minter) calls `ImtMint` to mint `1` unit of a custom `Imt` token `X` to themselves (or arranges/holds a `Nep245`-wrapped asset where `1` unit represents large real value, e.g., a 0-decimal vault-share token bridged into `defuse`).
2. Attacker signs a `TokenDiff` intent with `diff = { X: -1, RealValuableToken: +N }` and a counterparty signs the complementary `TokenDiff` with `diff = { X: +1, RealValuableToken: -N }`.
3. During `TokenDiff::execute_intent`, fees are only assessed on negative deltas: for the attacker, the only negative delta is `X: -1`; `token_fee()` returns `Pips::ZERO` for this leg (`TokenIdType::Imt` with `amount == 1`), so `fees_collected` for the attacker is empty.
4. The attacker receives `N` units of `RealValuableToken` while paying zero protocol fee on the trade, regardless of how large `N` is. [5](#0-4)

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

**File:** contracts/defuse/core/src/intents/imt.rs (L19-40)
```rust
pub struct ImtMint {
    /// Receiver of the minted tokens
    pub receiver_id: AccountId,

    /// The `token_ids` will be wrapped to bind the token ID to the
    /// minter authority (i.e. signer of this intent).
    /// The final string representation of the token will be as follows:
    /// `imt:<minter_id>:<token_id>`
    #[serde_as(as = "Amounts<BTreeMap<_, DisplayFromStr>>")]
    pub tokens: ImtTokens,

    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memo: Option<String>,

    /// Optionally notify `receiver_id` via `mt_on_transfer()`
    ///
    /// NOTE: `min_gas` is adjusted with following values:
    /// * minimum: 5TGas
    /// * default: 30TGas
    #[serde(flatten, default, skip_serializing_if = "Option::is_none")]
    pub notification: Option<NotifyOnTransfer>,
}
```

**File:** contracts/defuse/core/src/tokens.rs (L24-47)
```rust
    pub type ImtTokens = Amounts<BTreeMap<defuse_nep245::TokenId, u128>>;

    impl ImtTokens {
        #[inline]
        pub fn into_generic_tokens(
            self,
            minter_id: &AccountIdRef,
        ) -> Result<Amounts<BTreeMap<TokenId, u128>>> {
            let tokens = self
                .into_iter()
                .map(|(token_id, amount)| {
                    if token_id.len() > MAX_TOKEN_ID_LEN {
                        return Err(DefuseError::TokenIdTooLarge(token_id.len()));
                    }

                    let token = ImtTokenId::new(minter_id, token_id).into();

                    Ok((token, amount))
                })
                .collect::<Result<_, _>>()?;

            Ok(Amounts::new(tokens))
        }
    }
```
