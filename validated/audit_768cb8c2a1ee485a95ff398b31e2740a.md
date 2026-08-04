### Title
Unbounded relayer-supplied `call_weight_witness` combined with silently-dropped `actual_weight` in `pallet-whitelist::clean_and_dispatch` allows free, near-max-block-weight griefing - (File: `substrate/frame/whitelist/src/lib.rs`)

### Summary
`pallet-whitelist`'s new deferred-dispatch feature (`dispatch_whitelisted_call` / `dispatch_whitelisted_call_with_preimage`) lets *any* signed account act as a fee-exempt "relayer" for a governance-deferred, already-whitelisted call. The relayer supplies `call_weight_witness`, which is only lower-bounded (`ensure!(call.get_dispatch_info().call_weight.all_lte(call_weight_witness), ...)`), never upper-bounded, and it directly feeds the extrinsic's pre-dispatch `#[pallet::weight(...)]` annotation. The dispatch result's `actual_weight` is derived purely by `.map()`-ing over the *inner* call's own `actual_weight`; if the inner call reports `actual_weight: None` (the common default for calls that don't do custom weight refunding), `clean_and_dispatch`/the dispatchable returns `actual_weight: None` for the whole outer extrinsic instead of falling back to the inner call's real declared weight (which is what `frame_support::dispatch::extract_actual_weight` is designed to do, and which other pallets such as `pallet-utility` explicitly use). With `actual_weight: None` and `Pays::No` (since the relayer is deliberately not charged), the block's weight-accounting mechanism uses the huge, attacker-chosen `call_weight_witness` as the "actual" consumed weight, and the transaction-payment mechanism charges nothing for it.

### Finding Description
Relevant code, `substrate/frame/whitelist/src/lib.rs`:

```rust
#[pallet::weight(
    T::WeightInfo::dispatch_whitelisted_call(*call_encoded_len)
        .saturating_add(*call_weight_witness)
)]
pub fn dispatch_whitelisted_call(
    origin: OriginFor<T>,
    call_hash: T::Hash,
    call_encoded_len: u32,
    call_weight_witness: Weight,
) -> DispatchResultWithPostInfo {
    let relayer = match T::DispatchWhitelistedOrigin::try_origin(origin) {
        Ok(_) if WhitelistedCall::<T>::contains_key(call_hash) => None,
        Ok(_) => { Self::defer_dispatch(call_hash)?; return Ok(Some(T::WeightInfo::defer_dispatch(0)).into()); },
        Err(dispatch_origin) => {
            Some(Self::ensure_signed_deferred_dispatch(dispatch_origin, call_hash)?)
        },
    };
    ...
    ensure!(
        call.get_dispatch_info().call_weight.all_lte(call_weight_witness),
        Error::<T>::InvalidCallWeightWitness
    );
    let pays_fee = if relayer.is_some() { Pays::No } else { Pays::Yes };
    let call_actual_weight = Self::clean_and_dispatch(call_hash, call);
    ...
    let actual_weight = call_actual_weight.map(|w| {
        w.saturating_add(T::WeightInfo::dispatch_whitelisted_call(call_encoded_len))
    });
    Ok(PostDispatchInfo { actual_weight, pays_fee })
}
```

and:

```rust
fn clean_and_dispatch(call_hash: T::Hash, call: <T as Config>::RuntimeCall) -> Option<Weight> {
    WhitelistedCall::<T>::remove(call_hash);
    T::Preimages::unrequest(&call_hash);
    DeferredDispatch::<T>::remove(call_hash);
    let result = call.dispatch(frame_system::Origin::<T>::Root.into());
    let call_actual_weight = match result {
        Ok(call_post_info) => call_post_info.actual_weight,
        Err(call_err) => call_err.post_info.actual_weight,
    };
    ...
    call_actual_weight
}
``` [1](#0-0) [2](#0-1) 

The broken invariant: **the outer extrinsic's `actual_weight` must be derived from the inner call's true weight, falling back to the inner call's declared pre-dispatch weight when the inner call reports `None`.** Instead, `None.map(...)` produces `None` for the whole extrinsic, which frame_system's executive interprets as "use the full pre-dispatch declared weight" for block-weight accounting — but that declared weight includes the attacker-controlled, unbounded `call_weight_witness`, not the inner call's real (small) weight. Other pallets performing similar "execute an inner call and refund weight" patterns (e.g. `pallet-utility`) explicitly call `extract_actual_weight` to avoid exactly this trap (confirmed via `grep_search` for `extract_actual_weight`, which shows `pallet-utility` uses it but `pallet-whitelist` does not) [3](#0-2) .

Existing guards do not stop this because:
- The only bound on `call_weight_witness` is a lower bound (`all_lte`); there is no upper bound tied to the actual/declared weight of the inner call.
- `Pays::No` is granted unconditionally to any successful relayer dispatch, regardless of how inflated `call_weight_witness` was.
- `ensure_signed_deferred_dispatch` only checks signed origin, deferred-entry existence/non-expiry, and whitelist status — it never re-validates that `call_weight_witness` is close to the call's real weight beyond the trivial lower bound.

### Impact Explanation
This falls under "public underpriced work that degrades block production" from the required impact gate: an unprivileged, ordinary signed account (no governance, validator, relayer, or admin privilege required) can consume close to the chain's entire per-block "Normal" dispatch-class weight budget in a single, fee-free transaction, whenever any governance-deferred-and-whitelisted call exists that reports `actual_weight: None` post-dispatch (a common, default case for simple calls). Repeating this (once per available deferred/whitelisted call, or by governance regularly using this new feature) stalls block production for legitimate transactions, a direct chain-availability impact.

### Likelihood Explanation
The precondition (a call has been deferred via `defer_dispatch` and is currently whitelisted) is a *normal, expected* outcome of this newly introduced feature's intended workflow — governance calls `dispatch_whitelisted_call`/`_with_preimage` while the call isn't yet whitelisted, which defers it; later, `whitelist_call` whitelists it; then any relayer can execute it. No collusion with governance or validators is needed — the relayer role is explicitly designed to be permissionless ("executed later by any signed origin"). The attacker only needs to observe on-chain deferred+whitelisted entries (public storage: `DeferredDispatch` and `WhitelistedCall`) and race to be the one who calls the dispatch with an inflated witness.

### Recommendation
- Bound `call_weight_witness` from above as well (e.g., require it to equal, not merely lower-bound, the actual `call.get_dispatch_info().call_weight`, or cap it strictly to that value), so a relayer cannot declare more weight than the call truly needs.
- In `clean_and_dispatch`/the calling dispatchable, use `frame_support::dispatch::extract_actual_weight(&result, &info)` (as `pallet-utility` does) to correctly fall back to the *inner call's own pre-dispatch weight* when its `actual_weight` is `None`, instead of propagating `None` for the whole extrinsic.
- Reconsider granting `Pays::No` unconditionally to the relayer path; at minimum ensure the weight actually charged/accounted for cannot exceed the inner call's real weight plus a bounded pallet overhead.

### Proof of Concept
1. Governance (via `DispatchWhitelistedOrigin`) calls `dispatch_whitelisted_call_with_preimage(call = System::remark{remark: vec![]})` before whitelisting it — this defers the call (`DeferredDispatch` entry created).
2. Governance later whitelists the hash via `whitelist_call`.
3. Attacker (any `Signed` account) calls:
```rust
Whitelist::dispatch_whitelisted_call_with_preimage(
    RuntimeOrigin::signed(attacker),
    Box::new(RuntimeCall::System(frame_system::Call::remark { remark: vec![] })),
)
```
   Since `System::remark`'s dispatch reports `actual_weight: None` by default, `clean_and_dispatch` returns `None`, `actual_weight = None.map(...) = None`, and `pays_fee = Pays::No`.
4. Because `actual_weight` is `None`, the extrinsic's pre-dispatch declared weight — `T::WeightInfo::dispatch_whitelisted_call_with_preimage(call_len).saturating_add(call.get_dispatch_info().call_weight)` — is used by `frame_system`'s executive for block weight accounting, but since `call` here is just `System::remark{}` its own declared weight is small, so to actually grief the block the attacker must additionally note there is **no explicit witness parameter on the `_with_preimage` variant** (it derives weight directly from the call), so the more direct exploit uses `dispatch_whitelisted_call` (the hash+witness variant) where the attacker fully controls `call_weight_witness` independent of the (small) real inner call:
```rust
Whitelist::dispatch_whitelisted_call(
    RuntimeOrigin::signed(attacker),
    call_hash,               // hash of a small whitelisted+deferred call, e.g. System::remark
    call_encoded_len,
    Weight::from_parts(RuntimeBlockWeights::get().max_block.ref_time() * 9 / 10, u64::MAX), // near-max witness
)
```
   This passes the lower-bound `ensure!(call_weight.all_lte(call_weight_witness))` trivially (since witness ≫ real weight), executes for free (`Pays::No`), and because the inner call's `actual_weight` is `None`, the outer extrinsic's `actual_weight` collapses to `None`, causing the executive to charge the full inflated pre-dispatch weight (the huge witness) against the block's weight budget at zero fee.

Note: I was not able to execute this scenario in a live test harness (no code execution available in this environment); the above is derived directly from reading `substrate/frame/whitelist/src/lib.rs` and comparing the weight-refund pattern to `pallet-utility`'s use of `extract_actual_weight`. A background Devin agent with test/build tooling should write a concrete `#[test]` in `substrate/frame/whitelist/src/tests.rs` reproducing the free, inflated-weight relayer dispatch to confirm the actual on-chain weight accounting behavior before treating this as fully confirmed.

### Citations

**File:** substrate/frame/whitelist/src/lib.rs (L200-248)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight(
			T::WeightInfo::dispatch_whitelisted_call(*call_encoded_len)
				.saturating_add(*call_weight_witness)
		)]
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

**File:** substrate/frame/whitelist/src/lib.rs (L364-382)
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
}
```

**File:** substrate/frame/utility/src/lib.rs (L1-1)
```rust
// This file is part of Substrate.
```
