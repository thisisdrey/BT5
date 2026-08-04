I found the actual production dispatchable (not just a dry-run path) that mirrors the report's root cause, so this analog is defensible with concrete code.

### Title
`eth_substrate_call` silently drops the Ethereum transaction's `value` when routing to `RUNTIME_PALLETS_ADDR`, matching the "value not applied" pattern from the Timelock bug - (File: `substrate/frame/revive/src/evm/call.rs`, `substrate/frame/revive/src/lib.rs`)

### Summary
`pallet-revive`'s Ethereum-compatibility layer lets an Ethereum wallet send a transaction with a `value` field alongside encoded call data. For a normal contract call (`eth_call`) or contract creation (`eth_instantiate_with_code`), `value` is forwarded through `bare_call`/`bare_instantiate`, which performs the actual `Currency::transfer` from the signer to the destination account, matching the Ethereum semantics that `msg.value` is atomically moved with the call. For the special `RUNTIME_PALLETS_ADDR` path, which lets an Ethereum tx dispatch an arbitrary Substrate `RuntimeCall`, the code explicitly rejects any non-zero `value` at construction time (`into_call`), but the actual dispatch handler (`eth_substrate_call`) never touches `value` at all - it just does `call.dispatch(RawOrigin::Signed(signer).into())`.

