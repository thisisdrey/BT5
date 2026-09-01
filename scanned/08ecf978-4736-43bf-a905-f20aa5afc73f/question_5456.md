# Q5456: imt - token type misclassification changes the fee rule (8)

## Question
Given an `ImtMint` has already bound that id to the attacker as minter, can an unprivileged attacker, entering through a `TokenDiff` naming attacker-chosen `TokenId` keys, encode a token so `ImtTokenId` in `crates/primitives/token-id/src/imt.rs` classifies it as a `TokenIdType` whose fee rule differs, exploiting that `Nep171` and small-amount `Nep245`/`Imt` legs pay `Pips::ZERO`, breaking the invariant `the fee rule applied to a token == the rule for the asset class that token actually is` and leading to theft of unclaimed protocol fees / protocol fee bypass?

## Target
- File/function: [crates/primitives/token-id/src/imt.rs](crates/primitives/token-id/src/imt.rs) - `ImtTokenId` (cross-check `from_str` in the same file)
- Entrypoint: a `TokenDiff` naming attacker-chosen `TokenId` keys
- Attacker controls: the token id strings used as map keys in the signed diff
- Exploit idea: `TokenDiff::token_fee` branches on `TokenIdType` and `amount > 1`; a fungible position split into unit-amount legs of a fee-exempt type pays no fee. Set-up: an `ImtMint` has already bound that id to the attacker as minter.
- Invariant to test: the fee rule applied to a token == the rule for the asset class that token actually is
- Expected Immunefi impact: High - Theft of unclaimed protocol fees / protocol fee bypass
- Fast validation: Split a large fungible trade into unit-amount fee-exempt legs; assert the fee collected matches the notional.
