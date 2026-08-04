### Title
`Pallet::dispatch_whitelisted_call` / `dispatch_whitelisted_call_with_preimage` swallow the inner call's `DispatchResult`, letting the extrinsic report success even when the whitelisted call failed - (File: `substrate/frame/whitelist/src/lib.rs`)

### Summary
The external report flags `VaultController.__deployAdapter` for not checking the boolean success of an `AdminProxy.execute()` call, so a failed sub-call is silently swallowed and execution continues as if it succeeded. The direct analog in `pallet-whitelist` is `Pallet::clean_and_dispatch`, which dispatches the whitelisted `RuntimeCall` and then discards the `Result<PostDispatchInfo, DispatchErrorWithPostInfo<PostDispatchInfo>>` returned by `call.dispatch(...)`, keeping only the `actual_weight`. The calling extrinsics `dispatch_whitelisted_call` and `dispatch_whitelisted_call_with_preimage` then unconditionally return `Ok(PostDispatchInfo { .. })`, regardless of whether the inner privileged call actually succeeded.

### Finding Description
`clean_and_dispatch` at [1](#0-0)  performs the state cleanup (`WhitelistedCall::remove`, `Preimages::unrequest`, `DeferredDispatch::remove`) unconditionally, then dispatches the call with root origin:

```rust
let result = call.dispatch(frame_system::Origin::<T>::Root.into());
let call_actual_weight = match result {
    Ok(call_post_info) => call_post_info.actual_weight,
    Err(call_err) => call_err.post_info.actual_weight,
};
Self::deposit_event(Event::<T>::WhitelistedCallDispatched { call_hash, result });
call_actual_weight
```

Only `actual_weight` is returned to the caller; the `DispatchResult` itself is dropped except for being emitted as an event field. Both public entry points that call this helper then build their own `PostDispatchInfo` and return `Ok(..)` unconditionally: [2](#0-1) [3](#0-2) 

Neither function checks `result.is_err()` and neither propagates the inner `DispatchError` at the outer dispatchable level — unlike other similar wrappers in the same codebase that explicitly forward the inner error, e.g. `pallet_utility::dispatch_as_fallible` at [4](#0-3) , which does `call.dispatch_bypass_filter(...).map_err(|e| e.error)?;` so the outer extrinsic fails when the inner call fails. `pallet_multisig::as_multi_threshold_1` at [5](#0-4)  likewise returns the inner `result` (propagating success/failure) after emitting the event, not a manufactured `Ok`.

The mirrored analog to the Solidity bug is precise: `AdminProxy.execute()`'s `(bool success, bytes) ` is captured but not checked in `VaultController.__deployAdapter`'s last call — here `call.dispatch(...)`'s `Result` is captured in `result` but not checked before the outer function unconditionally returns `Ok`.

### Impact Explanation
- Any consumer that reasons about extrinsic success (e.g., XCM `Transact` handling via `Config::CallDispatcher::dispatch`, batching pallets like `Scheduler::execute_dispatch`, or off-chain monitoring relying on `ExtrinsicSuccess`/`ExtrinsicFailed`) will observe the whitelisted-call dispatch extrinsic as **always succeeding**, even when the actual privileged governance action (e.g., a runtime upgrade, a treasury spend, or a critical parameter change enacted via governance-whitelisted calls) reverted.
- Because the whitelist and preimage state (`WhitelistedCall::remove`, `Preimages::unrequest`, `DeferredDispatch::remove`) is cleared unconditionally in `clean_and_dispatch` *before* checking the dispatch result, a failed governance-approved call is permanently consumed: the call hash is removed from the whitelist and its preimage request is dropped, so it cannot be re-dispatched without going through the whitelisting process again. This causes a false state acceptance (the pallet believes intended governance action was executed) and a functional-but-silent failure of the governance pipeline — a chain-behavior-compromising bug that fits "runtime bugs that compromise intended behavior" and "forged or mis-bound... state acceptance" in the impact gate.
- For the relayer/deferred-dispatch path (`ensure_signed_deferred_dispatch` → unpaid dispatch by an unprivileged signed account), an attacker-triggered deferred dispatch of a call designed to fail (e.g., hitting a benign `ensure!`/filter check) still emits `DeferredDispatchExecuted` and returns `Ok`, at zero fee (`Pays::No`), even though nothing of consequence executed — enabling free, no-op "successful" relayer submissions that mislead downstream observers about the actual runtime state.

### Likelihood Explanation
This path is reachable by design: `dispatch_whitelisted_call`/`dispatch_whitelisted_call_with_preimage` are public dispatchables gated only by `DispatchWhitelistedOrigin` for the *direct* path, but the deferred-dispatch relayer path is explicitly `ensure_signed` — any signed, unprivileged account can trigger the final dispatch/cleanup once a call has been deferred and the whitelist condition is (re-)satisfied. No malicious validator, collator, or privileged actor is required; only a normal signed extrinsic submission is needed to trigger `clean_and_dispatch` on a call whose inner dispatch fails (e.g., due to filter changes, storage changes, or benign transient conditions between whitelisting and dispatch time).

### Recommendation
Propagate the inner dispatch result instead of discarding it. Change `clean_and_dispatch` to return the full `Result<PostDispatchInfo, DispatchErrorWithPostInfo<PostDispatchInfo>>` (or equivalent), and have `dispatch_whitelisted_call` / `dispatch_whitelisted_call_with_preimage` map it into the outer `DispatchResultWithPostInfo`, mirroring `pallet_utility::dispatch_as_fallible`'s `.map_err(|e| e.error)?` pattern, while still preserving/adjusting `actual_weight` accounting on both the success and failure branches (similar to how `as_derivative` and `if_else` build `post_info` on both paths). Ensure whitelist/preimage/deferred-dispatch state cleanup is not treated as evidence of "success" independent of the inner call's actual outcome, or explicitly document/test that cleanup-on-failure is intentional and that failed governance calls must be re-submitted through whitelisting.

### Proof of Concept
1. Governance (via `WhitelistOrigin`) whitelists `call_hash` for some `RuntimeCall` that will fail when dispatched with `Root` origin at execution time (e.g., a call guarded by a runtime filter or a storage precondition that becomes false between whitelisting and dispatch, such as `ensure!(SomeStorage::get() == expected, Error::Precondition)`).
2. `DispatchWhitelistedOrigin` (or, via the deferred path, any signed relayer after `defer_dispatch`) calls `dispatch_whitelisted_call(origin, call_hash, call_encoded_len, call_weight_witness)`.
3. Inside `clean_and_dispatch`, `WhitelistedCall::remove(call_hash)`, `Preimages::unrequest(&call_hash)`, and `DeferredDispatch::remove(call_hash)` execute unconditionally; `call.dispatch(Root)` then returns `Err(..)`.
4. `call_actual_weight` is still computed from `call_err.post_info.actual_weight`; the extrinsic-level function ignores `result`'s `Err` variant and returns `Ok(PostDispatchInfo { actual_weight, pays_fee })` — the block includes an `ExtrinsicSuccess` for this transaction and a `WhitelistedCallDispatched { result: Err(..) }` event that most simple success/failure observers (and any code path checking only extrinsic-level dispatch outcome) will miss.
5. The whitelisted call cannot be retried (its hash and preimage were already removed), permanently losing the intended governance action while the chain state falsely reflects a successful whitelist-dispatch operation. [6](#0-5) [1](#0-0)

### Citations

**File:** substrate/frame/whitelist/src/lib.rs (L205-248)
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

			let call_data = T::Preimages::fetch(&call_hash, Some(call_encoded_len))
				.map_err(|_| Error::<T>::UnavailablePreImage)?;

			let call = <T as Config>::RuntimeCall::decode_all_with_depth_limit(
				frame::deps::frame_support::MAX_EXTRINSIC_DEPTH,
				&mut &call_data[..],
			)
			.map_err(|_| Error::<T>::UndecodableCall)?;

			ensure!(
				call.get_dispatch_info().call_weight.all_lte(call_weight_witness),
				Error::<T>::InvalidCallWeightWitness
			);

			// Relayer isn't charged; the privileged direct path still pays.
			let pays_fee = if relayer.is_some() { Pays::No } else { Pays::Yes };

			let call_actual_weight = Self::clean_and_dispatch(call_hash, call);
			if let Some(who) = relayer {
				Self::deposit_event(Event::<T>::DeferredDispatchExecuted { call_hash, who });
			}

			let actual_weight = call_actual_weight.map(|w| {
				w.saturating_add(T::WeightInfo::dispatch_whitelisted_call(call_encoded_len))
			});
			Ok(PostDispatchInfo { actual_weight, pays_fee })
		}
```

**File:** substrate/frame/whitelist/src/lib.rs (L278-287)
```rust
			let call_actual_weight = Self::clean_and_dispatch(call_hash, *call);
			if let Some(who) = relayer {
				Self::deposit_event(Event::<T>::DeferredDispatchExecuted { call_hash, who });
			}

			let actual_weight = call_actual_weight.map(|w| {
				w.saturating_add(T::WeightInfo::dispatch_whitelisted_call_with_preimage(call_len))
			});
			Ok(PostDispatchInfo { actual_weight, pays_fee })
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

**File:** substrate/frame/utility/src/lib.rs (L573-585)
```rust
		pub fn dispatch_as_fallible(
			origin: OriginFor<T>,
			as_origin: Box<T::PalletsOrigin>,
			call: Box<<T as Config>::RuntimeCall>,
		) -> DispatchResult {
			ensure_root(origin)?;

			call.dispatch_bypass_filter((*as_origin).into()).map_err(|e| e.error)?;

			Self::deposit_event(Event::DispatchedAs { result: Ok(()) });

			Ok(())
		}
```

**File:** substrate/frame/multisig/src/lib.rs (L336-347)
```rust
			let (call_len, call_hash) = call.using_encoded(|c| (c.len(), blake2_256(&c)));
			let result = call.dispatch(RawOrigin::Signed(id.clone()).into());

			Self::deposit_event(Event::MultisigExecuted {
				approving: who,
				timepoint: Self::timepoint(),
				multisig: id,
				call_hash,
				result: result.map(|_| ()).map_err(|e| e.error),
			});

			result
```
