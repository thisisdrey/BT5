Audit Report

## Title
Nested Contract Instantiation Bypasses `InstantiateOrigin`/`UploadOrigin` Restrictions - (File: `substrate/frame/contracts/src/exec.rs`, `substrate/frame/contracts/src/lib.rs`)

## Summary
`pallet-contracts` exposes `T::UploadOrigin`/`T::InstantiateOrigin` config items so runtimes can restrict which accounts may deploy code or instantiate contracts. This restriction is enforced only at the outer dispatchable boundary (`instantiate`, `instantiate_with_code`, `upload_code`) via `InstantiateInput::ensure_origin`, which merely rejects `Origin::Root` and accepts any `Origin::Signed` [1](#0-0) , and is never re-checked when a contract calls `Ext::instantiate` on itself from within a running `call()`/constructor via `ExecStack::run_instantiate`/`push_frame`.

## Finding Description
`T::InstantiateOrigin`/`T::UploadOrigin` are intended as governance-controlled allow-lists restricting who may deploy new contract code, as documented in the PR that introduced them [2](#0-1) . The `Invokable` implementation for `InstantiateInput` only performs the coarse `Origin::Signed` vs `Origin::Root` check in `ensure_origin`, deferring to the outer dispatch path for the actual configured origin filter, and that filter is never consulted inside `ExecStack::run_instantiate` [1](#0-0) . The existing regression test `only_instantiation_origin_can_instantiate` confirms the restriction is enforced only for the direct/dispatchable `instantiate`/`instantiate_with_code` extrinsics, rejecting Bob with `BadOrigin` [3](#0-2) , but the `instantiation_from_contract` unit test demonstrates the nested `ctx.ext.instantiate(...)` path succeeds unconditionally regardless of caller privilege, with no origin check exercised at all [4](#0-3) . The `create_storage_and_instantiate.rs` fixture shows this is a realistic, legitimate contract pattern (a contract calling `api::instantiate_v2` with an attacker-supplied `code_hash`) that any unprivileged caller of that contract's `call()` entrypoint can trigger [5](#0-4) .

## Impact Explanation
This directly matches the Polkadot SDK Pivot requirement that "Public wrappers such as ... contracts and revive must not widen origin, bypass filters, or undercharge nested execution." Any runtime that relies on `InstantiateOrigin`/`UploadOrigin` to gate code deployment (e.g., to prevent unbounded state bloat or restrict deployable code to an approved set) loses that guarantee as soon as any reachable contract exposes an instantiate/factory primitive, since an unprivileged account can cause arbitrary new code to be deployed on-chain purely by calling an already-deployed, permissioned contract — an origin-escalation/unauthorized-execution bug against the intended behavior of a security-relevant guard.

## Likelihood Explanation
Exploitation requires only a standard, unprivileged `call()` extrinsic against any contract that internally exposes an instantiate capability — no relayer, validator, governance, or leaked-key assumptions are needed. Factory/proxy-deploy patterns (exactly like the repository's own `create_storage_and_instantiate.rs` fixture) are common and legitimate, making any chain that both deploys such a contract and relies on `InstantiateOrigin` for governance immediately exposed.

## Recommendation
Enforce `T::InstantiateOrigin`/`T::UploadOrigin` (or an explicit, deliberate opt-out) at every nested `ExecStack::run_instantiate`/`push_frame` invocation triggered via `Ext::instantiate`, not only at the outer dispatchable boundary, binding the permission check to the actual code-deployment action regardless of call depth.

## Proof of Concept
1. Configure a test runtime with `InstantiateOrigin = EnsureSignedBy<Alice>` as in `only_instantiation_origin_can_instantiate`.
2. Deploy, as Alice, a factory contract using the `create_storage_and_instantiate` pattern that calls `api::instantiate_v2(...)` internally with a caller-supplied `code_hash`.
3. As Bob (not authorized by `InstantiateOrigin`), call the factory contract's `call()` entrypoint, passing an arbitrary `code_hash`.
4. Observe the nested `instantiate` succeeds and deploys new code under Bob's initiation, despite Bob being rejected by `InstantiateOrigin` when invoking `instantiate`/`instantiate_with_code` directly — confirmed by contrasting `only_instantiation_origin_can_instantiate` (outer path rejected) against `instantiation_from_contract` (nested path succeeds unconditionally).

### Citations

**File:** substrate/frame/contracts/src/lib.rs (L1605-1653)
```rust
impl<T: Config> Invokable<T> for InstantiateInput<T> {
	type Output = (AccountIdOf<T>, ExecReturnValue);

	fn run(
		self,
		common: CommonInput<T>,
		mut gas_meter: GasMeter<T>,
	) -> InternalOutput<T, Self::Output> {
		let mut storage_deposit = Default::default();
		let try_exec = || {
			let schedule = T::Schedule::get();
			let InstantiateInput { salt, .. } = self;
			let CommonInput { origin: contract_origin, .. } = common;
			let origin = contract_origin.account_id()?;

			let executable = match self.code {
				WasmCode::Wasm(module) => module,
				WasmCode::CodeHash(code_hash) => WasmBlob::from_storage(code_hash, &mut gas_meter)?,
			};

			let contract_origin = Origin::from_account_id(origin.clone());
			let mut storage_meter =
				StorageMeter::new(&contract_origin, common.storage_deposit_limit, common.value)?;
			let CommonInput { value, data, debug_message, .. } = common;
			let result = ExecStack::<T, WasmBlob<T>>::run_instantiate(
				origin.clone(),
				executable,
				&mut gas_meter,
				&mut storage_meter,
				&schedule,
				value,
				data.clone(),
				&salt,
				debug_message,
			);

			storage_deposit = storage_meter.try_into_deposit(&contract_origin)?;
			result
		};
		InternalOutput { result: try_exec(), gas_meter, storage_deposit }
	}

	fn ensure_origin(&self, origin: Origin<T>) -> Result<(), DispatchError> {
		match origin {
			Origin::Signed(_) => Ok(()),
			Origin::Root => Err(DispatchError::RootNotAllowed),
		}
	}
}
```

**File:** prdoc/1.9.0/pr_3377.prdoc (L1-14)
```text
# Schema: Polkadot SDK PRDoc Schema (prdoc) v1.0.0
# See doc at https://raw.githubusercontent.com/paritytech/polkadot-sdk/master/prdoc/schema_user.json

title: Permissioned contract deployment

doc:
  - audience: Runtime Dev
    description: |
      This PR introduces two new config types that specify the origins allowed to
      upload and instantiate contract code. However, this check is not enforced when
      a contract instantiates another contract.

crates: 
- name: pallet-contracts
```

**File:** substrate/frame/contracts/src/tests.rs (L4385-4416)
```rust
#[test]
fn only_instantiation_origin_can_instantiate() {
	let (code, code_hash) = compile_module::<Test>("dummy").unwrap();
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

**File:** substrate/frame/contracts/src/exec.rs (L2723-2799)
```rust
	#[test]
	fn instantiation_from_contract() {
		let dummy_ch = MockLoader::insert(Call, |_, _| exec_success());
		let instantiated_contract_address = Rc::new(RefCell::new(None::<AccountIdOf<Test>>));
		let instantiator_ch = MockLoader::insert(Call, {
			let instantiated_contract_address = Rc::clone(&instantiated_contract_address);
			move |ctx, _| {
				// Instantiate a contract and save it's address in `instantiated_contract_address`.
				let (address, output) = ctx
					.ext
					.instantiate(
						Weight::zero(),
						BalanceOf::<Test>::zero(),
						dummy_ch,
						<Test as Config>::Currency::minimum_balance(),
						vec![],
						&[48, 49, 50],
					)
					.unwrap();

				*instantiated_contract_address.borrow_mut() = address.into();
				Ok(output)
			}
		});

		ExtBuilder::default()
			.with_code_hashes(MockLoader::code_hashes())
			.existential_deposit(15)
			.build()
			.execute_with(|| {
				let schedule = <Test as Config>::Schedule::get();
				let min_balance = <Test as Config>::Currency::minimum_balance();
				set_balance(&ALICE, min_balance * 100);
				place_contract(&BOB, instantiator_ch);
				let contract_origin = Origin::from_account_id(ALICE);
				let mut storage_meter = storage::meter::Meter::new(
					&contract_origin,
					Some(min_balance * 10),
					min_balance * 10,
				)
				.unwrap();

				assert_matches!(
					MockStack::run_call(
						contract_origin,
						BOB,
						&mut GasMeter::<Test>::new(GAS_LIMIT),
						&mut storage_meter,
						&schedule,
						min_balance * 10,
						vec![],
						None,
						Determinism::Enforced,
					),
					Ok(_)
				);

				let instantiated_contract_address =
					instantiated_contract_address.borrow().as_ref().unwrap().clone();

				// Check that the newly created account has the expected code hash and
				// there are instantiation event.
				assert_eq!(
					ContractInfo::<Test>::load_code_hash(&instantiated_contract_address).unwrap(),
					dummy_ch
				);
				assert_eq!(
					&events(),
					&[
						Event::Instantiated {
							deployer: BOB,
							contract: instantiated_contract_address
						},
						Event::Called { caller: Origin::from_account_id(ALICE), contract: BOB },
					]
				);
			});
```

**File:** substrate/frame/contracts/fixtures/contracts/create_storage_and_instantiate.rs (L1-58)
```rust
// This file is part of Substrate.

// Copyright (C) Parity Technologies (UK) Ltd.
// SPDX-License-Identifier: Apache-2.0

// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
// 	http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

//! This instantiates another contract and passes some input to its constructor.
#![no_std]
#![no_main]

use common::input;
use uapi::{HostFn, HostFnImpl as api};

#[no_mangle]
#[polkavm_derive::polkavm_export]
pub extern "C" fn deploy() {}

#[no_mangle]
#[polkavm_derive::polkavm_export]
pub extern "C" fn call() {
	input!(
		input: [u8; 4],
		code_hash: [u8; 32],
		deposit_limit: [u8; 8],
	);

	let value = 10_000u64.to_le_bytes();
	let salt = [0u8; 0];
	let mut address = [0u8; 32];
	let address = &mut &mut address[..];

	api::instantiate_v2(
		code_hash,
		0u64, // How much ref_time weight to devote for the execution. 0 = all.
		0u64, // How much proof_size weight to devote for the execution. 0 = all.
		Some(deposit_limit),
		&value,
		input,
		Some(address),
		None,
		&salt,
	)
	.unwrap();

	// Return the deployed contract address.
	api::return_value(uapi::ReturnFlags::empty(), address);
}
```
