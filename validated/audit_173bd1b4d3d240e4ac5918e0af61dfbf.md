## Analysis

The Solana report's core invariant break is: **a state-creation instruction that uses a deterministically-derived address is guarded with a "must not already exist" check (`init`) instead of an idempotent/graceful check, so anyone who can pre-populate that address permanently blocks the legitimate creator.**

The closest verifiable analog in this repository is `pallet-revive`'s `CREATE2` contract instantiation path, where a contract's account is deterministically derived and a duplicate-address instantiation permanently fails.

### Where the pattern lives

`address::create2` computes the target account purely from `(deployer, code, input_data, salt)` — it does **not** depend on the account nonce: [1](#0-0) 

That address is then handed to `ContractInfo::new`, which enforces a strict "must not already exist" guard and returns `Error::DuplicateContract` if any contract (even an empty/self-destructed placeholder) already occupies the address: [2](#0-1) 

`push_frame` propagates this the same way for the direct extrinsic-level `instantiate`/`instantiate_with_code` calls, and the pallet's own error catalogue documents `DuplicateContract`: "A contract with the same AccountId already exists": [3](#0-2) 

The behavior is explicitly exercised and asserted by test `create2_works`, which confirms the address is fully deterministic from `(caller_addr, initcode, salt)`: [4](#0-3) 

and by `instantiate_return_code`, which shows a second `instantiate` at the same salt returns `RuntimeReturnCode::DuplicateContractAddress` rather than succeeding or being retried: [5](#0-4) 

### Why this mirrors the Solana bug

- In the Solana report, `bonding_curve_token_account` is an ATA whose address is a pure function of `(mint, bonding_curve authority)` — knowable by anyone before the legitimate transaction lands, and guarded by Anchor's `init` (fails if it already exists).
- In `pallet-revive`, a `CREATE2` contract's account is a pure function of `(deployer, code, salt)` — also knowable by anyone who can observe/predict the salt and code (e.g. deterministic-deployment factory patterns, cross-chain vanity addresses, counterfactual wallet deployment), and guarded by `ContractInfo::new`'s hard existence check that returns `DuplicateContract` with **no idempotent fallback**.
- In both cases, once an adversary occupies the target address first, the intended party's creation transaction is **permanently** rejected — there is no `init_if_needed`-equivalent path in `pallet-revive`'s instantiate flow; the address is burned until (and unless) the squatting contract is destroyed via `seal_terminate`, which requires the squatter's cooperation.

### Caveat / uncertainty

Unlike the Solana ATA (which is derived from public token/mint parameters independent of the creator), pallet-revive's `CREATE2` deployer field is normally the direct transaction signer, so an attacker without that signer's private key cannot generally reproduce the same `(deployer, code, salt)` tuple. The griefing surface is real only when the "deployer" in the formula is a **shared, publicly-callable factory account** (a pattern increasingly used for deterministic/counterfactual deployments), in which case any party can race to occupy an address before the intended deployer. I was not able to locate a concrete privileged/factory pallet built on top of `pallet-revive`'s `instantiate` in this repo within the available index to confirm such a factory exists on-chain today; this weakens the claim to "the primitive exists and is exploitable in a factory-based deployment flow" rather than "an exploitable public entrypoint exists today with no legitimate signer requirement." I flag this explicitly rather than overstate certainty.

### Title
Permanent contract-address griefing via CREATE2 duplicate-address rejection with no idempotent fallback - (File: substrate/frame/revive/src/storage.rs)

### Summary
`ContractInfo::new` rejects instantiation at any `CREATE2`-derived address that is already occupied by returning `Error::DuplicateContract`, with no idempotent/`init_if_needed`-style recovery path, mirroring the Solana bonding-curve ATA `init` DoS pattern.

### Finding Description
`address::create2` derives the contract account solely from `(deployer, code, input_data, salt)` [6](#0-5) , and `ContractInfo::new` unconditionally fails if that address is already a contract [7](#0-6) . When the deployer identity in this formula is shared or publicly reproducible (e.g. a public deployment-factory pattern), any account that can predict `(code, salt)` ahead of the intended party can instantiate first — even a trivially reverting/self-destructing contract — permanently occupying the address. There is no idempotent-success or "adopt existing" branch; the legitimate deployer's transaction always errors out with `DuplicateContract`.

### Impact Explanation
A griefed deployer permanently loses the ability to deploy to the intended deterministic address, breaking any protocol relying on address determinism (counterfactual wallets, cross-chain address parity, precomputed integration addresses), and any code-upload/deposit costs already paid are wasted — a public, underpriced DoS against a specific victim with no privileged actor required.

### Likelihood Explanation
Requires only knowledge of the target `(deployer, code, salt)` tuple ahead of the legitimate call, which is realistic wherever deployment addresses are advertised/precomputed in advance (a common integration pattern) rather than kept secret until inclusion.

### Recommendation
Provide an explicit "idempotent instantiate" mode: if the target address already holds a contract whose code hash and initialization parameters exactly match the requested deployment, treat instantiation as a no-op success (return the existing address) instead of unconditionally erroring with `DuplicateContract`; alternatively, require an unpredictable, deployer-bound salt component so pre-occupation is infeasible.

### Proof of Concept
1. Attacker learns `(deployer_address, code_hash, salt)` that a victim plans to use for `instantiate_with_code`/`instantiate` (e.g. published as part of a counterfactual-deployment scheme).
2. Attacker instantiates trivial code at the same `create2(deployer_address, code, input_data, salt)` address first (via a shared factory path where deployer can be attacker-controlled or reproduced), consuming `ContractInfo::new`'s existence slot.
3. Victim's subsequent legitimate `instantiate` to the same address now unconditionally fails with `Error::DuplicateContract`, as demonstrated by the existing test assertion pattern in [5](#0-4) , with no retry/idempotent path available.

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1141-1158)
```rust
			FrameArgs::Instantiate { sender, executable, salt, input_data } => {
				let deployer = T::AddressMapper::to_address(&sender);
				let account_nonce = <System<T>>::account_nonce(&sender);
				let address = if let Some(salt) = salt {
					address::create2(&deployer, executable.code(), input_data, salt)
				} else {
					use sp_runtime::Saturating;
					address::create1(
						&deployer,
						// the Nonce from the origin has been incremented pre-dispatch, so we
						// need to subtract 1 to get the nonce at the time of the call.
						if origin_is_caller {
							account_nonce.saturating_sub(1u32.into()).saturated_into()
						} else {
							account_nonce.saturated_into()
						},
					)
				};
```

**File:** substrate/frame/revive/src/storage.rs (L196-212)
```rust
	pub fn new(
		address: &H160,
		nonce: T::Nonce,
		code_hash: sp_core::H256,
	) -> Result<Self, DispatchError> {
		if <AccountInfo<T>>::is_contract(address) {
			return Err(Error::<T>::DuplicateContract.into());
		}

		// Reject reuse of an address whose previous occupant still has unflushed
		// `NativeDepositOf` rows in the deletion queue. The on_idle drain will eventually
		// clear them; until it does, instantiating here would let the new contract inherit
		// stale per-payer entitlements.
		let account_id = T::AddressMapper::to_fallback_account_id(address);
		if NativeDepositOf::<T>::iter_prefix(&account_id).next().is_some() {
			return Err(Error::<T>::PendingDepositCleanup.into());
		}
```

**File:** substrate/frame/contracts/src/lib.rs (L1265-1266)
```rust
		/// A contract with the same AccountId already exists.
		DuplicateContract,
```

**File:** substrate/frame/revive/src/tests/sol/contract.rs (L659-670)
```rust
		let result = builder::bare_call(caller_addr)
			.data(create_call_data)
			.native_value(1000)
			.build_and_unwrap_result();

		let callee_addr = Caller::create2Call::abi_decode_returns(&result.data).unwrap();

		// Compute expected CREATE2 address
		let expected_addr = crate::address::create2(&caller_addr, &initcode, &[], &salt);

		let callee_addr: H160 = callee_addr.0.0.into();
		assert_eq!(callee_addr, expected_addr, "CREATE2 address should be deterministic");
```

**File:** substrate/frame/revive/src/tests/pvm.rs (L1477-1482)
```rust
		// Contract instantiation fails because the same salt is being used again.
		let result = builder::bare_call(contract.addr)
			.data(callee_hash.iter().chain(&0u32.to_le_bytes()).cloned().collect())
			.build_and_unwrap_result();
		assert_return_code!(result, RuntimeReturnCode::DuplicateContractAddress);
	});
```
