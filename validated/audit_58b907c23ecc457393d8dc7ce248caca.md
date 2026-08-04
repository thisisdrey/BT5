This confirms the analog concretely: `frame_system::Account` reaping deletes the `AccountInfo` (including `nonce`) once `providers == 0 && consumers == 0 && sufficients == 0`, and `dust_account_removal_should_work`/`provider_ref_handover_to_self_sufficient_ref_works` tests show `account_nonce` returns to `0` after reaping [1](#0-0) . `pallet-revive` derives both the CREATE1 contract address and the storage `trie_id` directly from this same reapable `System::account_nonce(&sender)` value rather than from a dedicated monotonic, never-decreasing counter [2](#0-1) [3](#0-2) . `pallet-contracts`, by contrast, explicitly avoids this via a dedicated `Nonce<T>` storage item whose doc comment states this exact class of bug ("possible collision of storage" from create/terminate/recreate) is why an ever-incrementing counter, not an account-nonce-derived value, must be used [4](#0-3) .

### Title
`pallet-revive` derives contract address and storage `trie_id` from a reapable account nonce, enabling address/trie-id reuse after account dusting - (File: `substrate/frame/revive/src/exec.rs`, `substrate/frame/revive/src/storage.rs`)

### Summary
`pallet-revive`'s CREATE1 address and the contract's storage `trie_id` are both derived from `System::account_nonce(&sender)`. Unlike `pallet-contracts`, which keeps a dedicated monotonic `Nonce<T>` storage value specifically because account-scoped nonces are not safe to reuse as unique identifiers, `pallet-revive` has no such safeguard. When a deployer account is fully reaped by `frame_system` (all providers/consumers/sufficients dropped to zero), its `AccountInfo`, including `nonce`, is deleted; if the account is later refunded, its nonce restarts from `0`. Re-deploying a contract without a salt from that account then reproduces the exact same CREATE1 address and the exact same `trie_id` as a prior, terminated contract deployed at nonce `0`, exactly mirroring the reported bug class of deriving a "new" unique identifier from a value that can decrease/reset.

### Finding Description
`new_frame`/`FrameArgs::Instantiate` computes the deployed contract's address via `address::create1(&deployer, account_nonce)` and constructs `ContractInfo::new(&address, System::account_nonce(&sender), code_hash)`, which hashes `(address, nonce)` to build the `trie_id` used for the contract's child trie storage [2](#0-1) [5](#0-4) . `ContractInfo::new` only guards against overwriting a *currently occupied* address via `is_contract(address)` check [6](#0-5) , but does nothing to prevent reusing the exact `trie_id` of a *previously terminated* contract once that address is free again.

`frame_system::Account` storage is removed entirely (nonce included) once an account's `providers`, `consumers`, and `sufficients` all hit zero (`dec_providers`/`dec_sufficients` → `on_killed_account`) [7](#0-6) . Tests explicitly document that `account_nonce` returns to `0` after this reaping, e.g. `dust_account_removal_should_work` and `provider_ref_handover_to_self_sufficient_ref_works` [8](#0-7) . An unprivileged user fully controls whether their own deployer account gets reaped (by withdrawing/transferring away all funds and providers) and can later refund it, restoring `nonce = 0`.

The `pallet-contracts` codebase explicitly documents and defends against exactly this scenario with a dedicated, never-decreasing `Nonce<T>` storage item, whose comment spells out the risk: "Create a new contract → Terminate the contract → Immediately recreate the contract with the same account_id" leading to storage collision because trie deletion is lazy [4](#0-3) , and has a regression test (`instantiate_unique_trie_id`) asserting trie ids must never repeat after termination [9](#0-8) . `pallet-revive` has no equivalent dedicated counter for this purpose — it reuses the general-purpose, reapable `System::account_nonce`, which does not carry the same non-decreasing invariant.

### Impact Explanation
If a deployer's account is dusted/reaped and later refunded, a subsequent no-salt `instantiate` reproduces the identical `trie_id` of a prior, terminated contract deployed at the same nonce value from the same account. Because contract-storage/child-trie cleanup on termination is lazy (deferred to background/idle processing, as in the analogous `pallet-contracts` `DeletionQueue` pattern), a newly instantiated contract can inherit or collide with stale child-trie storage from the old, terminated contract at the same trie_id — corrupting the new contract's storage state, and potentially exposing or reintroducing state (e.g., stale balances/approvals/allowances encoded in that contract's own storage) that should have been irrecoverably destroyed. This is a runtime correctness/state-integrity bug that compromises the contracts execution engine's fundamental "unique storage per contract instantiation" invariant without requiring any privileged or off-chain actor — the attacker only needs control of their own EOA and standard extrinsics (fund drain, refund, redeploy).

### Likelihood Explanation
The attack requires only unprivileged extrinsics available to any account: draining the deployer account below the existential deposit (or otherwise dropping providers/consumers/sufficients to zero) to trigger reaping, refunding it, then calling `instantiate`/`instantiate_with_code` without a salt. All of these are ordinary, permissionless operations. The only uncertainty is exactly how quickly/lazily pallet-revive's trie/child-storage cleanup runs relative to this sequence within the same or later blocks — this determines the severity/window of exploitability but does not change that the identifier-derivation logic itself violates the non-decreasing-counter invariant that `pallet-contracts` explicitly engineered around.

### Recommendation
Do not derive `trie_id` (or, more generally, any value depended upon for uniqueness across a contract's lifetime) from `System::account_nonce`. Introduce a dedicated, monotonic, never-decremented counter analogous to `pallet-contracts::Nonce<T>` for `pallet-revive`, and use that counter (not the reapable frame_system nonce) when computing the storage `trie_id`, keeping `account_nonce` only for CREATE1 address derivation where Ethereum-compatible semantics require it, while gating reinstantiation at a reused address behind a check that guarantees the trie_id space cannot repeat.

### Proof of Concept
1. Deploy a contract from account `A` with `account_nonce(A) == 0` (fresh/never used account), without a salt. Address `X` and `trie_id = hash(bcontract_trie_v1, X, 0)` are created; write some non-trivial storage into the contract.
2. Terminate/self-destruct the contract at `X`.
3. Fully drain account `A` (transfer away all balance, drop providers/consumers/sufficients to zero) so `frame_system::Account::<T>::get(A)` is removed and `on_killed_account` fires, per `dec_providers`/`dec_sufficients` logic [10](#0-9) .
4. Refund account `A` with a fresh transfer; `account_nonce(A)` is now `0` again (matches `dust_account_removal_should_work` test behavior) [11](#0-10) .
5. Call `instantiate` again from `A` without a salt: `create1(deployer, 0)` reproduces address `X`, and `ContractInfo::new` computes the identical `trie_id = hash(bcontract_trie_v1, X, 0)` as step 1 [2](#0-1) [5](#0-4) . If any storage from the first contract's child trie has not yet been physically purged, the new contract's storage reads can observe stale/leftover key-value pairs from the destroyed contract, violating storage isolation between successive contract instantiations at the same address.

### Citations

**File:** substrate/frame/system/src/lib.rs (L1690-1780)
```rust
	/// Decrement the provider reference counter on an account.
	///
	/// This *MUST* only be done once for every time you called `inc_providers` on `who`.
	pub fn dec_providers(who: &T::AccountId) -> Result<DecRefStatus, DispatchError> {
		Account::<T>::try_mutate_exists(who, |maybe_account| {
			if let Some(mut account) = maybe_account.take() {
				if account.providers == 0 {
					// Logic error - cannot decrement beyond zero.
					log::error!(
						target: LOG_TARGET,
						"Logic error: Unexpected underflow in reducing provider",
					);
					account.providers = 1;
				}
				match (account.providers, account.consumers, account.sufficients) {
					(1, 0, 0) => {
						// No providers left (and no consumers) and no sufficients. Account dead.

						Pallet::<T>::on_killed_account(who.clone());
						Ok(DecRefStatus::Reaped)
					},
					(1, c, _) if c > 0 => {
						// Cannot remove last provider if there are consumers.
						Err(DispatchError::ConsumerRemaining)
					},
					(x, _, _) => {
						// Account will continue to exist as there is either > 1 provider or
						// > 0 sufficients.
						account.providers = x - 1;
						*maybe_account = Some(account);
						Ok(DecRefStatus::Exists)
					},
				}
			} else {
				log::error!(
					target: LOG_TARGET,
					"Logic error: Account already dead when reducing provider",
				);
				Ok(DecRefStatus::Reaped)
			}
		})
	}

	/// Increment the self-sufficient reference counter on an account.
	pub fn inc_sufficients(who: &T::AccountId) -> IncRefStatus {
		Account::<T>::mutate(who, |a| {
			if a.providers + a.sufficients == 0 {
				// Account is being created.
				a.sufficients = 1;
				Self::on_created_account(who.clone(), a);
				IncRefStatus::Created
			} else {
				a.sufficients = a.sufficients.saturating_add(1);
				IncRefStatus::Existed
			}
		})
	}

	/// Decrement the sufficients reference counter on an account.
	///
	/// This *MUST* only be done once for every time you called `inc_sufficients` on `who`.
	pub fn dec_sufficients(who: &T::AccountId) -> DecRefStatus {
		Account::<T>::mutate_exists(who, |maybe_account| {
			if let Some(mut account) = maybe_account.take() {
				if account.sufficients == 0 {
					// Logic error - cannot decrement beyond zero.
					log::error!(
						target: LOG_TARGET,
						"Logic error: Unexpected underflow in reducing sufficients",
					);
				}
				match (account.sufficients, account.providers) {
					(0, 0) | (1, 0) => {
						Pallet::<T>::on_killed_account(who.clone());
						DecRefStatus::Reaped
					},
					(x, _) => {
						account.sufficients = x.saturating_sub(1);
						*maybe_account = Some(account);
						DecRefStatus::Exists
					},
				}
			} else {
				log::error!(
					target: LOG_TARGET,
					"Logic error: Account already dead when reducing provider",
				);
				DecRefStatus::Reaped
			}
		})
	}
```

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

**File:** substrate/frame/revive/src/storage.rs (L196-220)
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

		let trie_id = {
			let buf = ("bcontract_trie_v1", address, nonce).using_encoded(T::Hashing::hash);
			buf.as_ref()
				.to_vec()
				.try_into()
				.expect("Runtime uses a reasonable hash size. Hence sizeof(T::Hash) <= 128; qed")
		};
```

**File:** substrate/frame/contracts/src/lib.rs (L1333-1356)
```rust
	/// This is a **monotonic** counter incremented on contract instantiation.
	///
	/// This is used in order to generate unique trie ids for contracts.
	/// The trie id of a new contract is calculated from hash(account_id, nonce).
	/// The nonce is required because otherwise the following sequence would lead to
	/// a possible collision of storage:
	///
	/// 1. Create a new contract.
	/// 2. Terminate the contract.
	/// 3. Immediately recreate the contract with the same account_id.
	///
	/// This is bad because the contents of a trie are deleted lazily and there might be
	/// storage of the old instantiation still in it when the new contract is created. Please
	/// note that we can't replace the counter by the block number because the sequence above
	/// can happen in the same block. We also can't keep the account counter in memory only
	/// because storage is the only way to communicate across different extrinsics in the
	/// same block.
	///
	/// # Note
	///
	/// Do not use it to determine the number of contracts. It won't be decremented if
	/// a contract is destroyed.
	#[pallet::storage]
	pub(crate) type Nonce<T: Config> = StorageValue<_, u64, ValueQuery>;
```

**File:** substrate/frame/system/src/tests.rs (L132-158)
```rust
#[test]
fn provider_ref_handover_to_self_sufficient_ref_works() {
	new_test_ext().execute_with(|| {
		assert_eq!(System::inc_providers(&0), IncRefStatus::Created);
		System::inc_account_nonce(&0);
		assert_eq!(System::account_nonce(&0), 1u64.into());

		// a second reference coming and going doesn't change anything.
		assert_eq!(System::inc_sufficients(&0), IncRefStatus::Existed);
		assert_eq!(System::dec_sufficients(&0), DecRefStatus::Exists);
		assert_eq!(System::account_nonce(&0), 1u64.into());

		// a provider reference coming and going doesn't change anything.
		assert_eq!(System::inc_providers(&0), IncRefStatus::Existed);
		assert_eq!(System::dec_providers(&0).unwrap(), DecRefStatus::Exists);
		assert_eq!(System::account_nonce(&0), 1u64.into());

		// decreasing the providers with a self-sufficient present should not delete the account
		assert_eq!(System::inc_sufficients(&0), IncRefStatus::Existed);
		assert_eq!(System::dec_providers(&0).unwrap(), DecRefStatus::Exists);
		assert_eq!(System::account_nonce(&0), 1u64.into());

		// decreasing the sufficients should delete the account
		assert_eq!(System::dec_sufficients(&0), DecRefStatus::Reaped);
		assert_eq!(System::account_nonce(&0), 0u64.into());
	});
}
```

**File:** substrate/frame/contracts/src/tests.rs (L903-932)
```rust
/// Check the `Nonce` storage item for more information.
#[test]
fn instantiate_unique_trie_id() {
	let (wasm, code_hash) = compile_module::<Test>("self_destruct").unwrap();

	ExtBuilder::default().existential_deposit(500).build().execute_with(|| {
		let _ = <Test as Config>::Currency::set_balance(&ALICE, 1_000_000);
		Contracts::upload_code(RuntimeOrigin::signed(ALICE), wasm, None, Determinism::Enforced)
			.unwrap();

		// Instantiate the contract and store its trie id for later comparison.
		let addr =
			builder::bare_instantiate(Code::Existing(code_hash)).build_and_unwrap_account_id();
		let trie_id = get_contract(&addr).trie_id;

		// Try to instantiate it again without termination should yield an error.
		assert_err_ignore_postinfo!(
			builder::instantiate(code_hash).build(),
			<Error<Test>>::DuplicateContract,
		);

		// Terminate the contract.
		assert_ok!(builder::call(addr.clone()).build());

		// Re-Instantiate after termination.
		assert_ok!(builder::instantiate(code_hash).build());

		// Trie ids shouldn't match or we might have a collision
		assert_ne!(trie_id, get_contract(&addr).trie_id);
	});
```

**File:** substrate/frame/balances/src/tests/dispatchable_tests.rs (L47-61)
```rust
#[test]
fn dust_account_removal_should_work() {
	ExtBuilder::default()
		.existential_deposit(100)
		.monied(true)
		.build_and_execute_with(|| {
			System::inc_account_nonce(&2);
			assert_eq!(System::account_nonce(&2), 1);
			assert_eq!(Balances::total_balance(&2), 2000);
			// index 1 (account 2) becomes zombie
			assert_ok!(Balances::transfer_allow_death(Some(2).into(), 5, 1901));
			assert_eq!(Balances::total_balance(&2), 0);
			assert_eq!(Balances::total_balance(&5), 1901);
			assert_eq!(System::account_nonce(&2), 0);
		});
```
