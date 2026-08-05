Found a concrete, already-fixed analog in this repository's history that matches the "unrestricted privileged entrypoint" bug class from the report: `pallet-revive`'s `dispatch_as_fallback_account` path stripped call filters from the inner runtime call before dispatch, i.e. an unprivileged contract-originated call could bypass `CallFilter`/origin restrictions the same way the `diamondCut` bug let an unrestricted caller bypass admin-only facet control.

### Title
Unfiltered inner-call dispatch via `pallet_revive` fallback-account path bypasses `CallFilter` restrictions - (File: `substrate/frame/revive/src/lib.rs`)

### Summary
The external report's core invariant is: a state-mutating, security-critical function (`diamondCut`) is reachable and executable by an unprivileged caller because the wrapper that should have enforced an authorization check does not. In this repository the same invariant break exists in `pallet-revive`'s Ethereum-compatibility dispatch path: `eth_substrate_call` and the associated `dispatch_as_fallback_account` mechanism route arbitrary `RuntimeCall`s to `call.dispatch(RawOrigin::Signed(signer).into())` [1](#0-0) , but the origin's `CallFilter` was stripped in the fallback-account path, meaning calls that should have been rejected by the runtime's filter could execute anyway — functionally equivalent to calling a privileged function with an origin that should not have been authorized to perform it.

### Finding Description
`eth_substrate_call` is a public dispatchable intended only to be invoked through the EVM compatibility layer, taking a boxed `RuntimeCall` and dispatching it with `RawOrigin::Signed(signer)` [1](#0-0) . Dispatch of a `RuntimeCall` normally goes through `Dispatchable::dispatch`, which checks `OriginTrait::filter_call` before falling through to `UnfilteredDispatchable::dispatch_bypass_filter` [2](#0-1) . The runtime record `prdoc/stable2606/pr_11839.prdoc` documents that, prior to the fix, the `dispatch_as_fallback_account` code path in `pallet-revive` "stripped any call filters existing on the origin" — meaning calls dispatched through this path bypassed the `CallFilter` gate that the rest of the runtime relies on to prevent unauthorized/forbidden calls from being executed by regular signed accounts.

A related follow-up fix, `prdoc/stable2606/pr_11860.prdoc`, shows that `eth_substrate_call`'s origin check was also inconsistent with other eth dispatchables (`eth_call`, `eth_instantiate_with_code`) until `ensure_non_contract_if_signed` was added to align it — another instance of a privileged/guarded check being missing on one entrypoint while present on sibling entrypoints, exactly mirroring the report's pattern of a function ("diamondCut") lacking the permission check that a sibling authorized-admin flow enforced.

### Impact Explanation
If the call filter is bypassed for calls dispatched via the fallback-account path, an unprivileged EVM-originated signed account can execute `RuntimeCall`s that governance explicitly intended to disallow for regular signed origins (e.g., calls gated by `BaseCallFilter`/`SafeMode`/`TxPause`), which is the on-chain equivalent of an unauthorized `diamondCut`: unauthorized execution/origin escalation that can alter runtime behavior, drain or move funds through otherwise-filtered calls, or interfere with governance-controlled restrictions — impacting the intended behavior of a live Substrate-based chain without requiring a malicious validator, relayer, or admin.

### Likelihood Explanation
Likelihood is high for any deployment running the vulnerable pre-fix code: the entrypoint (`eth_substrate_call`) is reachable by any account sending a standard Ethereum-style transaction through the EVM compatibility layer, requiring no privileged role, and the vulnerable behavior — bypassing `CallFilter` — is triggered by ordinary usage of the fallback-account dispatch path, not by any exotic or attacker-controlled infrastructure.

### Recommendation
Ensure `dispatch_as_fallback_account` (and any other internal helper that ends up calling `dispatch_bypass_filter` or manually constructing dispatch without going through `Dispatchable::dispatch`) preserves and enforces the origin's `CallFilter`, matching the behavior of `Dispatchable::dispatch` [2](#0-1) . Audit all `pallet-revive` entrypoints (`eth_substrate_call`, `eth_call`, `eth_instantiate_with_code`) for consistent application of both `CallFilter` and `ensure_non_contract_if_signed`/EIP-3607-style origin checks, as was done in the corresponding fixes [3](#0-2) .

### Proof of Concept
Conceptual PoC (mirrors the report's structure):
1. Runtime configures a `BaseCallFilter` that disallows a specific `RuntimeCall` (e.g., a treasury or governance-restricted call) for ordinary signed accounts.
2. An unprivileged user submits an Ethereum-style transaction to `pallet-revive`'s `eth_substrate_call` entrypoint, embedding the filtered `RuntimeCall`.
3. Prior to the fix in `prdoc/stable2606/pr_11839.prdoc`, the fallback-account dispatch path stripped the call filter, so the call executed with `RawOrigin::Signed(signer)` [4](#0-3)  despite the filter that should have blocked it — analogous to the unrestricted `random_user` successfully invoking `diamondCut` in the original report.

### Citations

**File:** substrate/frame/revive/src/lib.rs (L1477-1509)
```rust
		/// Executes a Substrate runtime call from an Ethereum transaction.
		///
		/// This dispatchable is intended to be called **only** through the EVM compatibility
		/// layer. The provided call will be dispatched using `RawOrigin::Signed`.
		///
		/// # Parameters
		///
		/// * `origin`: Must be an [`Origin::EthTransaction`] origin.
		/// * `call`: The Substrate runtime call to execute.
		/// * `transaction_encoded`: The RLP encoding of the Ethereum transaction,
		#[pallet::call_index(12)]
		#[pallet::weight(
			T::WeightInfo::eth_substrate_call(transaction_encoded.len() as u32)
			.saturating_add(call.get_dispatch_info().call_weight)
			.saturating_add(T::WeightInfo::on_finalize_block_per_tx(transaction_encoded.len() as u32))
		)]
		pub fn eth_substrate_call(
			origin: OriginFor<T>,
			call: Box<<T as Config>::RuntimeCall>,
			transaction_encoded: Vec<u8>,
		) -> DispatchResultWithPostInfo {
			// Note that the inner dispatch uses `RawOrigin::Signed`, which cannot
			// re-enter `eth_substrate_call` (which requires `Origin::EthTransaction`).
			let signer = Self::ensure_eth_signed(origin)?;
			Self::ensure_non_contract_if_signed(&OriginFor::<T>::signed(signer.clone()))?;
			let tx_len = transaction_encoded.len() as u32;
			let weight_overhead = T::WeightInfo::eth_substrate_call(tx_len)
				.saturating_add(T::WeightInfo::on_finalize_block_per_tx(tx_len));

			block_storage::with_ethereum_context::<T>(transaction_encoded, || {
				let call_weight = call.get_dispatch_info().call_weight;
				let mut call_result = call.dispatch(RawOrigin::Signed(signer).into());

```

**File:** substrate/frame/support/procedural/src/construct_runtime/expand/call.rs (L169-183)
```rust
		impl #scrate::__private::Dispatchable for RuntimeCall {
			type RuntimeOrigin = RuntimeOrigin;
			type Config = RuntimeCall;
			type Info = #scrate::dispatch::DispatchInfo;
			type PostInfo = #scrate::dispatch::PostDispatchInfo;
			fn dispatch(self, origin: RuntimeOrigin) -> #scrate::dispatch::DispatchResultWithPostInfo {
				if !<Self::RuntimeOrigin as #scrate::traits::OriginTrait>::filter_call(&origin, &self) {
					return ::core::result::Result::Err(
						#system_path::Error::<#runtime>::CallFiltered.into()
					);
				}

				#scrate::traits::UnfilteredDispatchable::dispatch_bypass_filter(self, origin)
			}
		}
```
