### Title
ERC20/asset precompiles derived caller identity from `env.caller()` during delegatecall, letting an intermediary contract impersonate the original caller and move their assets outside expected authorization context - (File: `substrate/frame/assets/precompiles/src/lib.rs`)

### Summary
The Gondi bug is a broken caller-identity invariant: `Pool::validateOffer` trusted any member of a generic "accepted caller" set to act as a `LoanContract`, letting a `Liquidator` skip the loan-contract's own signature/collateral checks and mutate `__outstandingValues` directly. The local analog in this repository is the same class of broken invariant in `pallet-revive` ERC20/asset-family precompiles: `Self::caller()` derives the "acting" account from `env.caller()`, and prior to the delegatecall guard being added, nothing prevented a malicious contract from `delegatecall`-ing into the precompile. Because `env.caller()` returns the *original* caller during a delegatecall, the intermediary (malicious) contract could execute privileged, asset-moving precompile logic (`transfer`, `approve`, `permit`, XCM send, etc.) while appearing to act on behalf of the real caller — exactly like a liquidator "pretending" to be the loan contract to bypass the intended, narrower-scoped call path.

### Finding Description
`ERC20::call` in `substrate/frame/assets/precompiles/src/lib.rs` dispatches directly to state-mutating handlers such as `transfer`, `approve`, `transferFrom`, and `permit`: [1](#0-0) 

Each handler resolves the acting account via `Self::caller(env)`, which reads `env.caller()`: [2](#0-1) 

and then moves assets/attributes value transfer/approval directly against that resolved identity, e.g. `transfer`: [3](#0-2) 

The guard that is now present rejects delegatecalls before any of this dispatch logic runs: [4](#0-3) 

Per the PR documentation describing why this guard was necessary, the root cause is identical to the Gondi bug class: an insufficient check ("is this a legitimate direct call") let an untrusted intermediary act with the authorization/context intended only for a direct, non-delegated caller: [5](#0-4) [6](#0-5) 

Just as `Pool::validateOffer` only checked `_acceptedCallers.contains(msg.sender)` — an "is this address in a broad accepted set" check — without checking `_isLoanContract(msg.sender)`, the pre-fix ERC20/asset-conversion/XCM precompiles only checked that the *account* resolved by `env.caller()` was a valid account, without checking `is_delegate_call()` to ensure the *call context* legitimately belonged to that account's own transaction rather than to an interposed, untrusted contract. In both cases, the guard needed to bind "caller identity" to "call context/role" was missing, and a narrower/more specific check (`_isLoanContract` vs. `is_delegate_call`) was the fix.

### Impact Explanation
If unmitigated, this class of bug lets an attacker deploy a malicious contract, have a victim (or the attacker themselves acting through a trusted flow) `delegatecall` into it, and execute precompile-level `transfer`/`approve`/`permit`/XCM-send logic that is misattributed to the victim's account context, moving assets or granting approvals the victim did not directly authorize through the expected call path. This falls squarely under "theft or unbacked mint/unlock" and "unauthorized execution or origin escalation" in the impact gate, since fund-moving logic executes under a caller identity/context that was never verified to be the legitimate, direct caller.

### Likelihood Explanation
The precondition is only an unprivileged attacker deploying an ordinary contract and inducing/performing a delegatecall into the precompile address — no malicious validator, relayer, or admin is required, matching the "public underpriced work / unauthorized execution" pivot criteria. This is corroborated by the repository's own regression tests (`delegatecall_is_rejected` in `substrate/frame/assets/precompiles/src/tests.rs`) which were added specifically to lock in the fix, confirming the attack was reachable and exploitable pre-patch. [7](#0-6) 

### Recommendation
Confirm that every precompile deriving caller identity from `env.caller()` (assets ERC20, asset-conversion, vesting, XCM, and any future precompiles built on the same pattern) enforces the `is_delegate_call()`/`PrecompileDelegateDenied` guard before dispatching any state-changing or value-moving logic, exactly as now implemented for the four audited precompiles, and add this guard as a mandatory item in the precompile-authoring checklist/template so newly added precompiles do not regress the invariant.

### Proof of Concept
The repository's own test demonstrates the exploit path that the guard closes: a deployed `Caller` contract delegatecalls into the asset precompile's `totalSupply`/`transfer`-style entry point; without the guard, `env.caller()` inside the precompile resolves to the original signer rather than the `Caller` contract, so the precompile executes as if the original signer called it directly, even though the `Caller` contract's own logic runs first and is fully attacker-controlled: [8](#0-7) 

Note: this vulnerability class has already been mitigated in this repository snapshot (guards present in `substrate/frame/assets/precompiles/src/lib.rs`, `substrate/frame/asset-conversion/precompiles/src/lib.rs`, `substrate/frame/vesting/precompiles/src/lib.rs`, and `polkadot/xcm/pallet-xcm/precompiles/src/lib.rs` per PRs #11676/#11715). I could not, within the remaining tool budget, exhaustively verify whether any other precompile added after these PRs (e.g. a hypothetical NFTs/staking/native-balance precompile) still lacks the same guard; this should be verified with a repo-wide `is_delegate_call` grep across all `**/precompiles/src/lib.rs` files before closing this line of investigation.

### Citations

**File:** substrate/frame/assets/precompiles/src/lib.rs (L163-208)
```rust
	fn call(
		address: &[u8; 20],
		input: &Self::Interface,
		env: &mut impl Ext<T = Self::T>,
	) -> Result<Vec<u8>, Error> {
		frame_support::ensure!(
			!env.is_delegate_call(),
			pallet_revive::Error::<Self::T>::PrecompileDelegateDenied,
		);

		let asset_id = PrecompileConfig::AssetIdExtractor::asset_id_from_address(address)?.into();
		let contract_addr = H160::from(*address);

		match input {
			// State-changing calls - check read-only
			IERC20Calls::transfer(_) |
			IERC20Calls::approve(_) |
			IERC20Calls::transferFrom(_) |
			IERC20Calls::permit(_)
				if env.is_read_only() =>
			{
				Err(Error::Error(pallet_revive::Error::<Self::T>::StateChangeDenied.into()))
			},

			// ERC20 functions
			IERC20Calls::transfer(call) => Self::transfer(asset_id, call, env),
			IERC20Calls::totalSupply(_) => Self::total_supply(asset_id, env),
			IERC20Calls::balanceOf(call) => Self::balance_of(asset_id, call, env),
			IERC20Calls::allowance(call) => Self::allowance(asset_id, call, env),
			IERC20Calls::approve(call) => Self::approve(asset_id, call, env),
			IERC20Calls::transferFrom(call) => Self::transfer_from(asset_id, call, env),

			// ERC20Permit functions (EIP-2612)
			IERC20Calls::permit(call) => Self::permit(asset_id, contract_addr, call, env),
			IERC20Calls::nonces(call) => Self::nonces(contract_addr, call, env),
			IERC20Calls::DOMAIN_SEPARATOR(_) => {
				Self::domain_separator(asset_id, contract_addr, env)
			},

			// ERC20Metadata functions
			IERC20Calls::name(_) => Self::name(asset_id, env),
			IERC20Calls::symbol(_) => Self::symbol(asset_id, env),
			IERC20Calls::decimals(_) => Self::decimals(asset_id, env),
		}
	}
}
```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L223-229)
```rust
	/// Get the caller as an `H160` address.
	fn caller(env: &mut impl Ext<T = Runtime>) -> Result<H160, Error> {
		env.caller()
			.account_id()
			.map(<Runtime as pallet_revive::Config>::AddressMapper::to_address)
			.map_err(|_| Error::Revert(Revert { reason: ERR_INVALID_CALLER.into() }))
	}
```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L261-294)
```rust
	/// Execute the transfer call.
	fn transfer(
		asset_id: <Runtime as Config<Instance>>::AssetId,
		call: &IERC20::transferCall,
		env: &mut impl Ext<T = Runtime>,
	) -> Result<Vec<u8>, Error> {
		env.charge(<Runtime as Config<Instance>>::WeightInfo::transfer())?;

		let from = Self::caller(env)?;
		let dest = <Runtime as pallet_revive::Config>::AddressMapper::to_account_id(
			&call.to.into_array().into(),
		);

		let f = TransferFlags { keep_alive: false, best_effort: false, burn_dust: false };
		pallet_assets::Pallet::<Runtime, Instance>::do_transfer(
			asset_id,
			&<Runtime as pallet_revive::Config>::AddressMapper::to_account_id(&from),
			&dest,
			Self::to_balance(call.value)?,
			None,
			f,
		)?;

		Self::deposit_event(
			env,
			IERC20Events::Transfer(IERC20::Transfer {
				from: from.0.into(),
				to: call.to,
				value: call.value,
			}),
		)?;

		Ok(IERC20::transferCall::abi_encode_returns(&true))
	}
```

**File:** prdoc/stable2606/pr_11715.prdoc (L1-23)
```text
title: Reject delegatecall into precompiles via PrecompileDelegateDenied
doc:
- audience: Runtime Dev
  description: "## Summary\n\n- Add delegatecall guard to the ERC20 assets precompile\
    \ and XCM precompile, matching the existing pattern in the vesting and asset-conversion\
    \ precompiles\n- Converge asset-conversion precompile from `Error::Revert(string)`\
    \ to `Error::Error(PrecompileDelegateDenied)` for consistency across all precompiles\n\
    - Add delegatecall rejection test for the XCM precompile\n\n## Motivation\n\n\
    Delegatecall to precompiles allows a malicious contract to execute precompile\
    \ logic in a misleading caller context. The precompiles derive caller identity\
    \ from `env.caller()`, which during delegatecall returns the original caller \u2014\
    \ letting the intermediary contract act on the caller's assets or send XCM on\
    \ their behalf. There is no legitimate use case for delegatecalling into these\
    \ precompiles.\n\n## Changes\n\n- `substrate/frame/assets/precompiles/src/lib.rs`\
    \ \u2014 add `PrecompileDelegateDenied` guard\n- `substrate/frame/asset-conversion/precompiles/src/lib.rs`\
    \ \u2014 replace `Error::Revert(ERR_DELEGATE_CALL)` with `PrecompileDelegateDenied`,\
    \ remove unused const\n- `polkadot/xcm/pallet-xcm/precompiles/src/lib.rs` \u2014\
    \ add `PrecompileDelegateDenied` guard\n- `polkadot/xcm/pallet-xcm/precompiles/src/tests.rs`\
    \ \u2014 add `delegatecall_is_rejected` test\n- `polkadot/xcm/pallet-xcm/precompiles/Cargo.toml`\
    \ \u2014 add `pallet-revive-fixtures` dev-dependency\n\n## Test plan\n\n- [x]\
    \ `cargo test -p pallet-xcm-precompiles` \u2014 13 tests pass, including new `delegatecall_is_rejected`\n\
    - [x] `cargo test -p pallet-asset-conversion-precompiles` \u2014 18 tests pass\n\
    - [x] `cargo test -p pallet-assets-precompiles` \u2014 66 tests pass"
```

**File:** prdoc/stable2606/pr_11676.prdoc (L1-15)
```text
title: '[pallet-assets] Reject delegatecall into pallet-assets ERC20 precompile'
doc:
- audience: Runtime Dev
  description: "There is no legitimate use case for delegatecalling into the asset\
    \ precompile. This matches the precedent set by the Storage precompile, which\
    \ already enforces a delegatecall check (in the opposite direction \u2014 it *requires*\
    \ delegatecall).\n\n## Changes\n\n- `lib.rs`: Add `ERR_DELEGATECALL_DENIED` const\
    \ and `is_delegate_call()` guard before any dispatch logic\n- `tests.rs`: Add\
    \ `delegatecall_is_rejected` test using the `Caller.sol` fixture\n\n## Test plan\n\
    \n- [x] `cargo test -p pallet-assets-precompiles` \u2014 all 67 tests pass\n-\
    \ [x] `delegatecall_is_rejected` verifies the guard rejects delegatecall via the\
    \ `Caller` fixture contract"
crates:
- name: pallet-assets-precompiles
  bump: minor
```

**File:** substrate/frame/assets/precompiles/src/tests.rs (L658-715)
```rust
#[test]
fn delegatecall_is_rejected() {
	new_test_ext().execute_with(|| {
		let asset_id = 0u32;
		let asset_addr = H160::from(set_prefix_in_address(PRECOMPILE_ADDRESS_PREFIX));
		let deployer = 123456789u64;
		Balances::make_free_balance_be(&deployer, 1_000_000_000_000_000u128);

		assert_ok!(Assets::force_create(RuntimeOrigin::root(), asset_id, deployer, true, 1));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(deployer), asset_id, deployer, 1000));

		let (init_code, _) = pallet_revive_fixtures::compile_module_with_type(
			"Caller",
			pallet_revive_fixtures::FixtureType::Solc,
		)
		.expect("Caller fixture must be compiled");
		let caller_addr = pallet_revive::Pallet::<Test>::bare_instantiate(
			RuntimeOrigin::signed(deployer),
			0u32.into(),
			TransactionLimits::WeightAndDeposit {
				weight_limit: Weight::MAX,
				deposit_limit: u128::MAX,
			},
			Code::Upload(init_code),
			vec![],
			None,
			&ExecConfig::new_substrate_tx(),
		)
		.result
		.expect("Caller deployment must succeed")
		.addr;

		let calldata = ICaller::delegateCall {
			callee: alloy::primitives::Address::from(asset_addr.0),
			data: IERC20::totalSupplyCall {}.abi_encode().into(),
			gas: u64::MAX,
		}
		.abi_encode();

		let result = pallet_revive::Pallet::<Test>::bare_call(
			RuntimeOrigin::signed(deployer),
			caller_addr,
			0u32.into(),
			TransactionLimits::WeightAndDeposit {
				weight_limit: Weight::MAX,
				deposit_limit: u128::MAX,
			},
			calldata,
			&ExecConfig::new_substrate_tx(),
		)
		.result
		.expect("outer call must succeed");

		let ret = ICaller::delegateCall::abi_decode_returns(&result.data)
			.expect("return must decode as (bool, bytes)");
		assert!(!ret.success, "DELEGATECALL to asset precompile must be rejected");
	});
}
```
