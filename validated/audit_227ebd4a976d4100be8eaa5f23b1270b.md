### Title
Native/EVM value sent with `CALL` to a stateless (`HAS_CONTRACT_INFO = false`) `pallet-revive` precompile is permanently locked - ([File: substrate/frame/revive/src/exec.rs])

### Summary
`pallet-revive` lets any contract or EOA send a non-zero `value` together with a `CALL`/`eth_call` to any `H160` address, including the addresses of built-in Ethereum precompiles (`ECRecover`, `Sha256`, `Ripemd160`, `Identity`, `Modexp`, `Bn128Add/Mul/Pairing`, `Blake2F`) and any third-party precompile registered with `HAS_CONTRACT_INFO = false`. The value transfer happens unconditionally before the precompile body runs, but none of these stateless precompiles consume, forward, or refund the received value. Because these addresses are fixed, protocol-defined addresses with no corresponding private key and no contract code that can move funds out, any value sent to them is permanently stuck — the exact "payable function ignores `msg.value`" bug class from the external report, transplanted to `pallet-revive`.

### Finding Description
In `Stack::run` (the shared call-frame execution path), the balance transfer for a call is performed unconditionally for every non-delegate frame, *before* the callee code (contract or precompile) executes: [1](#0-0) 

Right after that, the code special-cases only precompiles that opt into `has_contract_info() == true` (they get an account/ED minted and a consumer added): [2](#0-1) 

For precompiles with `HAS_CONTRACT_INFO = false` — which is exactly how all builtin Ethereum-compatibility precompiles are declared, e.g. `System` (`const HAS_CONTRACT_INFO: bool = false;`) and the generic `ERC20` precompile helper — no such account bookkeeping happens, and the precompile body itself never calls `env.transfer(...)` or otherwise moves the received value anywhere: [3](#0-2) 

The `Precompile` trait explicitly documents that `HAS_CONTRACT_INFO = false` means "No account or any other state will be created for the address," yet the value transfer into that address's balance still occurs via `transfer_from_origin` regardless of this flag: [4](#0-3) 

This is confirmed by the repository's own test, which deliberately calls each of the classic Ethereum precompiles (`ECRecover`, `Sha256`, `Ripemd160`, `Identity`, `Modexp`, `Bn128Add`, `Bn128Mul`, `Bn128Pairing`, `Blake2F`) with `value = 100` and asserts the balance simply accumulates at the precompile address afterward: [5](#0-4) 

The corrupted/stuck value is the native balance credited to the `AccountId` that `T::AddressMapper` derives from the fixed precompile `H160` (e.g. `0x...01` for `ECRecover`). This address is a protocol-defined constant, not derived from any keypair, so:
- No signer can ever originate an extrinsic "from" that account to move the funds out.
- The account has no contract code/`ContractInfo` (since `HAS_CONTRACT_INFO = false`), so there is no `terminate()`/self-destruct path to redirect the balance to a beneficiary.
- The precompile's own `call()` implementation (identity, hashing, EC operations, etc.) has no notion of `value` and never forwards or refunds it.

This directly mirrors the reported Solidity bug pattern: a `payable`-equivalent entry point (`CALL` with non-zero `value` to a precompile address) accepts funds but never interacts with them, and — worse than the Solidity case, where the *contract itself* could later be upgraded/patched to sweep the balance — here the destination isn't a contract at all, so the funds are unrecoverable by design.

### Impact Explanation
Any account (EOA-mapped-address or contract) that sends native/EVM value to one of the fixed precompile addresses (either mistakenly, e.g. a naive `.call{value: x}()` in EVM tooling that hard-codes `0x01`–`0x09`, or as a griefing/self-harm vector triggered by a bridged/relayed transaction) permanently loses that value with no recovery path. This is a permanent user-fund lock inside `pallet-revive`, matching the "permanent user-fund or bridge-state lock" category in the impact gate. It does not require a malicious validator, collator, relayer, or governance actor — only a normal `eth_call`/PVM `CALL` with non-zero value directed at a well-known low address.

### Likelihood Explanation
Likelihood is non-trivial: precompile addresses `0x01`–`0x09` are the canonical Ethereum precompile addresses that a large amount of existing EVM tooling, contracts, and naive scripts reference directly (e.g., signature-recovery helper libraries calling `address(1).call{value: ...}(...)` by mistake, or contracts ported from Ethereum that assume these addresses behave like normal EVM precompiles which simply ignore/reject value — Ethereum precompiles do *not* accept value transfers that "disappear"; on Ethereum, sending value to a precompile succeeds but the precompile still just executes its function, and the ETH does land at that address, which is the same "gotcha" that exists on mainnet Ethereum too, but is far more consequential here because there is no way to top up gas and attempt recovery through a future hard fork or protocol-level sweep). The bug is deterministic and 100% reproducible on every call with `value > 0` to any `HAS_CONTRACT_INFO = false` precompile; the repository's own tests document rather than guard against this behavior.

### Recommendation
- Reject calls carrying non-zero `value` when the destination is a precompile with `HAS_CONTRACT_INFO = false`, mirroring the guard already applied for `RUNTIME_PALLETS_ADDR` in `substrate/frame/revive/src/evm/call.rs` (`"Runtime pallets address cannot be called with value"`).
- Alternatively, have `transfer_from_origin` skip/refuse the transfer for such precompile destinations, or have the shared call path in `exec.rs` explicitly branch on `has_contract_info()` before invoking `transfer_from_origin`, consistent with the fact that these addresses are documented as having "no account or any other state."
- Update `pure_precompile_works` (and any other test asserting balance accumulation at stateless precompiles) to instead assert the call reverts or that value is refunded, once the fix lands.

### Proof of Concept
1. Deploy or use any contract on a `pallet-revive`-enabled chain.
2. From that contract (or via `eth_call`), perform a low-level `CALL` with `value = N > 0` to address `0x0000000000000000000000000000000000000001` (the `ECRecover` precompile) with arbitrary/valid ABI-encoded input.
3. Observe the call succeeds and returns the expected `ECRecover` output (as shown by `pure_precompile_works`), and `Pallet::<T>::evm_balance(&precompile_addr)` now equals `N`.
4. Attempt to retrieve the `N` value from address `0x...01`: there is no private key for this address, no `ContractInfo`, and no code path in `System`/`ECRecover`/etc. that transfers the held balance anywhere — the funds are permanently locked, reproducible with the exact assertions already present in [5](#0-4) .

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

**File:** substrate/frame/revive/src/precompiles/builtin/system.rs (L31-49)
```rust
pub struct System<T>(PhantomData<T>);

impl<T: Config> BuiltinPrecompile for System<T> {
	type T = T;
	type Interface = ISystem::ISystemCalls;
	const MATCHER: BuiltinAddressMatcher =
		BuiltinAddressMatcher::Fixed(NonZero::new(0x900).unwrap());
	const HAS_CONTRACT_INFO: bool = false;

	fn call(
		_address: &[u8; 20],
		input: &Self::Interface,
		env: &mut impl Ext<T = Self::T>,
	) -> Result<Vec<u8>, Error> {
		use ISystem::ISystemCalls;
		match input {
			ISystemCalls::terminate(_) if env.is_read_only() => {
				Err(crate::Error::<T>::StateChangeDenied.into())
			},
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

**File:** substrate/frame/revive/src/tests/pvm.rs (L4751-4782)
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
}
```
