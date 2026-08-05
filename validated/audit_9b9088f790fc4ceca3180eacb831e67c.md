The claim is well-supported by the code I inspected.

Audit Report

## Title
Native/EVM value sent with `CALL` to a stateless (`HAS_CONTRACT_INFO = false`) `pallet-revive` precompile is permanently locked - ([File: substrate/frame/revive/src/exec.rs])

## Summary
`Stack::run` in `pallet-revive` unconditionally executes `transfer_from_origin` for every non-delegate call frame before invoking the callee, crediting `frame.value_transferred` to the destination account regardless of whether that destination is a stateless precompile. [1](#0-0)  Only precompiles with `has_contract_info() == true` receive an account/existential-deposit and consumer bookkeeping; precompiles declared with `const HAS_CONTRACT_INFO: bool = false` (e.g. all the built-in Ethereum precompiles) never get such handling and their `call()` bodies never touch or forward the transferred value. [2](#0-1) 

## Finding Description
The `Precompile` trait documents that when `HAS_CONTRACT_INFO = false`, "No account or any other state will be created for the address," yet the balance transfer via `transfer_from_origin` still runs unconditionally before this branch. [3](#0-2)  Built-in precompiles such as `System` explicitly set `HAS_CONTRACT_INFO = false`. [4](#0-3)  The addresses `0x01`–`0x09` (`ECRecover`, `Sha256`, `Ripemd160`, `Identity`, `Modexp`, `Bn128Add/Mul/Pairing`, `Blake2F`) are fixed protocol addresses with no private key and no `ContractInfo`/code that could move funds, so any balance credited there via `transfer_from_origin` cannot be swept back out through any code path available in `exec.rs` or the precompile `call()` implementations.

A guard for exactly this class of issue already exists elsewhere in the codebase for the `RUNTIME_PALLETS_ADDR`, which rejects non-zero `value` at call-construction time, confirming the project recognizes value-to-non-account destinations as something that must be explicitly blocked — but this guard was never extended to stateless precompiles. [5](#0-4) 

The repository's own test `pure_precompile_works` sends `value = 100` in a `CALL` to each of the nine classic precompile addresses and asserts that `Pallet::<Test>::evm_balance(&precompile_addr)` accumulates the value with no refund or rejection, directly documenting rather than guarding against the bug. [6](#0-5) 

## Impact Explanation
Any value sent with a `CALL`/`eth_call` to a `HAS_CONTRACT_INFO = false` precompile address becomes permanently unrecoverable native balance, matching the "permanent user-fund lock" impact category. The corrupted value is the native balance credited to the `AccountId` mapped from the fixed precompile `H160` address (e.g., the account for `0x...01`), which has no owning key, no contract code, and no `terminate()`/self-destruct path to redirect funds.

## Likelihood Explanation
The bug is deterministic and reproducible on every call with `value > 0` targeting any of these fixed low addresses; no privileged actor, validator, or compromised infrastructure is required — only a normal `eth_call`/PVM `CALL`, which matches the common EVM tooling pattern of referencing precompile addresses `0x01`–`0x09` directly.

## Recommendation
Reject calls carrying non-zero `value` when the destination is a precompile with `HAS_CONTRACT_INFO = false`, either in `Stack::run` (branch on `has_contract_info()` before invoking `transfer_from_origin`) or at call-construction time analogous to the existing `RUNTIME_PALLETS_ADDR` guard in `substrate/frame/revive/src/evm/call.rs`. Update `pure_precompile_works` to assert the value-carrying call reverts (or is refunded) once fixed.

## Proof of Concept
1. Instantiate a contract and call `builder::bare_call` targeting a fixed precompile address (e.g., `H160::from_low_u64_be(1)` for `ECRecover`) with `value = 100` and valid ABI-encoded input, as done in `pure_precompile_works`.
2. Observe the call succeeds, returns the expected precompile output, and `Pallet::<Test>::evm_balance(&precompile_addr)` equals `100`.
3. Confirm there is no code path (no key, no `ContractInfo`, no precompile logic) that can move this balance out of the precompile's account — the value is permanently locked. [7](#0-6)

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1375-1387)
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
```

**File:** substrate/frame/revive/src/exec.rs (L1389-1405)
```rust
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

**File:** substrate/frame/revive/src/precompiles/builtin/system.rs (L31-38)
```rust
pub struct System<T>(PhantomData<T>);

impl<T: Config> BuiltinPrecompile for System<T> {
	type T = T;
	type Interface = ISystem::ISystemCalls;
	const MATCHER: BuiltinAddressMatcher =
		BuiltinAddressMatcher::Fixed(NonZero::new(0x900).unwrap());
	const HAS_CONTRACT_INFO: bool = false;
```

**File:** substrate/frame/revive/src/evm/call.rs (L143-156)
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

**File:** substrate/frame/revive/src/tests/pvm.rs (L4690-4752)
```rust
#[test]
fn pure_precompile_works() {
	use hex_literal::hex;

	let cases = vec![
		(
			"ECRecover",
			H160::from_low_u64_be(1),
			hex!("18c547e4f7b0f325ad1e56f57e26c745b09a3e503d86e00e5255ff7f715d3d1c000000000000000000000000000000000000000000000000000000000000001c73b1693892219d736caba55bdb67216e485557ea6b6af75f37096c9aa6a5a75feeb940b1d03b21e36b0e47e79769f095fe2ab855bd91e3a38756b7d75a9c4549").to_vec(),
			hex!("000000000000000000000000a94f5374fce5edbc8e2a8697c15331677e6ebf0b").to_vec(),
		),
		(
			"Sha256",
			H160::from_low_u64_be(2),
			hex!("ec07171c4f0f0e2b").to_vec(),
			hex!("d0591ea667763c69a5f5a3bae657368ea63318b2c9c8349cccaf507e3cbd7c7a").to_vec(),
		),
		(
			"Ripemd160",
			H160::from_low_u64_be(3),
			hex!("ec07171c4f0f0e2b").to_vec(),
			hex!("000000000000000000000000a9c5ebaf7589fd8acfd542c3a008956de84fbeb7").to_vec(),
		),
		(
			"Identity",
			H160::from_low_u64_be(4),
			[42u8; 128].to_vec(),
			[42u8; 128].to_vec(),
		),
		(
			"Modexp",
			H160::from_low_u64_be(5),
			hex!("00000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000020000000000000000000000000000000000000000000000000000000000000002003fffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2efffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2f").to_vec(),
			hex!("0000000000000000000000000000000000000000000000000000000000000001").to_vec(),
		),
		(
			"Bn128Add",
			H160::from_low_u64_be(6),
			hex!("18b18acfb4c2c30276db5411368e7185b311dd124691610c5d3b74034e093dc9063c909c4720840cb5134cb9f59fa749755796819658d32efc0d288198f3726607c2b7f58a84bd6145f00c9c2bc0bb1a187f20ff2c92963a88019e7c6a014eed06614e20c147e940f2d70da3f74c9a17df361706a4485c742bd6788478fa17d7").to_vec(),
			hex!("2243525c5efd4b9c3d3c45ac0ca3fe4dd85e830a4ce6b65fa1eeaee202839703301d1d33be6da8e509df21cc35964723180eed7532537db9ae5e7d48f195c915").to_vec(),
		),
		(
			"Bn128Mul",
			H160::from_low_u64_be(7),
			hex!("2bd3e6d0f3b142924f5ca7b49ce5b9d54c4703d7ae5648e61d02268b1a0a9fb721611ce0a6af85915e2f1d70300909ce2e49dfad4a4619c8390cae66cefdb20400000000000000000000000000000000000000000000000011138ce750fa15c2").to_vec(),
			hex!("070a8d6a982153cae4be29d434e8faef8a47b274a053f5a4ee2a6c9c13c31e5c031b8ce914eba3a9ffb989f9cdd5b0f01943074bf4f0f315690ec3cec6981afc").to_vec(),
		),
		(
			"Bn128Pairing",
			H160::from_low_u64_be(8),
			hex!("1c76476f4def4bb94541d57ebba1193381ffa7aa76ada664dd31c16024c43f593034dd2920f673e204fee2811c678745fc819b55d3e9d294e45c9b03a76aef41209dd15ebff5d46c4bd888e51a93cf99a7329636c63514396b4a452003a35bf704bf11ca01483bfa8b34b43561848d28905960114c8ac04049af4b6315a416782bb8324af6cfc93537a2ad1a445cfd0ca2a71acd7ac41fadbf933c2a51be344d120a2a4cf30c1bf9845f20c6fe39e07ea2cce61f0c9bb048165fe5e4de877550111e129f1cf1097710d41c4ac70fcdfa5ba2023c6ff1cbeac322de49d1b6df7c2032c61a830e3c17286de9462bf242fca2883585b93870a73853face6a6bf411198e9393920d483a7260bfb731fb5d25f1aa493335a9e71297e485b7aef312c21800deef121f1e76426a00665e5c4479674322d4f75edadd46debd5cd992f6ed090689d0585ff075ec9e99ad690c3395bc4b313370b38ef355acdadcd122975b12c85ea5db8c6deb4aab71808dcb408fe3d1e7690c43d37b4ce6cc0166fa7daa").to_vec(),
			hex!("0000000000000000000000000000000000000000000000000000000000000001").to_vec(),
		),
		(
			"Blake2F",
			H160::from_low_u64_be(9),
			hex!("0000000048c9bdf267e6096a3ba7ca8485ae67bb2bf894fe72f36e3cf1361d5f3af54fa5d182e6ad7f520e511f6c3e2b8c68059b6bbd41fbabd9831f79217e1319cde05b61626300000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000300000000000000000000000000000001").to_vec(),
			hex!("08c9bcf367e6096a3ba7ca8485ae67bb2bf894fe72f36e3cf1361d5f3af54fa5d282e6ad7f520e511f6c3e2b8c68059b9442be0454267ce079217e1319cde05b").to_vec(),
		),
	];

	for (description, precompile_addr, input, output) in cases {
		let (code, _code_hash) = compile_module("call_and_return").unwrap();
```

**File:** substrate/frame/revive/src/tests/pvm.rs (L4761-4772)
```rust
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
```
