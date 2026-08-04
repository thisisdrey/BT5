## Finding [1](#0-0) [2](#0-1) [3](#0-2) 

### Title
CREATE1 (`salt=None`) contract-address derivation in `pallet-revive` depends only on the deploying contract's nonce, letting any un-related caller collide with a pre-funded, reorg-orphaned child-contract address - (File: `substrate/frame/revive/src/address.rs`, `substrate/frame/revive/src/exec.rs`)

### Summary
`pallet-revive` implements Ethereum's `CREATE`/`CREATE1` opcode semantics for contract instantiation when no salt is supplied. The resulting address is `keccak256(rlp(deployer, nonce))`, where `deployer` is the calling *contract's own account*, not the external transaction sender that triggered the call chain, and `nonce` is that contract's own on-chain nonce, incremented every time it instantiates a child. This is functionally identical to the `VaultBoosterFactory.createVaultBooster()` root cause in the external report: any Solidity/EVM-style factory contract compiled to PolkaVM/PVM and deployed on a `pallet-revive`-enabled chain that internally performs a plain `new Child()` (no salt) produces a child address that depends solely on the factory's nonce and is completely independent of `msg.sender`/the extrinsic's signer. Anyone who calls the factory's public creation function advances the same nonce counter and can be made to land at the exact address a previous caller (or the same caller in an earlier, now-orphaned block) already computed and funded.

### Finding Description
`create1` is defined generically without any binding to the caller/tx-signer identity:

