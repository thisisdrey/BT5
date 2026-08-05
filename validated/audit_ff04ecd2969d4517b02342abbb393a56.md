All code citations in the claim are verified accurate against the repository. The legacy `pallet_staking::nominate()` in `substrate/frame/staking/src/pallet/mod.rs` uses `!Validators::<T>::get(&n).blocked` without a `contains_key` check [1](#0-0) , while the sibling `pallet-staking-async::nominate()` correctly requires `Validators::<T>::contains_key(&n)` [2](#0-1) . The regression test `nominating_non_validators_is_not_ok` exists only in `staking-async`'s test suite, confirming it was never ported to legacy `pallet-staking`'s `substrate/frame/staking/src/tests.rs`, which only contains the `blocking_and_kicking_works` test exercising the already-registered-but-blocked case. The `prdoc/stable2509/pr_8436.prdoc` confirms this exact bug class was fixed, and by omission/scope, only in `pallet-staking-async`.

This is a genuine unpatched invariant violation reachable via the public, unprivileged `nominate()` extrinsic, matching the "runtime bugs that compromise intended behavior" impact category (silently wasting nominator voting weight / NPoS election integrity degradation), with a clear, reproducible PoC and named corrupted state (`Nominators::<T>::get(stash).targets` containing a non-existent validator account).

Audit Report

## Title
Legacy `pallet-staking::nominate()` accepts targets that were never registered as validators, wasting the nominator's voting weight - (File: `substrate/frame/staking/src/pallet/mod.rs`)

## Summary
`nominate()` in the legacy `pallet-staking` crate validates a nomination target only via `!Validators::<T>::get(&n).blocked`, without checking `Validators::<T>::contains_key(&n)`. Because the `StorageMap` returns a default `ValidatorPrefs { blocked: false, .. }` for any account that never called `validate()`, an arbitrary, never-registered account is silently accepted as a nomination target.

## Finding Description
In `nominate()`, the per-target closure is: [1](#0-0) . The check `!Validators::<T>::get(&n).blocked` returns `true` (accepting the target) for any `n` not present in `Validators`, because `StorageMap::get` on a missing key returns the type's default value, whose `blocked` field defaults to `false`. This means `Ok(n)` is returned even though `n` was never a validator. The fixed sibling pallet closes this gap by additionally requiring `Validators::<T>::contains_key(&n)`: [2](#0-1) . The legacy pallet's test suite only covers the "registered-but-blocked" path via `blocking_and_kicking_works`, and there is no `nominating_non_validators_is_not_ok`-equivalent test in `substrate/frame/staking/src/tests.rs`, confirming this gap was never exercised for the legacy pallet.

## Impact Explanation
`do_add_nominator` persists the bogus target into `Nominators::<T>`, and the nominator's bonded stake is counted against a "validator" that does not exist in the elected set. This is a runtime bug that compromises intended NPoS election behavior: the affected fraction of the nominator's voting weight becomes a dead vote that cannot back any real validator, silently weakening validator set legitimacy/support without any error surfaced to the caller, and lets an account consume `MaxNominations` slots with non-functional entries.

## Likelihood Explanation
Fully permissionless and trivial to trigger: any bonded, signed account can call `Staking::nominate(origin, targets)` with an account ID that never called `validate()`, at the cost of a normal extrinsic, with no privileged role or off-chain assumption required.

## Recommendation
Mirror the `pallet-staking-async` fix in `substrate/frame/staking/src/pallet/mod.rs`'s `nominate()`: require `Validators::<T>::contains_key(&n)` in conjunction with the `blocked` check before accepting a target, and add a regression test analogous to `nominating_non_validators_is_not_ok`.

## Proof of Concept
1. Bond account `1` via `Staking::bond(...)`.
2. Call `Staking::nominate(RuntimeOrigin::signed(1), vec![999])` where `999` never called `Staking::validate(...)`.
3. The call returns `Ok(())` and `Nominators::<T>::get(1).targets` contains `999`, demonstrating the missing `contains_key` check — contrast with `nominating_non_validators_is_not_ok` in `substrate/frame/staking-async/src/tests/bonding.rs`, which asserts `Error::<Test>::BadTarget` for the identical scenario in the patched pallet.

### Citations

**File:** substrate/frame/staking/src/pallet/mod.rs (L1406-1420)
```rust
			let targets: BoundedVec<_, _> = targets
				.into_iter()
				.map(|t| T::Lookup::lookup(t).map_err(DispatchError::from))
				.map(|n| {
					n.and_then(|n| {
						if old.contains(&n) || !Validators::<T>::get(&n).blocked {
							Ok(n)
						} else {
							Err(Error::<T>::BadTarget.into())
						}
					})
				})
				.collect::<Result<Vec<_>, _>>()?
				.try_into()
				.map_err(|_| Error::<T>::TooManyNominators)?;
```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L2165-2178)
```rust
			let targets: BoundedVec<_, _> = targets
				.into_iter()
				.map(|n| {
					if old.contains(&n) ||
						(Validators::<T>::contains_key(&n) && !Validators::<T>::get(&n).blocked)
					{
						Ok(n)
					} else {
						Err(Error::<T>::BadTarget.into())
					}
				})
				.collect::<Result<Vec<_>, DispatchError>>()?
				.try_into()
				.map_err(|_| Error::<T>::TooManyNominators)?;
```
