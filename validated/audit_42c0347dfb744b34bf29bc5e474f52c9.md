## Analog Found

### Title
`Whitelist::remove_deferred_dispatch` permanently orphans a whitelisted call's preimage request, permanently locking the preimage noter's deposit - (File: `substrate/frame/whitelist/src/lib.rs`)

### Summary
`pallet-whitelist`'s new deferred-dispatch feature splits execution of a governance call into two independent steps: a privileged `dispatch_whitelisted_call`/`dispatch_whitelisted_call_with_preimage` call that "locks in" a `call_hash` (`DeferredDispatch<T>`), and a later permissionless relay of the same `call_hash` once it becomes whitelisted. Exactly like the Symm IO bug, an unrelated, non-malicious, permissionless action taken between step 1 and step 2 (here: calling the cleanup extrinsic `remove_deferred_dispatch` after expiry) removes only half of the state that ties the multi-step process together, leaving a dangling `WhitelistedCall`/`Preimage` request that can never be cleaned up by its owner, permanently locking the reserved preimage deposit.

### Finding Description
The deferred-dispatch flow is:

1. `DispatchWhitelistedOrigin` calls `dispatch_whitelisted_call` for a `call_hash` that is not yet whitelisted → `defer_dispatch` stores `DeferredDispatch::<T>::insert(call_hash, expire_at)`. [1](#0-0) 

2. `WhitelistOrigin` later calls `whitelist_call(call_hash)`, which inserts `WhitelistedCall::<T>` and calls `T::Preimages::request(&call_hash)`, converting the preimage's `RequestStatus` to `Requested` and locking the original noter's reserved deposit until it is later `unrequest`-ed. [2](#0-1) 

3. Any signed account can then relay the call via `ensure_signed_deferred_dispatch`, which checks that `DeferredDispatch` exists, has not expired, and the call is still whitelisted — and dispatch proceeds through `clean_and_dispatch`, which correctly clears **all three** pieces of state: `WhitelistedCall`, `Preimages::unrequest`, and `DeferredDispatch`. [3](#0-2) 

However, if nobody relays the call before `expire_at`, **any signed account** may permissionlessly call `remove_deferred_dispatch`, which only removes the `DeferredDispatch` entry:

```
pub fn remove_deferred_dispatch(origin, call_hash) -> DispatchResultWithPostInfo {
    ensure_signed(origin)?;
    let expire_at = DeferredDispatch::<T>::get(call_hash).ok_or(DeferredDispatchNotFound)?;
    ensure!(now >= expire_at, DeferredDispatchNotExpired);
    DeferredDispatch::<T>::remove(call_hash);
    Self::deposit_event(...);
    Ok(Pays::No.into())
}
``` [4](#0-3) 

Unlike `clean_and_dispatch`, this cleanup path never calls `T::Preimages::unrequest(&call_hash)` and never removes `WhitelistedCall::<T>`. The corrupted persistent value is the `pallet-preimage` `RequestStatus` for that `call_hash`, which remains stuck in the `Requested` state forever. Because `unnote_preimage` in `pallet-preimage` only permits reclaiming a depositor's reserve while the request is `Unrequested`, the original preimage noter's reserved deposit can never be released by them — the same "stuck at step one, unrecoverable by the affected party" shape as the Symm IO nonce bug, where a completely ordinary state transition (here, calling the public cleanup extrinsic after expiry) desynchronizes two pieces of state (`DeferredDispatch` vs. `WhitelistedCall`/preimage request) that were meant to be consumed atomically together.

### Impact Explanation
This is a permanent user-fund lock: the account that called `Preimage::note_preimage` for the call and paid the storage deposit can never call `unnote_preimage` to reclaim it, because the preimage remains marked `Requested` by `pallet-whitelist` indefinitely. Recovery is only possible if the privileged `DispatchWhitelistedOrigin` independently decides to call `dispatch_whitelisted_call`/`_with_preimage` again for the exact same `call_hash` (which is still marked whitelisted) — an action with no guarantee of ever happening and outside the control of the affected depositor. This matches the "permanent user-fund … lock" impact category, and the trigger itself (`remove_deferred_dispatch`) is fully permissionless.

### Likelihood Explanation
The trigger requires no attacker privilege and no malicious actor: it is simply what happens whenever a deferred, whitelisted call is not relayed before `T::DeferredDispatchExpiration` elapses (e.g., due to relayer inactivity, chain congestion, or the relaying incentive—`Pays::No`—not being attractive enough) and someone (anyone) then calls the permissionless `remove_deferred_dispatch` to reclaim its own fee/refund. This is a normal, expected cleanup operation exposed to the public, so the conditions for triggering it are realistic and do not require any privileged or adversarial behavior — directly analogous to the Symm IO report where an ordinary `chargeFundingRate` call (non-malicious) desynchronized the liquidation state.

### Recommendation
`remove_deferred_dispatch` must fully unwind everything that `whitelist_call` set up for that `call_hash`, not just the `DeferredDispatch` marker:
- Call `T::Preimages::unrequest(&call_hash)` to release the preimage request.
- Remove the `WhitelistedCall::<T>` entry (or otherwise ensure the whitelisting state and preimage request lifecycle stay in lock-step with the deferred-dispatch lifecycle).
- Alternatively, make `whitelist_call`/`request_preimage` and `defer_dispatch`/`DeferredDispatch` share a single source of truth so that expiring one always tears down the other atomically.

### Proof of Concept
1. A user calls `Preimage::note_preimage(call_bytes)`, reserving a deposit `D` for `call_hash = H`.
2. `DispatchWhitelistedOrigin` calls `dispatch_whitelisted_call_with_preimage`/`dispatch_whitelisted_call` with `call_hash = H` before it is whitelisted → `DeferredDispatch::insert(H, expire_at)` (see step 1 code cited above).
3. `WhitelistOrigin` calls `whitelist_call(H)` → `WhitelistedCall::insert(H, ())` and `T::Preimages::request(&H)`, converting the preimage's status to `Requested`, locking the noter's deposit `D`.
4. No relayer calls `dispatch_whitelisted_call(_with_preimage)` before `expire_at` (e.g., due to inactivity).
5. Any signed account calls `remove_deferred_dispatch(H)` once `now >= expire_at` → only `DeferredDispatch::remove(H)` executes; `WhitelistedCall::<T>` and the preimage's `Requested` status remain untouched.
6. The original noter attempts `Preimage::unnote_preimage(H)` → fails, because status is still `Requested`, not `Unrequested`. Deposit `D` is now permanently locked unless the privileged `DispatchWhitelistedOrigin` independently re-dispatches `H` again — an action outside the depositor's control.

### Citations

**File:** substrate/frame/whitelist/src/lib.rs (L169-184)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::whitelist_call())]
		pub fn whitelist_call(origin: OriginFor<T>, call_hash: T::Hash) -> DispatchResult {
			T::WhitelistOrigin::ensure_origin(origin)?;

			ensure!(
				!WhitelistedCall::<T>::contains_key(call_hash),
				Error::<T>::CallAlreadyWhitelisted,
			);

			WhitelistedCall::<T>::insert(call_hash, ());
			T::Preimages::request(&call_hash);

			Self::deposit_event(Event::<T>::CallWhitelisted { call_hash });
			Ok(())
		}
```

**File:** substrate/frame/whitelist/src/lib.rs (L205-220)
```rust
		pub fn dispatch_whitelisted_call(
			origin: OriginFor<T>,
			call_hash: T::Hash,
			call_encoded_len: u32,
			call_weight_witness: Weight,
		) -> DispatchResultWithPostInfo {
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

**File:** substrate/frame/whitelist/src/lib.rs (L289-309)
```rust
		#[pallet::call_index(4)]
		#[pallet::weight(T::WeightInfo::remove_deferred_dispatch())]
		pub fn remove_deferred_dispatch(
			origin: OriginFor<T>,
			call_hash: T::Hash,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;

			let expire_at = DeferredDispatch::<T>::get(call_hash)
				.ok_or(Error::<T>::DeferredDispatchNotFound)?;

			let now = T::BlockNumberProvider::current_block_number();

			ensure!(now >= expire_at, Error::<T>::DeferredDispatchNotExpired);

			DeferredDispatch::<T>::remove(call_hash);

			Self::deposit_event(Event::<T>::DeferredDispatchRemoved { call_hash });

			Ok(Pays::No.into())
		}
```

**File:** substrate/frame/whitelist/src/lib.rs (L345-381)
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