```rust
pub fn create1(deployer: &H160, nonce: u64) -> H160 {
    let mut list = rlp::RlpStream::new_list(2);
    list.append(&deployer.as_bytes());
    list.append(&nonce);
    let hash = keccak_256(&list.out());
    H160::from_slice(&hash[12..])
}
``` [1](#0-0) 

In `Stack::new` / `new_frame`, when a top-level extrinsic instantiates without a salt, `deployer` is the *origin's* address and nonce; but when a *contract* instantiates a child (the on-chain `CREATE` opcode path used by Solidity factories), `deployer` is that contract's own address and the nonce comes from `System::account_nonce` of the contract itself, which is bumped unconditionally on every constructor invocation:

```rust
FrameArgs::Instantiate { sender, executable, salt, input_data } => {
    let deployer = T::AddressMapper::to_address(&sender);
    let account_nonce = <System<T>>::account_nonce(&sender);
    let address = if let Some(salt) = salt {
        address::create2(&deployer, executable.code(), input_data, salt)
    } else {
        address::create1(&deployer, ... account_nonce ...)
    };
``` [4](#0-3) 

and

```rust
// Contracts nonce starts at 1
<System<T>>::inc_account_nonce(account_id);
if bump_nonce || !is_first_frame {
    <System<T>>::inc_account_nonce(caller.account_id()?);
}
``` [5](#0-4) 

Because the factory's nonce is the sole address determinant, the child address is completely predictable and — critically — reproducible by *anyone* who can trigger the factory's public entry point, exactly matching the `VaultBoosterFactory` bug: address derivation depends only on a shared counter (`msg.sender`/factory nonce), not on a per-call `salt` that binds the true initiator.

**Reorg exploitation path (equivalent to the reported EVM scenario):** Substrate/PolkaVM chains produce best-block forks before finality (e.g., under network partition, equivocating block authors, or simple fork-choice re-selection), independent of any single malicious validator controlling the outcome. If:
1. Alice's extrinsic calls `Factory.createChild()` in block B (nonce N), producing address A = `create1(factory, N)`, and Alice sends funds to A anticipating the deployment succeeding,
2. block B is not yet finalized and gets reorged out before finality,
3. any other account (Bob, or even a re-ordered version of Alice's own batched extrinsics) calls `Factory.createChild()` in the new canonical chain using the same nonce slot N,

then the contract that lands at address A in the canonical chain is the one from Bob's call (with Bob-controlled constructor arguments / ownership), while Alice's pre-sent funds are already there. Existing guards (`inc_account_nonce`, `CheckNonce` transaction extension) only protect the account's own extrinsic ordering; they do nothing to bind the *factory's* CREATE1 address to the identity of whoever actually calls the factory's function, which is exactly the gap the report calls out.

### Impact Explanation
Funds sent to a `create1`-derived factory-child address in anticipation of a specific deployment can be redirected to, or claimed by, an unrelated party after a shallow, pre-finality reorg — a direct theft/fund-loss scenario matching the "theft or unbacked mint or unlock" and "duplicate settlement or payout" impact classes for `pallet-revive`-hosted contracts, which is the intended Ethereum-compatibility layer for Substrate chains.

### Likelihood Explanation
This requires only an ordinary, permissionless call to a factory contract's public function plus a naturally occurring shallow fork/best-chain reselection before finality — no malicious validator, collator, or governance actor is required, matching the accepted primitive from the original report. The likelihood scales with how commonly deployed Solidity/EVM contracts on `pallet-revive` chains use plain `new Child()` (i.e., omit salt) for factory patterns, which PR `pr_5556`/`pr_5701` explicitly enabled ("Make salt optional ... to allow clients to use CREATE1 semantics").

### Recommendation
For factory-style contract patterns on `pallet-revive`, document/encourage (or enforce at the tooling level) `CREATE2` usage with a salt that binds `msg.sender`/tx-origin, consistent with the report's recommended mitigation. Consider providing safer factory scaffolding/precompiles that always require an explicit salt bound to the calling context to prevent nonce-only collision across reorgs.

### Proof of Concept
1. Deploy `Factory` (PVM/EVM bytecode) whose constructor performs `new Child(msg.sender)` internally (compiles to `CREATE`, no salt) via `instantiate_with_code`/`eth_instantiate_with_code`.
2. Compute `addr = create1(factory_address, factory_nonce)` as done in the existing test `create1_address_from_extrinsic` at [6](#0-5) .
3. Send an extrinsic calling `Factory.createChild()` from account Alice in block N; before finalization, send value to `addr`.
4. Simulate/trigger a shallow reorg (drop block N pre-finality) and have account Bob call `Factory.createChild()` in the replacement block, consuming the same factory nonce slot.
5. Observe that the resulting child contract at `addr` is Bob's, while Alice's previously sent value remains at `addr`, now controlled by Bob's deployment.

### Citations

**File:** substrate/frame/revive/src/address.rs (L263-270)
```rust
/// Determine the address of a contract using CREATE semantics.
pub fn create1(deployer: &H160, nonce: u64) -> H160 {
	let mut list = rlp::RlpStream::new_list(2);
	list.append(&deployer.as_bytes());
	list.append(&nonce);
	let hash = keccak_256(&list.out());
	H160::from_slice(&hash[12..])
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

**File:** substrate/frame/revive/src/exec.rs (L1355-1363)
```rust

				// Contracts nonce starts at 1
				<System<T>>::inc_account_nonce(account_id);

				if bump_nonce || !is_first_frame {
					// Needs to be incremented before calling into the code so that it is visible
					// in case of recursion.
					<System<T>>::inc_account_nonce(caller.account_id()?);
				}
```

**File:** substrate/frame/revive/src/tests/pvm.rs (L304-329)
```rust
#[test]
fn create1_address_from_extrinsic() {
	let (binary, code_hash) = compile_module("dummy").unwrap();

	ExtBuilder::default().existential_deposit(1).build().execute_with(|| {
		let _ = <Test as Config>::Currency::set_balance(&ALICE, 1_000_000);

		assert_ok!(Contracts::upload_code(
			RuntimeOrigin::signed(ALICE),
			binary.clone(),
			deposit_limit::<Test>(),
		));

		assert_eq!(System::account_nonce(&ALICE), 0);
		System::inc_account_nonce(&ALICE);

		for nonce in 1..3 {
			let Contract { addr, .. } = builder::bare_instantiate(Code::Existing(code_hash))
				.salt(None)
				.build_and_unwrap_contract();
			assert!(AccountInfoOf::<Test>::contains_key(&addr));
			assert_eq!(
				addr,
				create1(&<Test as Config>::AddressMapper::to_address(&ALICE), nonce - 1)
			);
		}
```
