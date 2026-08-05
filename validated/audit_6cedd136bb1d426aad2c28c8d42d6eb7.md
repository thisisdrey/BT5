This claim is fully verified against the code. The value transfer at `substrate/frame/revive/src/exec.rs` lines 1375-1387 is unconditional for any non-delegate call, while the account/consumer bookkeeping at lines 1389-1405 only applies when `precompile.has_contract_info()` is true. All builtin precompiles (`ecrecover.rs`, `sha256.rs`, `ripemd160.rs`, `identity.rs`, `modexp.rs`, `bn128.rs`, `blake2f.rs`, `system.rs`, etc.) declare `HAS_CONTRACT_INFO: bool = false`, and the test at `substrate/frame/revive/src/tests/pvm.rs` lines 4751-4781 confirms balance simply accumulates at these fixed addresses with no consumption/refund. The only existing guard against value-with-call is for `RUNTIME_PALLETS_ADDR` in `substrate/frame/revive/src/evm/call.rs` lines 143-155, which does not cover precompile addresses. This matches the "permanent user-fund lock" impact category, is deterministic/reproducible, and requires no privileged actor.

Audit Report

## Title
Native/EVM value sent with `CALL` to a stateless (`HAS_CONTRACT_INFO = false`) `pallet-revive` precompile is permanently locked - ([File: substrate/frame/revive/src/exec.rs])

## Summary
`pallet-revive`'s shared call-frame execution path (`Stack::run`'s `do_transaction` closure) unconditionally transfers native/EVM `value` to the destination address for any non-delegate call before invoking the callee, regardless of whether that destination is a stateless precompile. Builtin precompiles such as `ECRecover`, `Sha256`, `Ripemd160`, `Identity`, `Modexp`, `Bn128Add/Mul/Pairing`, and `Blake2F` declare `HAS_CONTRACT_INFO = false` and never consume, forward, or refund received value, so any value sent to their fixed addresses is permanently and irrecoverably locked since these addresses have no private key, no `ContractInfo`, and no code path capable of moving the balance out.

## Finding Description
In `Stack::run`'s transaction closure, the balance transfer is performed unconditionally for every non-delegate frame via `Self::transfer_from_origin(...)` at `substrate/frame/revive/src/exec.rs` lines 1375-1387, before the callee executes. Immediately after, account/consumer bookkeeping (minting the existential deposit and incrementing consumers) is only performed when `precompile.has_contract_info()` is true, at lines 1389-1405. For precompiles with `HAS_CONTRACT_INFO = false` — which includes all builtin Ethereum-compatibility precompiles (`ecrecover.rs`, `sha256.rs`, `ripemd160.rs`, `identity.rs`, `modexp.rs`, `bn128.rs`, `blake2f.rs`) and `System` (`system.rs`) — no such bookkeeping occurs, and none of these precompiles' `call()` implementations reference or move the transferred `value` anywhere.

The `Precompile` trait at `substrate/frame/revive/src/precompiles.rs` (lines 176-214) explicitly documents that `HAS_CONTRACT_INFO = false` means "No account or any other state will be created for the address," yet `transfer_from_origin` still unconditionally credits the balance to the `AccountId` derived from that fixed `H160`, regardless of this flag.

The only existing guard against calling a special/fixed address with nonzero value is in `substrate/frame/revive/src/evm/call.rs` lines 143-155, which rejects nonzero `value` only for `RUNTIME_PALLETS_ADDR`. This guard does not extend to any of the `HAS_CONTRACT_INFO = false` precompile addresses (`0x01`-`0x09`, etc.), so the exploit path is unguarded.

This is confirmed by the repository's own test in `substrate/frame/revive/src/tests/pvm.rs` (lines 4751-4781), which calls each classic Ethereum precompile with `value = 100` and asserts `Pallet::<Test>::evm_balance(&precompile_addr) == U256::from(100)` afterward — documenting rather than preventing the fund lock. Since these addresses are protocol-defined constants with no corresponding keypair, no signer can ever originate an extrinsic from that account, there is no contract code or `terminate()`/self-destruct path to redirect the balance, and the precompile logic itself (hashing, EC recovery, etc.) has no notion of `value`.

## Impact Explanation
Any account that sends native/EVM value together with a `CALL`/`eth_call` to a fixed precompile address with `HAS_CONTRACT_INFO = false` permanently loses that value with no recovery path — this is a permanent user-fund lock inside `pallet-revive`, matching the "permanent user-fund or bridge-state lock" category in the Polkadot SDK impact gate. The corrupted value is the native balance credited to the `AccountId` derived from the fixed precompile `H160` address, which becomes permanently stranded.

## Likelihood Explanation
The bug is deterministic and 100% reproducible on every call with `value > 0` targeting any `HAS_CONTRACT_INFO = false` precompile, requiring only an unprivileged `eth_call`/PVM `CALL` from any EOA or contract. Precompile addresses `0x01`-`0x09` are canonical Ethereum precompile addresses referenced by widely used EVM tooling and ported contracts, some of which may naively attach value to such calls (e.g., helper libraries or contracts assuming standard EVM precompile semantics). No malicious validator, collator, relayer, governance action, or leaked key is required.

