## Analysis

The external report describes `approvedHashes[user][hash]` in `Community.sol`/`Project.sol`: once a user signs and submits an approval hash, there is no function to invalidate it, so a stale or mistakenly-signed approval remains permanently exploitable by anyone holding it.

The direct local analog is the EIP-2612 `permit` pallet added for the asset precompiles, in `substrate/frame/assets/precompiles/src/permit.rs`. Here `Nonces<T>` is the on-chain state that determines whether an off-chain-signed `Permit(owner, spender, value, nonce, deadline)` message is still redeemable, and the *only* code path that advances the nonce is `use_permit`: [1](#0-0) 

The normal, non-signature `approve()` entry point in the precompile dispatcher never touches `Nonces`: [2](#0-1) 

### Title
Owner cannot invalidate a previously signed EIP-2612 `permit` via a plain `approve()` call, allowing a stale/leaked signature to silently reinstate an unwanted allowance - (File: `substrate/frame/assets/precompiles/src/permit.rs`)

### Summary
`Nonces<T>` (keyed by `(verifying_contract, owner)`) is the only replay guard for a signed `permit`. It is incremented **exclusively** inside `use_permit`, called from `permit()` in `lib.rs`. The regular `approve()`/`transferFrom` flow that a user would call to "revoke" or change an allowance never mutates `Nonces`, so a signed-but-not-yet-submitted permit stays valid at the *same* nonce indefinitely (bounded only by `deadline`).

### Finding Description
Analogous to `approvedHashes` in the external report, once an owner has produced a valid EIP-712 `Permit` signature for a `spender`/`value`, that signature remains redeemable by anyone in possession of it until its `deadline` expires or the exact nonce is consumed by another `use_permit` call. There is no `revokeNonce`/`invalidateNonce`/`incrementNonce` extrinsic exposed to the account owner, and the ordinary `IERC20Calls::approve` path deliberately (and correctly, per ERC-20) does not go through `permit::Pallet::use_permit`, so it leaves `Nonces` untouched: [3](#0-2) 

Consequences: if an owner signs a permit for a spender, then reconsiders (e.g., realizes the dApp/spender is malicious, or wants to lower/zero the allowance) and calls `approve(spender, 0)` on-chain to protect their funds, the previously signed permit is *still valid* at that same nonce. Anyone still holding the old signature (the spender, a relayer, or an observer who saw it broadcast/mempool) can later submit `permit()` with the original `value`, reinstating the exact allowance the owner explicitly tried to cancel, and then drain the funds via `transferFrom`.

### Impact Explanation
This directly maps to "unauthorized execution or origin escalation" and enables "theft ... or unbacked mint/unlock" from the impact gate: the spender regains an ERC‑20 allowance the token owner had actively revoked through the canonical on-chain revoke path (`approve(0)`), and can then call `transferFrom` to move the owner's assets without further owner consent. Because assets precompiles wrap `pallet-assets` balances exposed through `pallet-revive`, a successful reinstated-permit drain is real fund loss, not merely an off-chain semantic quibble as it was disputed to be in the original Solidity report.

### Likelihood Explanation
Requires no privileged actor: any unprivileged holder of a previously-signed, still-undelivered `permit` payload (a common relayer/dApp integration pattern for "gasless approvals") can resubmit it at any time before `deadline`. The owner's use of the intended revoke mechanism (`approve(spender, 0)`) gives no guarantee of protection because it never advances `Nonces`, so the race is always available to whoever holds the signature, not merely a front-run window.

### Recommendation
Add a permissioned, owner-only extrinsic/precompile call (e.g. `invalidate_nonce` / `increment_nonce`) that lets the signer bump their own `Nonces<T>` entry for a given `verifying_contract`, invalidating all previously-issued signatures at that nonce. Additionally, consider having `do_cancel_approval`/`approve(0)` on the precompile side also bump the permit nonce for that `(verifying_contract, owner)` pair so an explicit on-chain revoke cannot be undone by replaying an older signed permit.

### Proof of Concept
1. Owner signs `Permit(owner, spender=S, value=100, nonce=0, deadline=T)` off-chain and shares it with `S` (e.g., a dApp) to get a gasless approval.
2. Before `S` submits it, owner discovers `S` is malicious and calls `IERC20.approve(S, 0)` on-chain — `pallet_assets::Pallet::do_cancel_approval` clears the allowance, but `permit::Nonces` remains at `0` (see `substrate/frame/assets/precompiles/src/lib.rs:352-419`, no interaction with `permit::Pallet`).
3. `S` (or anyone who saw the signature) submits the stored `permit(owner, S, 100, deadline=T, v, r, s)` call to the precompile. `permit::Pallet::use_permit` re-validates against the still-unconsumed `nonce=0` and succeeds (`substrate/frame/assets/precompiles/src/permit.rs:374-403`), re-instating the 100-token allowance and incrementing the nonce only now.
4. `S` calls `transferFrom(owner, S, 100)`, draining funds the owner explicitly tried to revoke. [4](#0-3)

### Citations

**File:** substrate/frame/assets/precompiles/src/permit.rs (L374-403)
```rust
		pub fn use_permit(
			verifying_contract: &H160,
			name: &[u8],
			owner: &H160,
			spender: &H160,
			value: &[u8; 32],
			deadline: &[u8; 32],
			v: u8,
			r: &[u8; 32],
			s: &[u8; 32],
		) -> Result<(), Error<T>> {
			// Verify the permit first
			Self::do_verify_permit(
				verifying_contract,
				name,
				owner,
				spender,
				value,
				deadline,
				v,
				r,
				s,
			)?;

			// Consume the permit by incrementing the nonce
			// This prevents the same permit from being used again
			Self::increment_nonce(verifying_contract, owner)?;

			Ok(())
		}
```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L147-207)
```rust
impl<Runtime, PrecompileConfig, Instance: 'static> Precompile
	for ERC20<Runtime, PrecompileConfig, Instance>
where
	PrecompileConfig: AssetPrecompileConfig,
	Runtime: crate::Config<Instance> + pallet_revive::Config + permit::Config,
	<<PrecompileConfig as AssetPrecompileConfig>::AssetIdExtractor as AssetIdExtractor>::AssetId:
		Into<<Runtime as Config<Instance>>::AssetId>,
	Call<Runtime, Instance>: Into<<Runtime as pallet_revive::Config>::RuntimeCall>,
	alloy::primitives::U256: TryInto<<Runtime as Config<Instance>>::Balance>,
	alloy::primitives::U256: TryFrom<<Runtime as Config<Instance>>::Balance>,
{
	type T = Runtime;
	type Interface = IERC20::IERC20Calls;
	const MATCHER: AddressMatcher = PrecompileConfig::MATCHER;
	const HAS_CONTRACT_INFO: bool = false;

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
```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L352-419)
```rust
	fn approve(
		asset_id: <Runtime as Config<Instance>>::AssetId,
		call: &IERC20::approveCall,
		env: &mut impl Ext<T = Runtime>,
	) -> Result<Vec<u8>, Error> {
		use frame_support::traits::fungibles::approvals::Inspect as ApprovalsInspect;

		// Reserve worst-case gas upfront, then refund the unused portion.
		let worst_case = <Runtime as Config<Instance>>::WeightInfo::allowance()
			.saturating_add(<Runtime as Config<Instance>>::WeightInfo::cancel_approval())
			.saturating_add(<Runtime as Config<Instance>>::WeightInfo::approve_transfer());
		let charged = env.charge(worst_case)?;

		let owner = Self::caller(env)?;
		let owner_account =
			<Runtime as pallet_revive::Config>::AddressMapper::to_account_id(&owner);
		let spender: H160 = call.spender.into_array().into();
		let spender_account = env.to_account_id(&spender);
		// Saturate: `type(uint256).max` is the standard "infinite allowance" idiom and must
		// not revert at the conversion boundary.
		let new_amount: <Runtime as Config<Instance>>::Balance = call.value.unique_saturated_into();

		let current = pallet_assets::Pallet::<Runtime, Instance>::allowance(
			asset_id.clone(),
			&owner_account,
			&spender_account,
		);

		let actual_weight;
		if new_amount.is_zero() {
			if !current.is_zero() {
				// Revoke: use the pallet's cancel logic to remove the approval and
				// unreserve the deposit.
				pallet_assets::Pallet::<Runtime, Instance>::do_cancel_approval(
					&asset_id,
					&owner_account,
					&spender_account,
				)?;
				actual_weight = <Runtime as Config<Instance>>::WeightInfo::allowance()
					.saturating_add(<Runtime as Config<Instance>>::WeightInfo::cancel_approval());
			} else {
				// 0→0 no-op: only the allowance read was needed.
				actual_weight = <Runtime as Config<Instance>>::WeightInfo::allowance();
			}
		} else {
			// If there's an existing non-zero allowance, cancel it first so we
			// overwrite (not accumulate) — matching ERC-20 spec semantics.
			// NOTE: This does not mitigate the well-known ERC-20 approve front-running
			// race condition. Callers concerned about this should approve to 0 first,
			// or use increaseAllowance/decreaseAllowance if available.
			if !current.is_zero() {
				pallet_assets::Pallet::<Runtime, Instance>::do_cancel_approval(
					&asset_id,
					&owner_account,
					&spender_account,
				)?;
				actual_weight = worst_case;
			} else {
				actual_weight = <Runtime as Config<Instance>>::WeightInfo::allowance()
					.saturating_add(<Runtime as Config<Instance>>::WeightInfo::approve_transfer());
			}
			pallet_assets::Pallet::<Runtime, Instance>::do_approve_transfer(
				asset_id,
				&owner_account,
				&spender_account,
				new_amount,
			)?;
		}
```

**File:** substrate/frame/assets/precompiles/src/permit_precompile_tests.rs (L323-415)
```rust
#[test_case(PRECOMPILE_ADDRESS_PREFIX)]
#[test_case(PRECOMPILE_ADDRESS_PREFIX_FOREIGN)]
fn permit_set_and_revoke(asset_index: u16) {
	use frame_support::traits::fungibles::approvals::Inspect;

	new_test_ext().execute_with(|| {
		let setup = permit_setup(asset_index);
		let deposit: u128 = <Test as pallet_assets::Config>::ApprovalDeposit::get();

		assert_eq!(
			permit::Pallet::<Test>::nonce(&setup.asset_addr, &HARDHAT_ACCOUNT_0),
			U256::zero()
		);

		// 0 → 100: fresh approval.
		permit_sign_and_call(
			setup.submitter,
			setup.asset_addr,
			setup.spender_addr,
			AlloyU256::from(100),
			setup.deadline,
		);
		assert_eq!(
			Assets::allowance(setup.asset_id, &setup.owner_account, &setup.spender_account),
			100
		);
		assert_eq!(Balances::reserved_balance(&setup.owner_account), deposit);
		assert_eq!(
			permit::Pallet::<Test>::nonce(&setup.asset_addr, &HARDHAT_ACCOUNT_0),
			U256::one()
		);
		assert_contract_event(
			setup.asset_addr,
			IERC20Events::Approval(IERC20::Approval {
				owner: HARDHAT_ACCOUNT_0.0.into(),
				spender: setup.spender_addr.0.into(),
				value: AlloyU256::from(100),
			}),
		);

		// 100 → 0: revoke. ERC-20 conformance: must fire Approval(_, _, 0).
		permit_sign_and_call(
			setup.submitter,
			setup.asset_addr,
			setup.spender_addr,
			AlloyU256::from(0),
			setup.deadline,
		);
		assert_eq!(
			Assets::allowance(setup.asset_id, &setup.owner_account, &setup.spender_account),
			0
		);
		assert_eq!(Balances::reserved_balance(&setup.owner_account), 0);
		assert_eq!(
			permit::Pallet::<Test>::nonce(&setup.asset_addr, &HARDHAT_ACCOUNT_0),
			U256::from(2)
		);
		assert_contract_event(
			setup.asset_addr,
			IERC20Events::Approval(IERC20::Approval {
				owner: HARDHAT_ACCOUNT_0.0.into(),
				spender: setup.spender_addr.0.into(),
				value: AlloyU256::from(0),
			}),
		);

		// 0 → 50: fresh approval again.
		permit_sign_and_call(
			setup.submitter,
			setup.asset_addr,
			setup.spender_addr,
			AlloyU256::from(50),
			setup.deadline,
		);
		assert_eq!(
			Assets::allowance(setup.asset_id, &setup.owner_account, &setup.spender_account),
			50
		);
		assert_eq!(Balances::reserved_balance(&setup.owner_account), deposit);
		assert_eq!(
			permit::Pallet::<Test>::nonce(&setup.asset_addr, &HARDHAT_ACCOUNT_0),
			U256::from(3)
		);
		assert_contract_event(
			setup.asset_addr,
			IERC20Events::Approval(IERC20::Approval {
				owner: HARDHAT_ACCOUNT_0.0.into(),
				spender: setup.spender_addr.0.into(),
				value: AlloyU256::from(50),
			}),
		);
	});
}
```
