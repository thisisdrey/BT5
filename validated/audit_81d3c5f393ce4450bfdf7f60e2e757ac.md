### No vulnerability found for this question.

The behavior described is explicit, intended design, not a bug. `TokenDiff::token_fee` at [1](#0-0)  contains the comment "do not take fees on NFTs and MTs with |delta| <= 1", and the match arm at lines 211-213 is precisely coded to return `Pips::ZERO` for `Nep171`/`Nep245`/`Imt` token types when `amount <= 1`. This is a deliberate fee-schedule decision (NFT/MT items transferred as single units are treated as non-fungible and therefore fee-exempt), and it is directly covered by the existing test `closure_delta` at [2](#0-1) , which explicitly parametrizes `Nep245TokenId` with delta `1`/`-1` and verifies the closure invariant holds under `Pips::ZERO` fee for those cases alongside non-zero fee tiers.

`From<&Nep245TokenId> for TokenIdType` at [3](#0-2)  simply maps any `Nep245TokenId` to `TokenIdType::Nep245` regardless of `mt_token_id` — there is no code path distinguishing "fungible" vs "non-fungible" semantics within a NEP-245 contract at this layer, so amount-based fee exemption applies uniformly by design to any single-unit transfer, matching the stated intent in the comment.

No balance is moved without authorization, no fee-collector-owed value is stolen from custody, and no invariant binding (`fee_collector` credit == `Σ Pips::fee_ceil` over negative deltas) is violated relative to what the code specifies — the code specifies zero fee for `|delta| <= 1` on these token types as a matter of policy, not as an accidental omission of a guard. Batching many such legs to move "economically significant" value fee-free is an economic consequence of this fee-schedule choice (each unit of an NFT/MT-style token is fee-exempt by design), not an authorization bypass, double-settlement, non-zero-sum batch, `TokenId` collision, or under-collection contradicting an enforced invariant. This is a known/intended trade-off already reflected and tested in the source, not a demonstrable vulnerability under the stated impact categories.

### Citations

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

**File:** contracts/defuse/core/src/intents/token_diff.rs (L228-260)
```rust
    #[rstest]
    #[test]
    fn closure_delta(
        #[values(
            (Nep141TokenId::new("ft.near".parse::<AccountId>().unwrap()).into(), 1_000_000),
            (Nep141TokenId::new("ft.near".parse::<AccountId>().unwrap()).into(), -1_000_000),
            (Nep171TokenId::new("nft.near".parse::<AccountId>().unwrap(), "1".to_string()).into(), 1),
            (Nep171TokenId::new("nft.near".parse::<AccountId>().unwrap(), "1".to_string()).into(), -1),
            (Nep245TokenId::new("mt.near".parse::<AccountId>().unwrap(), "ft1".to_string()).into(), 1_000_000),
            (Nep245TokenId::new("mt.near".parse::<AccountId>().unwrap(), "ft1".to_string()).into(), -1_000_000),
            (Nep245TokenId::new("mt.near".parse::<AccountId>().unwrap(), "nft1".to_string()).into(), 1),
            (Nep245TokenId::new("mt.near".parse::<AccountId>().unwrap(), "nft1".to_string()).into(), -1),
        )]
        token_delta: (TokenId, i128),
        #[values(
            Pips::ZERO,
            Pips::ONE_PIP,
            Pips::ONE_BIP,
            Pips::ONE_PERCENT,
            Pips::ONE_PERCENT * 50,
        )]
        fee: Pips,
    ) {
        let (token_id, delta) = token_delta;
        let closure = TokenDiff::closure_delta(&token_id, delta, fee).unwrap();

        assert_eq!(
            TokenDiff::supply_delta(&token_id, delta, fee).unwrap()
                + TokenDiff::supply_delta(&token_id, closure, fee).unwrap(),
            0,
            "invariant violated for {token_id}: delta: {delta}, closure: {closure}, fee: {fee}",
        );
    }
```

**File:** crates/primitives/token-id/src/nep245.rs (L54-58)
```rust
impl From<&Nep245TokenId> for TokenIdType {
    #[inline]
    fn from(_: &Nep245TokenId) -> Self {
        Self::Nep245
    }
```
