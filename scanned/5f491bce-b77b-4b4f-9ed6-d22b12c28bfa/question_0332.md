# Q0332: force - locked account still mutated through an unchecked path (2)

## Question
Given the victim account has no stored entry yet, can an unprivileged attacker, entering through `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry, reach `is_account_locked` in `contracts/defuse/src/contract/accounts/force.rs` on a locked account via a path that uses `as_inner_unchecked_mut()` (or `get_or_create(...).as_inner_unchecked_mut()`) instead of `get_mut()`, so the lock does not actually prevent the mutation, breaking the invariant `the set of mutations possible on a locked account == deposits and refunds only` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/accounts/force.rs](contracts/defuse/src/contract/accounts/force.rs) - `is_account_locked`
- Entrypoint: `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry
- Attacker controls: the target `receiver_id` and the (possibly minimal) deposited amount
- Exploit idea: Deposits, refunds and some account-creation paths deliberately bypass the lock; enumerate which mutations are reachable by an unprivileged caller on a locked victim. Set-up: the victim account has no stored entry yet.
- Invariant to test: the set of mutations possible on a locked account == deposits and refunds only
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Lock an account, then attempt every unprivileged entrypoint against it; assert only deposits/refunds succeed.
