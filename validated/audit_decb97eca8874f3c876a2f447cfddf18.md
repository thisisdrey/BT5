Audit Report

## Title
Unbounded relayer-supplied `call_weight_witness` in permissionless `pallet_whitelist::dispatch_whitelisted_call` enables free, oversized-weight extrinsics that degrade block production - (File: `substrate/frame/whitelist/src/lib.rs`)

## Summary
`pallet-whitelist`'s deferred-dispatch feature lets any signed account relay a governance-approved whitelisted call via `dispatch_whitelisted_call`, but the extrinsic's declared pre-dispatch weight is computed directly from a caller-supplied `call_weight_witness` that is validated only as a lower bound on the real call weight, never an upper bound, while the relayer path is fee-free (`Pays::No`). This lets an unprivileged relayer submit a cheap inner call wrapped in an arbitrarily inflated declared weight, consuming a large share of a block's weight budget for free.

## Finding Description
`dispatch_whitelisted_call`'s weight annotation feeds `call_weight_witness` straight into pre-dispatch weight accounting: `T::WeightInfo::dispatch_whitelisted_call(*call_encoded_len).saturating_add(*call_weight_witness)` [1](#0-0) . Once a privileged origin has deferred a call hash via `defer_dispatch` and it has been whitelisted, any signed account can reach the relayer branch through `ensure_signed_deferred_dispatch`, which only checks that the deferred entry exists, hasn't expired, and the call is still whitelisted — it performs no check on the caller-supplied weight [2](#0-1) [3](#0-2) .

The only validation on `call_weight_witness` inside the call body is a one-sided lower-bound check: `ensure!(call.get_dispatch_info().call_weight.all_lte(call_weight_witness), Error::<T>::InvalidCallWeightWitness)` [4](#0-3) . This guarantees the witness is *at least* the real weight but places no ceiling on it, so a relayer can set it far above the actual weight. Combined with `pays_fee = if relayer.is_some() { Pays::No } else { Pays::Yes }` [5](#0-4) , the relayer pays zero transaction fee regardless of how inflated the declared weight is.

This differs materially from the sibling extrinsic `dispatch_whitelisted_call_with_preimage`, where the call itself is supplied inline and its weight is derived directly from `call.get_dispatch_info().call_weight` at weight-annotation time — no attacker-controlled witness exists there [6](#0-5) . The witness pattern is necessary in `dispatch_whitelisted_call` only because the real call is fetched from preimage storage during execution and is unknown at weight-annotation time, but the implementation trusts the caller's value upward without bound.

Because block authorship fits extrinsics into a block using their declared pre-dispatch weight before execution (as in `sc-basic-authorship`'s `apply_extrinsics` loop, which checks `HitBlockWeightLimit` against declared weight), an inflated witness can crowd out legitimate transactions from the same block, and any correction via `actual_weight` in `PostDispatchInfo` happens only after the block-fit decision was already made.

## Impact Explanation
An unprivileged signed account can submit, for zero fee, an extrinsic whose declared weight is set close to the `Normal` dispatch class's maximum extrinsic weight while its true execution cost is negligible (e.g., relaying a cheap governance-approved call). This directly matches "public underpriced work that degrades block production": it wastes block weight capacity that could have serviced other transactions, without cost to the attacker, and requires no validator, collator, governance, or relayer-trust compromise.

## Likelihood Explanation
Exploitation requires only a signed account and a publicly-known, currently deferred-and-whitelisted `call_hash`, which is discoverable via the `DispatchDeferred` and `CallWhitelisted` events/storage. No privileged access is needed. The abuse is bounded to one occurrence per governance-created deferred entry, since `clean_and_dispatch` clears the `WhitelistedCall` and `DeferredDispatch` storage after execution [7](#0-6) , making this an opportunistic rather than continuously repeatable attack, but it remains reachable by any ordinary user without special conditions.

## Recommendation
Enforce a tight bound on `call_weight_witness` relative to the call's actual measured weight (e.g., reject witnesses that exceed the real weight by more than a small tolerance), or cap the witness independent of caller input, or make the relayer path's fee/weight-cost proportional to the declared witness so inflating it is economically penalized rather than free.

## Proof of Concept
1. Privileged origin defers a cheap call (e.g. `system::remark`) via `dispatch_whitelisted_call(call_hash, len, weight)` before it is whitelisted, creating a `DeferredDispatch` entry with an expiry [8](#0-7) .
2. Privileged origin calls `whitelist_call(call_hash)`.
3. Attacker (any signed account) calls `dispatch_whitelisted_call(call_hash, correct_len, call_weight_witness)` with `call_weight_witness` set near the Normal dispatch class's maximum extrinsic weight.
4. `ensure_signed_deferred_dispatch` succeeds since the entry exists, is unexpired, and is whitelisted.
5. `ensure!(call.get_dispatch_info().call_weight.all_lte(call_weight_witness), ...)` passes trivially since the real call weight is far below the inflated witness.
6. The extrinsic dispatches with `Pays::No`, consuming the inflated declared weight from the block's weight budget for free while the real inner call executes cheaply.

### Citations

**File:** substrate/frame/whitelist/src/lib.rs (L200-204)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight(
			T::WeightInfo::dispatch_whitelisted_call(*call_encoded_len)
				.saturating_add(*call_weight_witness)
		)]
```

**File:** substrate/frame/whitelist/src/lib.rs (L211-220)
```rust
			let relayer = match T::DispatchWhitelistedOrigin::try_origin(origin) {
				Ok(_) if WhitelistedCall::<T>::contains_key(call_hash) => None,
				Ok(_) => {
					Self::defer_dispatch(call_hash)?;
					return Ok(Some(T::WeightInfo::defer_dispatch(0)).into());
				},
				Err(dispatch_origin) => {
					Some(Self::ensure_signed_deferred_dispatch(dispatch_origin, call_hash)?)
				},
			};
```

**File:** substrate/frame/whitelist/src/lib.rs (L231-234)
```rust
			ensure!(
				call.get_dispatch_info().call_weight.all_lte(call_weight_witness),
				Error::<T>::InvalidCallWeightWitness
			);
```

**File:** substrate/frame/whitelist/src/lib.rs (L236-237)
```rust
			// Relayer isn't charged; the privileged direct path still pays.
			let pays_fee = if relayer.is_some() { Pays::No } else { Pays::Yes };
```

**File:** substrate/frame/whitelist/src/lib.rs (L250-256)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight({
			let call_weight = call.get_dispatch_info().call_weight;
			let call_len = call.encoded_size() as u32;
			T::WeightInfo::dispatch_whitelisted_call_with_preimage(call_len)
				.saturating_add(call_weight)
		})]
```

**File:** substrate/frame/whitelist/src/lib.rs (L318-330)
```rust
	fn defer_dispatch(call_hash: T::Hash) -> DispatchResult {
		let now = T::BlockNumberProvider::current_block_number();

		let expire_at = now.saturating_add(T::DeferredDispatchExpiration::get());

		ensure!(!DeferredDispatch::<T>::contains_key(call_hash), Error::<T>::AlreadyDeferred);

		DeferredDispatch::<T>::insert(call_hash, expire_at);

		Self::deposit_event(Event::<T>::DispatchDeferred { call_hash });

		Ok(())
	}
```

**File:** substrate/frame/whitelist/src/lib.rs (L345-362)
```rust
	fn ensure_signed_deferred_dispatch(
		origin: T::RuntimeOrigin,
		call_hash: T::Hash,
	) -> Result<T::AccountId, DispatchError> {
		let who = ensure_signed(origin)?;

		let expire_at =
			DeferredDispatch::<T>::get(call_hash).ok_or(Error::<T>::DeferredDispatchNotFound)?;

		ensure!(
			T::BlockNumberProvider::current_block_number() < expire_at,
			Error::<T>::DeferredDispatchExpired
		);

		ensure!(WhitelistedCall::<T>::contains_key(call_hash), Error::<T>::CallIsNotWhitelisted);

		Ok(who)
	}
```

**File:** substrate/frame/whitelist/src/lib.rs (L364-381)
```rust
	/// Clean whitelisting/preimage and dispatch call.
	///
	/// Returns the inner call's actual weight.
	fn clean_and_dispatch(call_hash: T::Hash, call: <T as Config>::RuntimeCall) -> Option<Weight> {
		WhitelistedCall::<T>::remove(call_hash);
		T::Preimages::unrequest(&call_hash);
		DeferredDispatch::<T>::remove(call_hash);

		let result = call.dispatch(frame_system::Origin::<T>::Root.into());

		let call_actual_weight = match result {
			Ok(call_post_info) => call_post_info.actual_weight,
			Err(call_err) => call_err.post_info.actual_weight,
		};
		Self::deposit_event(Event::<T>::WhitelistedCallDispatched { call_hash, result });

		call_actual_weight
	}
```
