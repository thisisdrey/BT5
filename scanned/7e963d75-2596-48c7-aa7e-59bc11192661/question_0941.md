# Q0941: nep171 - failed promise treated as fully used (no refund) (5)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote, make the promise feeding `NonFungibleTokenForceWithdrawer` in `contracts/defuse/src/tokens/nep171.rs` fail in a way the resolver classifies as 'used', so the debited balance is neither delivered nor returned, breaking the invariant `for every withdrawal, assets delivered + assets refunded == assets debited` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/src/tokens/nep171.rs](contracts/defuse/src/tokens/nep171.rs) - `NonFungibleTokenForceWithdrawer` (cross-check `nft_withdraw` in the same file)
- Entrypoint: `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote
- Attacker controls: `sender_id`, `amount`, the `msg` (receiver, notify, or nested intents), and the token's own behaviour
- Exploit idea: `ft_resolve_withdraw` sets `used = amount` on any promise error when `is_call`, deliberately not refunding; probe whether an attacker can force that error path for a victim's withdrawal. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: for every withdrawal, assets delivered + assets refunded == assets debited
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Force the callee to fail after the resolver's gas budget is exhausted; assert whether the owner's balance is restored.
