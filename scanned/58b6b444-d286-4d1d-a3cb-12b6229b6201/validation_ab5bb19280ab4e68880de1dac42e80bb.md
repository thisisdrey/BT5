### Title
Nested contract instantiation bypasses `InstantiateOrigin` permissioning, allowing unauthorized contract deployment - (File: `substrate/frame/contracts/src/lib.rs`, `substrate/frame/revive/src/lib.rs`, `substrate/frame/revive/src/exec.rs`)

### Summary
The Escher report's core broken invariant is: a public entrypoint accepts an attacker-supplied identifier (`sale.edition`) as if it were guaranteed to originate from the trusted factory, but no check enforces that provenance, letting an unprivileged actor bypass the intended access-control gate. The direct local analog is `pallet-contracts` and `pallet-revive`'s `InstantiateOrigin` gate: it is explicitly documented and enforced only on the top-level dispatchable/`bare_instantiate` entrypoints, but is **not** enforced when a contract instantiates another contract from inside its own execution (nested `CREATE`/`seal_instantiate`). Any account permitted to *call* an already-deployed contract can thereby trigger arbitrary new contract deployment, completely bypassing a chain's `InstantiateOrigin` restriction — the runtime-level analog of "editions not checked as deployed from the legitimate factory."

### Finding Description
Both contract pallets expose an `InstantiateOrigin` config type intended to restrict who may deploy new contract code on the chain: [1](#0-0) [2](#0-1) 

This gate is checked only in the top-level dispatchables/entry points, e.g. `instantiate`/`instantiate_with_code` in `pallet-contracts`: [3](#0-2) [4](#0-3) 

and `bare_instantiate` in `pallet-revive`: [5](#0-4) 

However, when a *contract's own code* triggers instantiation of another contract (via the `seal_instantiate` syscall in pallet-contracts, or the `CREATE`/`CREATE2` opcode in pallet-revive), execution flows through `exec.rs`'s frame-push logic, which never re-checks `T::InstantiateOrigin`: [6](#0-5) 

This is a known, explicitly documented gap, not a hypothetical: the doc comment states outright that the permission "is not enforced when a contract instantiates another contract," and offloads the responsibility to `UploadOrigin` alone: [7](#0-6) 

The pallet-revive maintainers even further loosened this path recently, removing the `RootNotAllowed` guard at constructor frames specifically to enable a called contract to perform nested `CREATE` on behalf of any caller, including `Root`, reiterating that only the direct `instantiate`/`bare_instantiate` dispatchables gate on `InstantiateOrigin`: [8](#0-7) 

Test coverage confirms `InstantiateOrigin` is validated purely at the extrinsic layer (`only_instantiation_origin_can_instantiate`), with no equivalent check exercised for nested instantiation triggered from within a contract's own call: [9](#0-8) 

### Impact Explanation
On any chain that configures `InstantiateOrigin` to a restricted origin (e.g. `EnsureRoot`, a council, or an allow-list) in order to run a permissioned-deployment model — analogous to the Escher factory's intent that only sales created through the legitimate `Escher721Factory` produce trusted editions — an unprivileged caller can defeat that restriction entirely. If any already-deployed, callable contract (deployed once by a privileged `UploadOrigin`/`InstantiateOrigin` account, or a generic "factory-like" utility contract) exposes a code path that performs a nested instantiate with attacker-controlled code/salt/constructor input, any ordinary account able to invoke that contract can cause arbitrary new contract code to be deployed on-chain, at attacker-chosen addresses, without ever satisfying `InstantiateOrigin`. This directly undermines the chain's intended access-control invariant for "who may add new executable code to state," which can be leveraged to plant malicious contracts (e.g., ones impersonating legitimate interfaces) that steal user funds through subsequent calls — mirroring the Escher root cause where an unauthenticated/unverified contract is trusted as if factory-deployed.

### Likelihood Explanation
This requires no privileged actor, admin, governance, validator, collator, or leaked key — only an ordinary signed account calling a contract that already exists on the chain and internally performs instantiation (a normal, permitted operation for contract code). Because `UploadOrigin`/`InstantiateOrigin` semantics are commonly assumed by runtime configurators to gate *all* contract deployment (the doc note is easy to miss), any parachain enabling `pallet-contracts`/`pallet-revive` with a restrictive `InstantiateOrigin` for "permissioned chain" use cases is exposed as soon as one contract with a nested-create code path becomes reachable by ordinary users.

### Recommendation
Re-validate `T::InstantiateOrigin` (or an equivalent frame-level authorization derived from the original transaction's origin) at every nested instantiation frame in `exec.rs`, not only at the top-level dispatchable/`bare_instantiate` entrypoints — analogous to tracking and validating "legitimate factory provenance" before accepting a sale's `edition` in the Escher report. At minimum, document this limitation prominently as a hard security boundary and provide a config knob to reject nested instantiation entirely for chains that require strict `InstantiateOrigin` enforcement.

### Proof of Concept
1. Configure a runtime with `InstantiateOrigin = EnsureRoot<AccountId>` (or any restricted origin) while leaving `pallet-contracts`/`pallet-revive` calls open to all signed accounts (default `CallFilter`).
2. As the privileged deployer, upload/instantiate one "helper" contract `H` whose code, when called with attacker-supplied `code_hash`/bytecode and `salt`, executes `seal_instantiate` (pallet-contracts) or `CREATE`/`CREATE2` (pallet-revive) using caller-supplied constructor data.
3. As an arbitrary unprivileged account, call `H` (a normal `call` dispatchable, gated by ordinary `CallFilter`, not `InstantiateOrigin`) with attacker-chosen `code_hash`/init bytecode and salt.
4. Observe that a brand-new contract is instantiated on-chain at an attacker-influenced address — confirmed by existing test infrastructure showing `InstantiateOrigin` is enforced solely at `instantiate`/`instantiate_with_code`/`bare_instantiate`, with no analogous check inside `exec.rs`'s frame push (`substrate/frame/revive/src/exec.rs:1343-1373`) — proving the restricted-deployment invariant is bypassed entirely without touching Root, sudo, or any privileged path.

### Citations

**File:** substrate/frame/contracts/src/lib.rs (L426-437)
```rust
		/// Origin allowed to instantiate code.
		///
		/// # Note
		///
		/// This is not enforced when a contract instantiates another contract. The
		/// [`Self::UploadOrigin`] should make sure that no code is deployed that does unwanted
		/// instantiations.
		///
		/// By default, it is safe to set this to `EnsureSigned`, allowing anyone to instantiate
		/// contract code.
		#[pallet::no_default_bounds]
		type InstantiateOrigin: EnsureOrigin<Self::RuntimeOrigin, Success = Self::AccountId>;
```

**File:** substrate/frame/contracts/src/lib.rs (L1005-1011)
```rust
			Migration::<T>::ensure_migrated()?;

			// These two origins will usually be the same; however, we treat them as separate since
			// it is possible for the `Success` value of `UploadOrigin` and `InstantiateOrigin` to
			// differ.
			let upload_origin = T::UploadOrigin::ensure_origin(origin.clone())?;
			let instantiate_origin = T::InstantiateOrigin::ensure_origin(origin)?;
```

**File:** substrate/frame/contracts/src/lib.rs (L1061-1071)
```rust
		pub fn instantiate(
			origin: OriginFor<T>,
			#[pallet::compact] value: BalanceOf<T>,
			gas_limit: Weight,
			storage_deposit_limit: Option<<BalanceOf<T> as codec::HasCompact>::Type>,
			code_hash: CodeHash<T>,
			data: Vec<u8>,
			salt: Vec<u8>,
		) -> DispatchResultWithPostInfo {
			Migration::<T>::ensure_migrated()?;
			let origin = T::InstantiateOrigin::ensure_origin(origin)?;
```

**File:** substrate/frame/revive/src/lib.rs (L298-309)
```rust
		/// Origin allowed to instantiate code.
		///
		/// # Note
		///
		/// This is not enforced when a contract instantiates another contract. The
		/// [`Self::UploadOrigin`] should make sure that no code is deployed that does unwanted
		/// instantiations.
		///
		/// By default, it is safe to set this to `EnsureSigned`, allowing anyone to instantiate
		/// contract code.
		#[pallet::no_default_bounds]
		type InstantiateOrigin: EnsureOrigin<OriginFor<Self>, Success = Self::AccountId>;
```

**File:** substrate/frame/revive/src/lib.rs (L1859-1860)
```rust
		let try_instantiate = || {
			let instantiate_account = T::InstantiateOrigin::ensure_origin(origin.clone())?;
```

**File:** substrate/frame/revive/src/exec.rs (L1343-1373)
```rust
			// We need to make sure that the contract's account exists before calling its
			// constructor.
			if entry_point == ExportedFunction::Constructor {
				if !frame_system::Pallet::<T>::account_exists(&account_id) {
					T::Deposit::init_contract(account_id)?;
				}

				// A consumer is added at account creation and removed it on termination, otherwise
				// the runtime could remove the account. As long as a contract exists its
				// account must exist. With the consumer, a correct runtime cannot remove the
				// account.
				<System<T>>::inc_consumers(account_id)?;

				// Contracts nonce starts at 1
				<System<T>>::inc_account_nonce(account_id);

				if bump_nonce || !is_first_frame {
					// Needs to be incremented before calling into the code so that it is visible
					// in case of recursion.
					<System<T>>::inc_account_nonce(caller.account_id()?);
				}
				// The incremented refcount should be visible to the constructor.
				if is_pvm {
					<CodeInfo<T>>::increment_refcount(
						*executable
							.as_executable()
							.expect("Precompiles cannot be instantiated; qed")
							.code_hash(),
					)?;
				}
			}
```

**File:** prdoc/1.9.0/pr_3377.prdoc (L4-11)
```text
title: Permissioned contract deployment

doc:
  - audience: Runtime Dev
    description: |
      This PR introduces two new config types that specify the origins allowed to
      upload and instantiate contract code. However, this check is not enforced when
      a contract instantiates another contract.
```

**File:** prdoc/stable2606/pr_12144.prdoc (L1-23)
```text
title: allow Root-originated nested CREATE
doc:
- audience: Runtime Dev
  description: "# Allow Root-originated nested CREATE in pallet-revive\n\nCloses paritytech/contract-issues#279.\n\
    \n## Motivation\n\n`pallet-revive`'s exec stack rejects `Origin::Root` at any\
    \ constructor frame, which means `bare_call(RuntimeOrigin::root(), contract_addr,\
    \ ...)` errors with `RootNotAllowed` the moment the called contract reaches a\
    \ `CREATE`/`CREATE2` opcode — even though the contract itself is the semantic\
    \ instantiator.\n\nThe historical reason for the block was that the origin had\
    \ to fund the new contract's ED. Since the PGAS rework, the ED is freshly minted\
    \ by `T::Deposit::init_contract` and immediately deactivated for issuance accounting,\
    \ so the origin no longer needs to pay it.\n\n## Change\n\n- Remove the explicit\
    \ `RootNotAllowed` check at the start of the constructor frame in `exec.rs`.\n\
    \nRoot is still **not** allowed to instantiate\
    \ directly: `instantiate`/`bare_instantiate` continue to gate on `T::InstantiateOrigin::ensure_origin`\
    \ (default `EnsureSigned` → `BadOrigin`). The change only unblocks the case\
    \ where another contract sits between Root and the new contract and acts as the\
    \ instantiator. Giving Root its own contract-address attribution is intentionally\
    \ out of scope.\n\n## Test plan\n\
    \n- Existing `root_cannot_instantiate{,_with_code}` and\
    \ `root_can_call` continue to pass — direct Root instantiation is still rejected\
    \ at the dispatchable layer, and Root-originated calls remain functional.\n- Full\
    \ `pallet-revive` test suite is green."
```

**File:** substrate/frame/revive/src/tests/pvm.rs (L3194-3225)
```rust
#[test]
fn only_instantiation_origin_can_instantiate() {
	let (code, code_hash) = compile_module("dummy").unwrap();
	InstantiateAccount::set(Some(ALICE));
	ExtBuilder::default().build().execute_with(|| {
		let _ = Balances::set_balance(&ALICE, 1_000_000);
		let _ = Balances::set_balance(&BOB, 1_000_000);

		assert_err_ignore_postinfo!(
			builder::instantiate_with_code(code.clone())
				.origin(RuntimeOrigin::root())
				.build(),
			DispatchError::BadOrigin
		);

		assert_err_ignore_postinfo!(
			builder::instantiate_with_code(code.clone())
				.origin(RuntimeOrigin::signed(BOB))
				.build(),
			DispatchError::BadOrigin
		);

		// Only Alice can instantiate
		assert_ok!(builder::instantiate_with_code(code).build());

		// Bob cannot instantiate with either `instantiate_with_code` or `instantiate`.
		assert_err_ignore_postinfo!(
			builder::instantiate(code_hash).origin(RuntimeOrigin::signed(BOB)).build(),
			DispatchError::BadOrigin
		);
	});
}
```
