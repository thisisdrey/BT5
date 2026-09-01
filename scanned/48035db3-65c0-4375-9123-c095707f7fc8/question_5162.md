# Q5162: lock - locked account still mutated through an unchecked path (14)

## Question
Given the attacker deposited 1 unit to force the victim's entry into existence, can an unprivileged attacker, entering through `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry, reach `Lock` in `contracts/defuse/core/src/lock.rs` on a locked account via a path that uses `as_inner_unchecked_mut()` (or `get_or_create(...).as_inner_unchecked_mut()`) instead of `get_mut()`, so the lock does not actually prevent the mutation, breaking the invariant `the set of mutations possible on a locked account == deposits and refunds only` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/lock.rs](contracts/defuse/core/src/lock.rs) - `Lock` (cross-check `get_mut_maybe_forced` in the same file)
- Entrypoint: `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry
- Attacker controls: the target `receiver_id` and the (possibly minimal) deposited amount
- Exploit idea: Deposits, refunds and some account-creation paths deliberately bypass the lock; enumerate which mutations are reachable by an unprivileged caller on a locked victim. Set-up: the attacker deposited 1 unit to force the victim's entry into existence.
- Invariant to test: the set of mutations possible on a locked account == deposits and refunds only
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Lock an account, then attempt every unprivileged entrypoint against it; assert only deposits/refunds succeed.
