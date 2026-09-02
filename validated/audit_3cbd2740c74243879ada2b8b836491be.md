### No vulnerability found for this question.

**Analysis supporting this conclusion:**

The claimed collision cannot occur, for two independent reasons:

1. **`AccountId` cannot contain `:`.** NEAR account IDs are restricted to lowercase alphanumerics and the separators `.`, `_`, `-`; `:` is not a valid character. Since `ImtTokenId::new`'s `minter_id` field is a validated `AccountId` [1](#0-0) , `signer_a != signer_b` implies their string forms are also different (distinct valid `AccountId` strings never coincide). Because `Display` writes `"{minter_id}:{token_id}"` [2](#0-1) , and the `minter_id` prefix differs between the two mints, the resulting strings can never be equal regardless of what `token_id` (attacker-controlled, up to `MAX_TOKEN_ID_LEN`) contains.

2. **The actual balance map key is not the `Display` string at all** — `TokenId::Imt(ImtTokenId)` is stored/compared via the struct's derived `PartialEq, Eq, Hash` on its typed fields (`minter_id: AccountId`, `token_id: String`) [3](#0-2) , not via any stringified form. `total_supplies`/`token_balances` maps key on this typed `TokenId`, so even a hypothetical `Display` collision would not merge two distinct structs — equality requires the `minter_id` fields to be literally equal, which contradicts `signer_a != signer_b`.

The existing round-trip test `display_from_str_roundtrip` already covers the intra-pair `Display`/`FromStr` correctness [4](#0-3) , and `into_generic_tokens` constructs `ImtTokenId::new(minter_id, token_id)` directly from the verified `ImtMint` signer [5](#0-4) , so the `(minter_id, token_id)` pair a `TokenId::Imt` key represents is exactly the pair authorized by that mint's signer, with no cross-minter collision path.

### Citations

**File:** crates/primitives/token-id/src/imt.rs (L14-19)
```rust
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ImtTokenId {
    pub minter_id: AccountId,

    pub token_id: String,
}
```

**File:** crates/primitives/token-id/src/imt.rs (L37-42)
```rust
impl std::fmt::Display for ImtTokenId {
    #[inline]
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Debug::fmt(&self, f)
    }
}
```

**File:** crates/primitives/token-id/src/imt.rs (L69-75)
```rust
    #[rstest]
    #[trace]
    fn display_from_str_roundtrip(#[from(make_arbitrary)] token_id: ImtTokenId) {
        let s = token_id.to_string();
        let got: ImtTokenId = s.parse().unwrap();
        assert_eq!(got, token_id);
    }
```

**File:** contracts/defuse/core/src/tokens.rs (L26-46)
```rust
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
```