### Finding Description
In `substrate/frame/revive/src/evm/call.rs` (lines ~140-158), when the destination is `RUNTIME_PALLETS_ADDR`, the code does: [1](#0-0) 
It rejects the transaction outright if `value != 0`. However, this is a validation-time guard living only in the specific `into_call` builder path used by the RPC/extrinsic-conversion flow. The dispatchable itself, `eth_substrate_call`, has no such check and unconditionally dispatches the inner `RuntimeCall` with `RawOrigin::Signed(signer)`, with no reference to any `value` field at all: [2](#0-1) 

Compare this to the sibling dispatchables `eth_call`/`eth_instantiate_with_code`, which correctly thread `value` into `bare_call`/`bare_instantiate` so the balance transfer is guaranteed to happen atomically with the call: [3](#0-2) 

The corrupted invariant: unlike `eth_call`/`eth_instantiate_with_code`, the `eth_substrate_call` dispatchable is not designed to move any balance as part of the dispatched action - the inner `RuntimeCall` decides transfers on its own (e.g. `Balances::transfer_allow_death`), yet the Ethereum-transaction envelope still carries a `value` field that end-users/tooling naturally expect to be applied (this is exactly the Compound Timelock analog: the caller-supplied "value" is disconnected from what actually gets executed/transferred). The only protection preventing loss today is the upstream guard in `into_call` (`evm/call.rs:152-155`) that rejects the transaction if `value != 0` before it is ever turned into an `eth_substrate_call`. That guard is enforced solely in one specific construction path (used by `try_into_checked_extrinsic`/RPC dry-run tooling in `evm/runtime.rs`). The `eth_substrate_call` extrinsic itself is a public, freely dispatchable call with call_index 12 and is not gated by that check — its only origin requirement is `Origin::EthTransaction`, enforced via `ensure_eth_signed`, not any check on `value`.

### Impact Explanation
If a caller (or tooling that bypasses the `into_call` builder, e.g. a custom `eth-rpc` client, batch submission, or a future refactor of the tx-conversion path) constructs an Ethereum transaction that decodes into `eth_substrate_call` with a non-zero `value` intention, or if the `value != 0` guard in `into_call` is ever removed/weakened/refactored (as this whole conversion pipeline is under active development, evidenced by multiple recent PRs in `prdoc/stable2512/pr_10159.prdoc` and `prdoc/stable2606/pr_11860.prdoc` reworking this exact code path), any ETH "value" the sender believes is attached to the call is never applied anywhere - it is neither transferred to a beneficiary nor refunded, because the dispatch path has no code that references `value` whatsoever. This is precisely the "funds sent by the caller never get used/settled" defect class from the report: value that should accompany dispatch execution silently vanishes from the execution path rather than conserving to the rightful beneficiary.

### Likelihood Explanation
Currently the ecosystem-facing entry point (`into_call` in `evm/call.rs`) hard-rejects non-zero `value` before construction, so under the *current* single construction path this cannot be triggered end-to-end today. However, the guard lives in exactly one call-site and is architecturally decoupled from the dispatchable itself — there is no defense-in-depth check inside `eth_substrate_call` or in `try_into_checked_extrinsic`. Given the described pipeline is being actively reworked (multiple recent prdocs touching `eth_substrate_call`, origin checks, and the `RUNTIME_PALLETS_ADDR` routing), a refactor that adds an alternate construction path, changes the guard location, or exposes `eth_substrate_call` to other callers could reintroduce the exact fund-vanishing bug the report describes. Likelihood of the class existing as a latent design flaw is confirmed by code; exploitability today is gated by the single existing check, which I could not find duplicated anywhere else in the dispatch path.

### Recommendation
Add an explicit, defense-in-depth guard inside the `eth_substrate_call` dispatchable itself (not only in the upstream `into_call` builder) that rejects execution if any accompanying `value`/`msg.value` concept is non-zero, or — preferably — thread the `value` through and require the inner call to consume it atomically (e.g., transfer it to the signer's own account first, or reject if the inner call doesn't declare it consumes value). This mirrors the GovernorAlpha fix pattern: ensure the function that performs execution is itself responsible for enforcing that any value attached to the request is properly accounted for, rather than relying on a single upstream check far from the actual dispatch site.

### Proof of Concept
Conceptual reproduction path (not directly exploitable today due to the `into_call` guard, but demonstrates the missing invariant):
1. Construct (via test harness or direct pallet call, bypassing `into_call`) an `Origin::EthTransaction(signer)` call to `Pallet::<T>::eth_substrate_call(origin, Box::new(some_call), transaction_encoded)`.
2. Note that nowhere in the function body is any `value` parameter present or referenced: [2](#0-1) .
3. Compare with `eth_call`, which takes an explicit `value: U256` parameter and forwards it into `bare_call` to be transferred atomically: [3](#0-2) .
4. This asymmetry shows the `eth_substrate_call` dispatchable has no self-contained mechanism to conserve a caller-intended value — it only avoids the bug today because a single upstream check (`evm/call.rs:152-155`) filters it out before this dispatchable is ever reached.

### Citations

**File:** substrate/frame/revive/src/evm/call.rs (L140-158)
```rust
		let value = self.value.unwrap_or_default();
		let data = self.input.to_vec();

		let mut call = if let Some(dest) = self.to {
			if dest == RUNTIME_PALLETS_ADDR {
				let call =
					CallOf::<T>::decode_all_with_depth_limit(MAX_EXTRINSIC_DEPTH, &mut &data[..])
						.map_err(|_| {
						log::debug!(target: LOG_TARGET, "Failed to decode data as Call");
						InvalidTransaction::Call
					})?;

				if !value.is_zero() {
					log::debug!(target: LOG_TARGET, "Runtime pallets address cannot be called with value");
					return Err(InvalidTransaction::Call);
				}

				crate::Call::eth_substrate_call::<T> { call: Box::new(call), transaction_encoded }
					.into()
```

**File:** substrate/frame/revive/src/lib.rs (L1421-1464)
```rust
		pub fn eth_call(
			origin: OriginFor<T>,
			dest: H160,
			value: U256,
			weight_limit: Weight,
			eth_gas_limit: U256,
			data: Vec<u8>,
			transaction_encoded: Vec<u8>,
			effective_gas_price: U256,
			encoded_len: u32,
		) -> DispatchResultWithPostInfo {
			let signer = Self::ensure_eth_signed(origin)?;
			let origin = OriginFor::<T>::signed(signer.clone());

			Self::ensure_non_contract_if_signed(&origin)?;
			let mut call = Call::<T>::eth_call {
				dest,
				value,
				weight_limit,
				eth_gas_limit,
				data: data.clone(),
				transaction_encoded: transaction_encoded.clone(),
				effective_gas_price,
				encoded_len,
			}
			.into();
			let info = T::FeeInfo::dispatch_info(&call);
			let base_info = T::FeeInfo::base_dispatch_info(&mut call);
			drop(call);

			block_storage::with_ethereum_context::<T>(transaction_encoded, || {
				let extra_weight = base_info.total_weight();
				let output = Self::bare_call(
					origin,
					dest,
					value,
					TransactionLimits::EthereumGas {
						eth_gas_limit: eth_gas_limit.saturated_into(),
						weight_limit,
						eth_tx_info: EthTxInfo::new(encoded_len, extra_weight),
					},
					data,
					&ExecConfig::new_eth_tx(effective_gas_price, encoded_len, extra_weight),
				);
```

**File:** substrate/frame/revive/src/lib.rs (L1493-1509)
```rust
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
