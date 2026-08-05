### Title
CREATE2 address griefing permanently blocks a deployer's expected `pallet-revive` contract instantiation - ([File: substrate/frame/revive/src/exec.rs])

### Summary
`pallet_revive::Pallet::instantiate`/`instantiate_with_code` derive the new contract's address deterministically from `(deployer, code_hash, input_data, salt)` for CREATE2 (or `(deployer, nonce)` for CREATE1) via `address::create2`/`address::create1` [1](#0-0) , and `ContractInfo::new` returns `Error::DuplicateContract` if that address already resolves to a contract account. Because instantiation is a fully public, unprivileged dispatchable, any adversary who can predict the same `(deployer, code_hash, input_data, salt)` tuple another party intends to use can pre-instantiate at that exact address first, causing the legitimate deployer's subsequent instantiate call to permanently fail — structurally the same "attacker pre-creates a uniquely-keyed on-chain resource to permanently block the intended creator" pattern as the AutoRoller/SpaceFactory bug.

### Finding Description
`instantiate`/`instantiate_with_code` are unauthenticated, permissionless dispatchables available to any signed account [2](#0-1) . When a salt is supplied, the resulting contract address is computed purely as a deterministic function of public inputs:

`address::create2(&deployer, executable.code(), input_data, salt)` [3](#0-2) 

`ContractInfo::new(&address, nonce, code_hash)` is then called to write contract state at that address, and it is documented to fail with `Error::DuplicateContract` (formerly it trapped the caller; now it returns an error code) when the address is already occupied by a contract [4](#0-3) [5](#0-4) .

Because `deployer` (the caller's mapped address), `code_hash`, `input_data`, and `salt` are all either public (on-chain code hash, caller address) or chosen by the deployer and frequently fixed/well-known (e.g., canonical factory patterns, cross-chain deterministic deployment schemes, CREATE2 "vanity"/predictable deployments used by wallets, multisigs, or bridge/factory contracts), an adversary can observe or predict the exact tuple a victim intends to use and submit their own `instantiate`/`instantiate_with_code` call with the same `(deployer-equivalent input if using a shared factory, code_hash, input_data, salt)` first. Whoever's transaction lands first in the block wins the address; the second, legitimate call now permanently reverts with `DuplicateContract`, since the address occupancy check has no fallback, ownership verification, or reuse path — the contract is not "the same" contract just because the address matches; the victim cannot ever redeploy their intended contract at that specific address on this chain.

This mirrors the AutoRoller bug precisely: an actor relying on a deterministic identifier (there: `maturity` from `RollerUtils#getFutureMaturity`; here: the CREATE2 address) to create a uniquely-keyed on-chain resource via a public entrypoint can be permanently blocked by an unprivileged third party who front-runs the same identifier, because the underlying "already exists" guard (`SpaceFactory#create`'s `POOL_ALREADY_EXISTS`; here `Error::DuplicateContract`/`ContractInfo::new`'s occupancy check) has no reconciliation path — it just hard-fails forever for that specific derived key.

### Impact Explanation
Any protocol, wallet, or bridge component on an Asset Hub / parachain running `pallet-revive` that relies on deterministic CREATE2 deployment for cross-chain address consistency (a common pattern for account abstraction factories, multisig factories, or counterfactual deployment schemes) can have its deployment permanently griefed at zero cost to the attacker beyond gas: the intended contract can never be deployed at the expected address on that chain, which can permanently strand funds/logic that other contracts or off-chain systems expect to find at that deterministic address (e.g., funds sent pre-deployment to the counterfactual address, or cross-chain messaging that hardcodes the expected address). This matches the "public underpriced work"/"permanent user-fund or bridge-state lock" impact class: the griefer's contract occupies the slot with attacker-controlled bytecode, degrading or permanently blocking the legitimate deployment flow.

### Likelihood Explanation
Likelihood is moderate: it requires the attacker to learn the exact `(deployer, code_hash, input_data, salt)` tuple in advance (via mempool observation for front-running, or because the deployment scheme is public/standardized, as is typical for counterfactual/factory deployment patterns), and then race a transaction ahead of the victim's. No privileged role, validator/collator collusion, or leaked keys are required — only observation of a pending or publicly known deployment intent, making it a plausible unprivileged griefing vector wherever deterministic CREATE2 deployment is used as part of an automated or expected on-chain flow.

### Recommendation
- For factory-style / counterfactual deployment flows relying on CREATE2, avoid depending solely on address occupancy as proof of "this is my contract"; verify code hash/owner/salt provenance where the deployed contract matters, and design flows so an occupied-address failure degrades gracefully (e.g., detect and reuse, or bind salts to caller-specific values such as including the caller's account/nonce in the salt derivation to prevent a third party from ever producing the identical tuple).
- Consider strengthening `address::create2` derivation (or documentation/guidance) to discourage patterns where `salt` alone, without caller-specific binding, is sufficient to fully determine the address, which is what enables blind front-running of another account's intended deployment.
- Audit any first-party or system pallets/tooling that programmatically deploy contracts via deterministic CREATE2 addresses (analogous to AutoRoller's automatic series creation) to ensure they handle `DuplicateContract` by falling back (e.g., re-deriving a fresh salt) instead of failing permanently.

### Proof of Concept
1. Victim (or a factory contract acting on the victim's behalf) intends to deploy contract `C` with code hash `H`, empty `input_data`, and salt `S` from deployer address `D`. The resulting CREATE2 address is `A = create2(D, H, [], S)`, fully computable by anyone who observes the pending transaction or knows the standardized parameters.
2. Attacker observes this intent (mempool, or a publicly documented/standard deployment scheme) and submits their own `instantiate`/`instantiate_with_code` call using code hash `H` (or any code, if the goal is just to occupy the slot) with the identical `salt` `S`, from the same deployer semantics needed to reproduce `A`. Because `create2` depends only on public/predictable inputs [6](#0-5) , this is fully derivable off-chain.
3. Attacker's transaction lands first in the block; `ContractInfo::new(&A, ...)` succeeds and occupies `A` with attacker-controlled code.
4. Victim's transaction (or automated flow) then calls `instantiate` with the same tuple; `ContractInfo::new` detects the address is occupied and the constructor frame in `Stack::new`/`push_frame` returns `Error::DuplicateContract` [7](#0-6) , permanently preventing the victim from deploying their intended contract at `A` on this chain — exactly analogous to the AutoRoller being permanently blocked from creating a series/pool at a maturity an adversary pre-created.

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1141-1163)
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
				let contract = ContractInfo::new(
					&address,
					<System<T>>::account_nonce(&sender),
					*executable.code_hash(),
				)?;
```

**File:** substrate/frame/revive/src/lib.rs (L1248-1280)
```rust
		/// Instantiates a new contract from the supplied `code` optionally transferring
		/// some balance.
		///
		/// This dispatchable has the same effect as calling [`Self::upload_code`] +
		/// [`Self::instantiate`]. Bundling them together provides efficiency gains. Please
		/// also check the documentation of [`Self::upload_code`].
		///
		/// # Parameters
		///
		/// * `value`: The balance to transfer from the `origin` to the newly created contract.
		/// * `weight_limit`: The weight limit enforced when executing the constructor.
		/// * `storage_deposit_limit`: The maximum amount of balance that can be charged/reserved
		///   from the caller to pay for the storage consumed.
		/// * `code`: The contract code to deploy in raw bytes.
		/// * `data`: The input data to pass to the contract constructor.
		/// * `salt`: Used for the address derivation. If `Some` is supplied then `CREATE2`
		/// 	semantics are used. If `None` then `CRATE1` is used.
		///
		///
		/// Instantiation is executed as follows:
		///
		/// - The supplied `code` is deployed, and a `code_hash` is created for that code.
		/// - If the `code_hash` already exists on the chain the underlying `code` will be shared.
		/// - The destination address is computed based on the sender, code_hash and the salt.
		/// - The smart-contract account is created at the computed address.
		/// - The `value` is transferred to the new account.
		/// - The `deploy` function is executed in the context of the newly-created account.
		#[pallet::call_index(3)]
		#[pallet::weight(
			<T as Config>::WeightInfo::instantiate_with_code(code.len() as u32, data.len() as u32)
			.saturating_add(*weight_limit)
		)]
		pub fn instantiate_with_code(
```

**File:** prdoc/stable2503/pr_7414.prdoc (L1-20)
```text
title: '[pallet-revive] do not trap the caller on instantiations with duplicate contracts'
doc:
- audience: Runtime Dev
  description: |-
    This PR changes the behavior of `instantiate` when the resulting contract address already exists (because the caller tried to instantiate the same contract with the same salt multiple times): Instead of trapping the caller, return an error code.

    Solidity allows `catch`ing this, which doesn't work if we are trapping the caller. For example, the change makes the following snippet work:

    ```Solidity
    try new Foo{salt: hex"00"}() returns (Foo) {
        // Instantiation was successful (contract address was free and constructor did not revert)
    } catch {
        // This branch is expected to be taken if the instantiation failed because of a duplicate salt
    }
    ```
crates:
- name: pallet-revive
  bump: major
- name: pallet-revive-uapi
  bump: major
```

**File:** prdoc/pr_12645.prdoc (L1-18)
```text
title: '[pallet-revive] Reject re-entrant instantiate at an in-construction address'
doc:
- audience: Runtime Dev
  description: |-
    Fixes https://github.com/paritytech/polkadot-sdk/issues/12639

    A contract's `ContractInfo` is not written to `AccountInfoOf` until its constructor
    frame pops, so the `is_contract` collision guard in `ContractInfo::new` could not see an
    address that was still being constructed. A re-entrant `CREATE2` with the same salt and
    code (which is nonce independent) therefore resolved to the same address and ran a second
    constructor frame for one account, permanently leaking its consumer reference and code
    refcount and orphaning the second child trie's storage deposit.

    `push_frame` now rejects a nested instantiate whose target address already appears as a
    `Constructor` frame on the call stack, returning `DuplicateContract` (matching EIP-684).
crates:
- name: pallet-revive
  bump: patch
```

**File:** substrate/frame/revive/src/address.rs (L45-67)
```rust
/// make use of the [`OriginalAccount`] storage item to reverse the mapping.
pub trait AddressMapper<T: Config>: private::Sealed {
	/// Convert an account id to an ethereum address.
	fn to_address(account_id: &T::AccountId) -> H160;

	/// Convert an ethereum address to a native account id.
	fn to_account_id(address: &H160) -> T::AccountId;

	/// Same as [`Self::to_account_id`] but always returns the fallback account.
	///
	/// This skips the query into [`OriginalAccount`] and always returns the stateless
	/// fallback account. This is useful when we know for a fact that the `address`
	/// in question is originally a `H160`. This is usually only the case when we
	/// generated a new contract address.
	fn to_fallback_account_id(address: &H160) -> T::AccountId;

	/// Create a stateful mapping for `account_id`
	///
	/// This will enable `to_account_id` to map back to the original
	/// `account_id` instead of the fallback account id.
	fn map(account_id: &T::AccountId) -> DispatchResult;

	/// Map an account id without taking any deposit, without verifying that the
```

**File:** substrate/frame/revive/src/exec/tests.rs (L1270-1349)
```rust
#[test]
fn reentrant_instantiate_at_same_address_is_rejected() {
	// EIP-684: while `B1` constructs at address `X`, its constructor re-enters the deployer to
	// instantiate the same code+salt. That resolves to `X` again and must be rejected rather
	// than run a second constructor for one account.
	let salt = [42u8; 32];

	let constructor_ch = MockLoader::insert(Constructor, |ctx, _| {
		// Re-enter the deployer (BOB) while we are still being constructed.
		ctx.ext
			.call(
				&CallResources::NoLimits,
				&BOB_ADDR,
				U256::zero(),
				vec![],
				ReentrancyProtection::AllowReentry,
				false,
			)
			.unwrap();
		exec_success()
	});

	let invocations = Rc::new(RefCell::new(0u32));
	let second_instantiate_error = Rc::new(RefCell::new(None::<DispatchError>));
	let factory_ch = MockLoader::insert(Call, {
		let invocations = Rc::clone(&invocations);
		let second_instantiate_error = Rc::clone(&second_instantiate_error);
		move |ctx, _| {
			*invocations.borrow_mut() += 1;
			let n = *invocations.borrow();
			// Bound the recursion in case the guard fails to reject the collision.
			if n <= 2 {
				let min_balance = <Test as Config>::Currency::minimum_balance();
				let value = Pallet::<Test>::convert_native_to_evm(min_balance);
				let result = ctx.ext.instantiate(
					&CallResources::NoLimits,
					Code::Existing(constructor_ch),
					value,
					vec![],
					Some(&salt),
				);
				if n == 2 {
					if let Err(err) = &result {
						*second_instantiate_error.borrow_mut() = Some(err.error);
					}
				}
			}
			exec_success()
		}
	});

	ExtBuilder::default()
		.with_code_hashes(MockLoader::code_hashes())
		.existential_deposit(15)
		.build()
		.execute_with(|| {
			let min_balance = <Test as Config>::Currency::minimum_balance();
			set_balance(&ALICE, min_balance * 1000);
			place_contract(&BOB, factory_ch);
			let origin = Origin::from_account_id(ALICE);
			let mut meter =
				TransactionMeter::<Test>::new_from_limits(WEIGHT_LIMIT, min_balance * 100).unwrap();

			// `B1` still constructs; only the colliding re-entrant instantiate fails.
			assert_ok!(MockStack::run_call(
				origin,
				BOB_ADDR,
				&mut meter,
				Pallet::<Test>::convert_native_to_evm(min_balance * 100),
				vec![],
				&ExecConfig::new_substrate_tx(),
			));

			// Initial call plus one re-entry; without the guard it would recurse further.
			assert_eq!(*invocations.borrow(), 2);
			assert_eq!(
				*second_instantiate_error.borrow(),
				Some(<Error<Test>>::DuplicateContract.into())
			);
		});
```
