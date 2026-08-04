## Analysis

I found a concrete local analog in `pallet-revive`'s handling of native value sent to "pure" (stateless) precompiles such as `ECRecover`, `Sha256`, `Ripemd160`, `Identity`, `Modexp`, `Bn128Add/Mul/Pairing`, and `Blake2F`. These precompiles have `HAS_CONTRACT_INFO = false`, meaning by design "no account or any other state will be created for the address" [1](#0-0) , and their `call()` entry point only receives ABI-decoded input — it has no host-function access to move, forward, or reject `msg.value` [2](#0-1) .

Despite this, the generic `PrecompileExt::call` path unconditionally performs the native-value transfer for *any* callee, including pure precompiles, before dispatching into the precompile logic [3](#0-2) . The destination account used for the transfer is the fallback account derived purely from the address bytes [4](#0-3) , and the existing test `pure_precompile_works` explicitly confirms that after calling e.g. `ECRecover`/`Sha256`/`Identity` with `value = 100`, `Pallet::<Test>::evm_balance(&precompile_addr)` becomes `100` [5](#0-4) .

This mirrors the external report's core invariant break: a payable entry point accepts native value it has no logic to consume or return, and the caller's funds are silently retained by an address that cannot use, withdraw, or forward them.

### Title
Native value sent to stateless "pure" precompiles is permanently locked with no return path - (File: `substrate/frame/revive/src/exec.rs`)

### Summary
`Stack::call` in `pallet-revive` transfers `msg.value` into the target account before dispatching to a precompile, without checking whether the precompile is a "pure" function precompile (`HAS_CONTRACT_INFO = false`) that has no mechanism to spend, forward, or refund that value.

### Finding Description
Precompiles with `HAS_CONTRACT_INFO = false` are documented as never having account state and only implementing the stateless `Precompile::call` entry point, which receives only `input`/`env: &mut impl Ext` and returns ABI-encoded bytes — it cannot move balance out or reject an incoming transfer [6](#0-5) . Nonetheless, `Stack::new_frame`/`Stack::call` performs `Self::transfer_from_origin(...)` for every non-delegate call unconditionally, before checking whether the callee is a precompile at all [7](#0-6) . The mint-and-consumer-registration guard only fires `if precompile.has_contract_info()`, meaning for pure precompiles no account/consumer bookkeeping is even established even though the balance transfer already succeeded [8](#0-7) . The destination for the transfer is `T::AddressMapper::to_fallback_account_id(dest_addr)` — a stateless, address-derived account with no owning key or contract logic that could later withdraw the funds [4](#0-3) .

### Impact Explanation
Any account (EOA, contract, or Solidity `call{value: x}(...)`/`send`/`transfer` from EVM-compatible tooling) that sends native value to one of the fixed low-numbered precompile addresses (1–9, or the custom `System` precompile at `0x900`) will have that value permanently and irrecoverably locked, since the precompile logic cannot spend it and no privileged withdrawal mechanism exists for these addresses. This is a direct, unbacked, permanent user-fund lock, matching the "permanent user-fund or bridge-state lock" impact category, and is reachable by any unprivileged user without needing a malicious peer, relayer, or governance actor.

### Likelihood Explanation
Likelihood is non-trivial: EVM-tooling users routinely send `value` alongside calls (e.g., via `.call{value: ...}()` in Solidity, or naive wallet integrations that attach gas/value defaults), and precompile addresses 1–9 are commonly interacted with directly in low-level EVM code. No special conditions, front-running, or privileged access are required — a single mistaken or naive transaction is sufficient to lose funds, exactly as in the seed report.

### Recommendation
In `Stack::call`/`new_frame`, check whether the resolved callee is a precompile with `HAS_CONTRACT_INFO = false` (a "pure" precompile) before performing `transfer_from_origin`, and revert with an explicit error (e.g., `Error::<T>::TransferToPurePrecompileNotAllowed`) if `value` is non-zero, rather than silently completing the transfer into an unusable fallback account.

### Proof of Concept
1. Deploy any contract, or use a signed extrinsic through `eth_call`/`bare_call`, targeting a fixed precompile address, e.g. `H160::from_low_u64_be(2)` (`Sha256`).
2. Issue a call with `value = 100` and any valid `Sha256` input, e.g. as done in the existing test harness:
```rust
let result = builder::bare_call(addr)
    .data((&precompile_addr, 100u64).encode().into_iter().chain(input).collect::<Vec<_>>())
    .build_and_unwrap_result();
assert_eq!(Pallet::<Test>::evm_balance(&precompile_addr), U256::from(100));
``` [5](#0-4) 
3. Observe the call succeeds and returns the expected `Sha256` digest, and the precompile's fallback account balance is now `100`.
4. There is no dispatchable, precompile function, or governance call in the codebase that allows withdrawing balance from this fallback account back to the sender — the value is permanently stuck.

### Citations

**File:** substrate/frame/revive/src/precompiles.rs (L162-224)
```rust
pub trait Precompile {
	/// Your runtime.
	type T: Config;
	/// The Solidity ABI definition of this pre-compile.
	///
	/// Use the [`self::alloy::sol`] macro to define your interface using Solidity syntax.
	/// The input the caller passes to the pre-compile will be validated and parsed
	/// according to this interface.
	///
	/// Please note that the return value is not validated and it is the pre-compiles
	/// duty to return the abi encoded bytes conformant with the interface here.
	type Interface: SolInterface;
	/// Defines at which addresses this pre-compile exists.
	const MATCHER: AddressMatcher;
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

	/// Entry point for your pre-compile when `HAS_CONTRACT_INFO = false`.
	#[allow(unused_variables)]
	fn call(
		address: &[u8; 20],
		input: &Self::Interface,
		env: &mut impl Ext<T = Self::T>,
	) -> Result<Vec<u8>, Error> {
		unimplemented!("{UNIMPLEMENTED}")
	}
```

**File:** substrate/frame/revive/src/exec.rs (L1375-1419)
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

			let mut code_deposit = executable
				.as_executable()
				.map(|exec| exec.code_info().deposit())
				.unwrap_or_default();

			let mut output = match executable {
				ExecutableOrPrecompile::Executable(executable) => {
					executable.execute(self, entry_point, input_data)
				},
				ExecutableOrPrecompile::Precompile { instance, .. } => {
					instance.call(input_data, self)
				},
			}
```

**File:** substrate/frame/revive/src/exec.rs (L2178-2184)
```rust

			// We can skip the stateful lookup for pre-compiles.
			let dest = if <AllPrecompiles<T>>::get::<Self>(dest_addr.as_fixed_bytes()).is_some() {
				T::AddressMapper::to_fallback_account_id(dest_addr)
			} else {
				T::AddressMapper::to_account_id(dest_addr)
			};
```

**File:** substrate/frame/revive/src/tests/pvm.rs (L4751-4773)
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
```
