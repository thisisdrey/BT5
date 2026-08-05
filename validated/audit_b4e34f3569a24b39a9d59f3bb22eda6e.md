Audit Report

## Title
Nominator/validator reward permanently lost when settlement transfer fails after payout state is marked claimed - (File: substrate/frame/staking-async/src/pallet/impls.rs)

## Summary
`do_payout_stakers_by_page` calls `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` at impls.rs:386 before any actual token settlement occurs. The settlement is performed later in `make_payout_from_provider` (impls.rs:598-616) via `T::Currency::transfer(...)`, which can fail (e.g. `FundsUnavailable`, ED-related failures under `Preservation::Expendable`); on failure the code only logs the error and returns `None` (impls.rs:607-616), never propagating an error or reverting the claimed marker.

## Finding Description
The exact code path is: `do_payout_stakers_by_page` validates inputs, then unconditionally sets the claim marker via `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` (impls.rs:386), before the exposure/reward split are even computed. It then dispatches payouts via `payout_from_provider` (impls.rs:481-516), which calls `make_payout_from_provider` per validator/nominator (impls.rs:578-630). Inside `make_payout_from_provider`, `T::Currency::transfer(&staker_rewards_pot, &payout_account, amount, Preservation::Expendable)` is attempted; on `Err`, the code logs the error and returns `None` (impls.rs:602-616) without surfacing any failure to the caller of `do_payout_stakers_by_page`, which still returns `Ok(...)` overall.

Because the claim marker was already persisted before the transfer attempt, subsequent calls to the payout dispatchable for the same `(era, stash, page)` will hit the early-return guard `if Eras::<T>::is_rewards_claimed(era, &stash, page) { return Err(Error::<T>::AlreadyClaimed...) }` (impls.rs:381-384), permanently blocking any retry for that page — even for the specific account whose transfer failed. There is no compensating rollback of the claimed flag and no per-account retry/reclaim mechanism. This is a genuine violation of the required invariant that payout/settlement markers must only advance after settlement actually succeeds.

## Impact Explanation
The payout dispatchable is public and unprivileged — any signed account can call it on behalf of any validator/era/page (this matches the general staking `payout_stakers`/`payout_stakers_by_page` dispatchable pattern, confirmed present in `substrate/frame/staking-async/src/pallet/mod.rs`). If a nominator's or validator's destination account is in a state that causes `Currency::transfer` to fail (frozen/locked balance from another pallet, insufficient balance to satisfy existential deposit under `Preservation::Expendable`, etc.), that specific reward becomes permanently and silently unrecoverable: the reward pot balance stays stranded, the claim marker is irreversibly set, and no error is returned to alert the caller. This matches the allowed "permanent user-fund lock" impact category, and the corrupted value is precisely `Eras::<T>::is_rewards_claimed(era, &stash, page)` being set to true despite the underlying transfer to `stash`/nominator having failed.

## Likelihood Explanation
This is reachable without any privileged action: `Currency::transfer` failures from locks/freezes placed by other pallets (e.g. democracy, nomination-pools, or a drained account below ED) are a realistic, non-exotic condition. Since payout calls are permissionless and callable by anyone at any time for any page, an attacker could also proactively arrange for a target account's balance/lock state to trigger the failure right before invoking payout, deterministically stranding that specific reward. This is a repeatable failure mode, not a one-off edge case.

## Recommendation
Do not persist the claimed marker until settlement transfers have been attempted and their outcome is known. Options: (1) perform transfers first and only call `set_rewards_as_claimed` after success/accepted-partial success; (2) on transfer failure, do not treat that specific stash's share as claimed — track per-account unclaimed remainders in a sweepable storage item; or (3) surface failures from `do_payout_stakers_by_page` (partial success reporting) so affected nominators can remediate their account state and retry before the page is fully marked claimed.

## Proof of Concept
1. A validator has at least one nominator `N` in an exposure page for era `E`, page `P`.
2. Arrange for `N`'s payout destination account to be in a state where an inbound `Currency::transfer` with `Preservation::Expendable` fails (e.g. externally-imposed lock/freeze, or account below ED with no existing providers).
3. Any signed account calls the payout dispatchable for `(validator_stash, E, P)`.
4. `Eras::<T>::set_rewards_as_claimed(E, &stash, P)` executes at impls.rs:386; later, `make_payout_from_provider` for `N` hits the `Err` branch at impls.rs:602-616, logs the error, returns `None`; no `Rewarded` event is emitted for `N`; the extrinsic overall returns `Ok`.
5. Any subsequent call to the payout dispatchable for `(validator_stash, E, P)` immediately fails with `Error::<T>::AlreadyClaimed` (impls.rs:381-384), permanently preventing `N` from ever receiving that reward, which remains stranded in the era's staker-reward pot account.