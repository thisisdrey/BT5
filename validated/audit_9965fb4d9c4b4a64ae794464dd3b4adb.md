### No vulnerability found for this question.

The claimed ambiguity does not exist. `Nep171TokenId::from_str`/`Nep245TokenId::from_str` use `data.split_once(':')`, which always splits at the *first* colon in the string [1](#0-0) . Since `contract_id` is a `near_account_id::AccountId`, and NEAR account IDs cannot contain a `:` character, the leftmost colon in `"{contract_id}:{nft_token_id}"` is guaranteed to be exactly the separator placed by `Display`/`Debug` [2](#0-1)  — regardless of how many colons are embedded later inside `nft_token_id` (e.g. `"other.near:5"`). The same reasoning applies at the outer `TokenId::from_str` layer: `TokenIdType` discriminants (`nep141`, `nep171`, `nep245`, `imt`) never contain `:`, so `s.split_once(':')` deterministically isolates the type tag from the variant's data [3](#0-2) .

The repo already has a property test (`display_from_str_roundtrip`) using `arbitrary`-generated `Nep171TokenId`/`Nep245TokenId`/`TokenId` values, which would generate `nft_token_id`/`mt_token_id` strings containing arbitrary characters including colons, and this round-trip already holds [4](#0-3) [5](#0-4) . There is no path by which an attacker-controlled `nft_token_id`/`mt_token_id` containing an embedded `"validaccount:something"` substring can shift the split point, because the split is always anchored at the first colon, which is structurally guaranteed to immediately follow the `AccountId` (which cannot itself contain `:`).

### Citations

**File:** crates/primitives/token-id/src/nep171.rs (L31-43)
```rust
impl std::fmt::Debug for Nep171TokenId {
    #[inline]
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}:{}", self.contract_id, self.nft_token_id)
    }
}

impl std::fmt::Display for Nep171TokenId {
    #[inline]
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Debug::fmt(&self, f)
    }
}
```

**File:** crates/primitives/token-id/src/nep171.rs (L48-53)
```rust
    fn from_str(data: &str) -> Result<Self, Self::Err> {
        let (contract_id, token_id) = data
            .split_once(':')
            .ok_or(strum::ParseError::VariantNotFound)?;
        Ok(Self::new(contract_id.parse::<AccountId>()?, token_id))
    }
```

**File:** crates/primitives/token-id/src/nep171.rs (L69-75)
```rust
    #[rstest]
    #[trace]
    fn display_from_str_roundtrip(#[from(make_arbitrary)] token_id: Nep171TokenId) {
        let s = token_id.to_string();
        let got: Nep171TokenId = s.parse().unwrap();
        assert_eq!(got, token_id);
    }
```

**File:** crates/primitives/token-id/src/lib.rs (L84-96)
```rust
    #[inline]
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let (typ, data) = s
            .split_once(':')
            .ok_or(strum::ParseError::VariantNotFound)?;
        match typ.parse()? {
            TokenIdType::Nep141 => data.parse().map(Self::Nep141),
            TokenIdType::Nep171 => data.parse().map(Self::Nep171),
            TokenIdType::Nep245 => data.parse().map(Self::Nep245),
            TokenIdType::Imt => data.parse().map(Self::Imt),
        }
    }
}
```

**File:** crates/primitives/token-id/src/lib.rs (L180-186)
```rust
    #[rstest]
    #[trace]
    fn display_from_str_roundtrip(#[from(make_arbitrary)] token_id: TokenId) {
        let s = token_id.to_string();
        let got: TokenId = s.parse().unwrap();
        assert_eq!(got, token_id);
    }
```
