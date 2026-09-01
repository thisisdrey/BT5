# Q2659: nep141 - token type misclassification changes the fee rule (4)

## Question
Given the sub-token id is the empty string, can an unprivileged attacker, entering through a `TokenDiff` naming attacker-chosen `TokenId` keys, encode a token so `Nep141TokenId` in `crates/primitives/token-id/src/nep141.rs` classifies it as a `TokenIdType` whose fee rule differs, exploiting that `Nep171` and small-amount `Nep245`/`Imt` legs pay `Pips::ZERO`, breaking the invariant `the fee rule applied to a token == the rule for the asset class that token actually is` and leading to theft of unclaimed protocol fees / protocol fee bypass?

## Target
- File/function: [crates/primitives/token-id/src/nep141.rs](crates/primitives/token-id/src/nep141.rs) - `Nep141TokenId` (cross-check `from_str` in the same file)
- Entrypoint: a `TokenDiff` naming attacker-chosen `TokenId` keys
- Attacker controls: the token id strings used as map keys in the signed diff
- Exploit idea: `TokenDiff::token_fee` branches on `TokenIdType` and `amount > 1`; a fungible position split into unit-amount legs of a fee-exempt type pays no fee. Set-up: the sub-token id is the empty string.
- Invariant to test: the fee rule applied to a token == the rule for the asset class that token actually is
- Expected Immunefi impact: High - Theft of unclaimed protocol fees / protocol fee bypass
- Fast validation: Split a large fungible trade into unit-amount fee-exempt legs; assert the fee collected matches the notional.
