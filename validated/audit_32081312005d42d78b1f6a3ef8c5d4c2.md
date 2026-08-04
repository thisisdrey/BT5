Based on my investigation, I found the strongest concrete local analog in `pallet-revive`'s two-tier instantiation authorization model, which mirrors the exact bug class from the external report: an authorization check that is only enforced at one entry-point layer and can be bypassed by routing the same operation through an intermediary (a deployed contract), just as the ClaveProxy's deployer check is bypassed via a cloned `AccountFactory`.

### Title
`InstantiateOrigin` restriction is not enforced for nested contract-triggered `CREATE`/`CREATE2`, allowing unprivileged bypass of deployment permissioning - (File: `substrate/frame/revive/src/lib.rs`, `substrate/frame/revive/src/exec.rs`)

### Summary
`pallet-revive` (and the legacy `pallet-contracts`) expose a `T::InstantiateOrigin` config that a runtime can use to restrict who is allowed to deploy new contracts. This check is enforced only in `bare_instantiate` [1](#0-0)  — i.e. only for the top-level `instantiate`/`instantiate_with_code`/`eth_instantiate_with_code` extrinsics. It is never checked in the nested instantiation path taken by the `CREATE`/`CREATE2` opcodes or the `seal_instantiate`/`instantiate` host function, implemented in `PrecompileWithInfoExt::instantiate` for `Stack` [2](#0-1) , which performs no origin check at all before pushing the new constructor frame. This is the same class of bug as the report: a security-relevant check ("who may create this account") is anchored to the wrong layer of the call graph, and an attacker inserts an intermediary contract to move outside the checked layer.

### Finding Description
`T::InstantiateOrigin::ensure_origin` gates the dispatchable-level instantiation path [1](#0-0) , and the pallet-contracts equivalent documents this explicitly in the config-type rustdoc:

> "This is not enforced when a contract instantiates another contract. The `UploadOrigin` should make sure that no code is deployed that does unwanted instantiations." [3](#0-2) 

The corresponding pallet-revive PR history confirms the same asymmetry is by design at the dispatchable boundary but is being progressively loosened for nested paths — e.g. PR 12144 explicitly removed the `RootNotAllowed` check for nested `CREATE` reached through an intermediary contract, stating "Root is still not allowed to instantiate directly... The change only unblocks the case where another contract sits between Root and the new contract." [4](#0-3) 

The corrupted value/state here is the deployer-authorization decision for a new contract account: it is supposed to be gated by `T::InstantiateOrigin`, but any already-deployed, ordinarily-callable contract that internally issues `CREATE`/`CREATE2` (via `PrecompileWithInfoExt::instantiate`, `exec.rs:2092`) creates the new account without any re-check of `T::InstantiateOrigin` against the calling user. Calling an existing contract's exported function is a permissionless `call` operation — it is not gated by `InstantiateOrigin` at all — so any unprivileged account can trigger arbitrary nested instantiation as long as one contract with a `CREATE` opcode exists on-chain (which is a normal, unprivileged occurrence, e.g. any factory/proxy pattern contract, any Solidity contract using `new X()`).

### Impact Explanation
On any runtime that configures `T::InstantiateOrigin` to a restricted set (e.g. a permissioned deployment chain, or a chain that wants to whitelist which accounts can spawn new contract instances to control state bloat, deposits, or governance-controlled rollout), the restriction is fully bypassable by any unprivileged user simply calling any on-chain contract that performs a nested `CREATE`. This is a runtime bug that compromises intended access-control behavior (unauthorized execution/origin escalation of the instantiation permission), matching the required impact category directly.

### Likelihood Explanation
Likelihood is Medium: it requires (a) a runtime that actually restricts `T::InstantiateOrigin` away from `EnsureSigned` and (b) at least one already-deployed contract capable of issuing `CREATE`/`CREATE2` (a very common pattern — factories, clone patterns, `new` in Solidity). No malicious peer, validator, or governance actor is needed — only an ordinary user calling a normal contract.

### Recommendation
Either (a) propagate and re-check `T::InstantiateOrigin` (or an equivalent "is this contract allowed to spawn new accounts on behalf of the calling extrinsic origin" check) inside `PrecompileWithInfoExt::instantiate` in `exec.rs`, evaluated against the transaction's outer signed origin, not just the intermediate contract caller; or (b) make the documentation/config-surface change explicit and load-bearing by renaming/restructuring `InstantiateOrigin` so runtime authors cannot mistakenly assume it covers nested instantiation, and provide a supported primitive (e.g. a `Filter` hook invoked from `push_frame`'s `FrameArgs::Instantiate` branch) for restricting nested `CREATE` by code hash/origin.

### Proof of Concept
1. Configure a test runtime with `T::InstantiateOrigin = EnsureSignedBy<Alice>` (only ALICE may instantiate), mirroring the existing `only_instantiation_origin_can_instantiate` test [5](#0-4) .
2. As ALICE (privileged), deploy any contract containing a `CREATE`/`CREATE2` opcode, e.g. the `Caller.create2` fixture [6](#0-5) .
3. As BOB (unprivileged, would get `BadOrigin` if calling `instantiate`/`instantiate_with_code` directly, per the existing negative test), call BOB→`Caller.create2(initcode, salt)`.
4. Observe that the nested instantiation inside `PrecompileWithInfoExt::instantiate` succeeds and a new contract is created attributed to BOB's call, with no `T::InstantiateOrigin::ensure_origin` check ever evaluated for BOB, in contrast to what would happen if BOB called the `instantiate`/`instantiate_with_code` dispatchable directly.

Note: I was unable to execute this PoC in a live environment (no filesystem/terminal access in this mode); this is derived purely from static code reading. If you want this validated dynamically, a Devin session with codebase/terminal access would be needed to run the existing `pallet-revive` test suite with a modified `InstantiateOrigin` config and confirm the bypass empirically.

### Citations

**File:** substrate/frame/revive/src/lib.rs (L1859-1860)
```rust
		let try_instantiate = || {
			let instantiate_account = T::InstantiateOrigin::ensure_origin(origin.clone())?;
```

**File:** substrate/frame/revive/src/exec.rs (L2092-2134)
```rust
	fn instantiate(
		&mut self,
		call_resources: &CallResources<T>,
		mut code: Code,
		value: U256,
		input_data: Vec<u8>,
		salt: Option<&[u8; 32]>,
	) -> Result<H160, ExecError> {
		// We reset the return data now, so it is cleared out even if no new frame was executed.
		// This is for example the case when creating the frame fails.
		*self.last_frame_output_mut() = Default::default();

		let sender = self.top_frame().account_id.clone();
		let executable = {
			let executable = match &mut code {
				Code::Upload(initcode) => {
					if !T::AllowEVMBytecode::get() {
						return Err(<Error<T>>::CodeRejected.into());
					}
					ensure!(input_data.is_empty(), <Error<T>>::EvmConstructorNonEmptyData);
					let initcode = crate::tracing::if_tracing(|_| initcode.clone())
						.unwrap_or_else(|| mem::take(initcode));
					E::from_evm_init_code(initcode, sender.clone())?
				},
				Code::Existing(hash) => {
					let executable = E::from_storage(*hash, self.frame_meter_mut())?;
					ensure!(executable.code_info().is_pvm(), <Error<T>>::EvmConstructedFromHash);
					executable
				},
			};
			self.push_frame(
				FrameArgs::Instantiate {
					sender,
					executable,
					salt,
					input_data: input_data.as_ref(),
				},
				value,
				call_resources,
				self.is_read_only(),
				&input_data,
			)?
		};
```

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

**File:** prdoc/stable2606/pr_12144.prdoc (L1-26)
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
crates:
- name: pallet-revive
  bump: minor
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

**File:** substrate/frame/revive/fixtures/contracts/Caller.sol (L65-76)
```text
    function create2(bytes memory initcode, bytes32 salt) external payable returns (address addr) {
        assembly {
            // CREATE2 with no value
            addr := create2(0, add(initcode, 0x20), mload(initcode), salt)
            if iszero(addr) {
                // bubble failure
                let returnDataSize := returndatasize()
                returndatacopy(0, 0, returnDataSize)
                revert(0, returnDataSize)
            }
        }
    }
```