## Recommendation
Reject calls carrying non-zero `value` when the destination is a precompile with `HAS_CONTRACT_INFO = false`, mirroring the existing guard for `RUNTIME_PALLETS_ADDR` in `substrate/frame/revive/src/evm/call.rs`. Alternatively, have `transfer_from_origin` or the shared call path in `exec.rs` explicitly branch on `has_contract_info()` before performing the transfer, refusing or refunding value sent to stateless precompiles, consistent with the documented "no account or any other state will be created for the address" semantics in `precompiles.rs`. Update `pure_precompile_works` and any similar tests to assert reversion or refund once fixed.

## Proof of Concept
1. On a `pallet-revive`-enabled chain, deploy any contract (e.g., via `bare_instantiate`).
2. From that contract (or via `eth_call`), perform a low-level `CALL` with `value = N > 0` to `H160::from_low_u64_be(1)` (the `ECRecover` precompile address) with valid ABI-encoded `ECRecover` input.
3. Observe the call succeeds and returns the expected output, exactly as exercised in `substrate/frame/revive/src/tests/pvm.rs` lines 4751-4781, and `Pallet::<Test>::evm_balance(&precompile_addr)` equals `N`.
4. Confirm there is no private key, `ContractInfo`, or code path in the `ECRecover`/`System` precompile logic that can move the `N` balance out of that address — the funds are permanently locked. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1375-1405)
```rust
			// Every non delegate call or instantiate also optionally transfers the balance.
			// If it is a delegate call, then we've already transferred tokens in the
			// last non-delegate frame.
			if frame.delegate.is_none() {
				Self::transfer_from_origin(
					&self.origin,
					&caller,
					account_id,
					frame.value_transferred,
					&mut frame.frame_meter,
					self.exec_config,
				)?;
			}

			// We need to make sure that the pre-compiles contract exist before executing it.
			// A few more conditionals:
			// 	- Only contracts with extended API (has_contract_info) are guaranteed to have an
			//    account.
			//  - Only when not delegate calling we are executing in the context of the pre-compile.
			//    Pre-compiles itself cannot delegate call.
			if let Some(precompile) = executable.as_precompile() &&
				precompile.has_contract_info() &&
				frame.delegate.is_none() &&
				!<System<T>>::account_exists(account_id)
			{
				// prefix matching pre-compiles cannot have a contract info
				// hence we only mint once per pre-compile
				T::Currency::mint_into(account_id, T::Currency::minimum_balance())?;
				// make sure the pre-compile does not destroy its account by accident
				<System<T>>::inc_consumers(account_id)?;
			}
```

**File:** substrate/frame/revive/src/precompiles.rs (L176-214)
```rust
	/// Defines whether this pre-compile needs a contract info data structure in storage.
	///
	/// Enabling it unlocks more APIs for the pre-compile to use. Only pre-compiles with a
	/// fixed matcher can set this to true. This is enforced at compile time. Reason is that
	/// contract info is per address and not per pre-compile. Too many contract info structures
	/// and accounts would be created otherwise.
	///
	/// # When set to **true**
	///
	/// - An account will be created at the pre-compiles address when it is called for the first
	///   time. The ed is minted.
	/// - Contract info data structure will be created in storage on first call.
	/// - Only `call_with_info` should be implemented. `call` is never called.
	///
	/// # When set to **false**
	///
	/// - No account or any other state will be created for the address.
	/// - Only `call` should be implemented. `call_with_info` is never called.
	///
	/// # What to use
	///
	/// Should be set to false if the additional functionality is not needed. A pre-compile with
	/// contract info will incur both a storage read and write to its contract metadata when called.
	///
	/// The contract info enables additional functionality:
	/// - Storage deposits: Collect deposits from the origin rather than the caller. This makes it
	///   easier for contracts to interact with the pre-compile as deposits
	/// 	are paid by the transaction signer (just like gas). It also makes refunding easier.
	/// - Contract storage: You can use the contracts key value child trie storage instead of
	///   providing your own state.
	/// 	The contract storage automatically takes care of deposits.
	/// 	Providing your own storage and using pallet_revive to collect deposits is also possible,
	/// though.
	/// - Instantitation: Contract instantiation requires the instantiator to have an account. This
	/// 	is because its nonce is used to derive the new contracts account id and child trie id.
	///
	/// Have a look at [`ExtWithInfo`] to learn about the additional APIs that a contract info
	/// unlocks.
	const HAS_CONTRACT_INFO: bool;
```

**File:** substrate/frame/revive/src/evm/call.rs (L143-155)
```rust
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
```

**File:** substrate/frame/revive/src/tests/pvm.rs (L4751-4781)
```rust
	for (description, precompile_addr, input, output) in cases {
		let (code, _code_hash) = compile_module("call_and_return").unwrap();
		ExtBuilder::default().build().execute_with(|| {
			let _ = <Test as Config>::Currency::set_balance(&ALICE, 100_000_000_000);
			let Contract { addr, .. } = builder::bare_instantiate(Code::Upload(code))
				.native_value(1_000)
				.build_and_unwrap_contract();

			let result = builder::bare_call(addr)
				.data(
					(&precompile_addr, 100u64)
						.encode()
						.into_iter()
						.chain(input)
						.collect::<Vec<_>>(),
				)
				.build_and_unwrap_result();

			assert_eq!(
				Pallet::<Test>::evm_balance(&precompile_addr),
				U256::from(100),
				"{description}: unexpected balance"
			);
			assert_eq!(
				alloy_core::hex::encode(result.data),
				alloy_core::hex::encode(output),
				"{description} Unexpected output for precompile: {precompile_addr:?}",
			);
			assert_eq!(result.flags, ReturnFlags::empty());
		});
	}
```
