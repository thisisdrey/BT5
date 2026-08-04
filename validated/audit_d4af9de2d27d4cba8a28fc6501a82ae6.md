## Analysis

The Solana report's core broken invariant is: **a value that is supposed to reflect a live on-chain parameter is instead computed from a hardcoded local constant, so the funds transferred/reserved fall short of what the destination system actually requires.**

The closest local analog to this class of bug is in the Identity migration flow between the relay chain and the People Chain parachain, implemented in `ToParachainIdentityReaper::calculate_remote_deposit`.

### Title
Stale hardcoded remote-chain deposit constants in `calculate_remote_deposit` can under-fund `poke_deposit`, causing irreversible loss of identity state after `reap_identity` - (File: `polkadot/runtime/rococo/src/impls.rs`, `polkadot/runtime/westend/src/impls.rs`, `substrate/frame/staking-async/runtimes/rc/src/impls.rs`)

### Summary
`calculate_remote_deposit` computes the amount of native token to teleport to the People Chain when a relay-chain identity is reaped, using constants copy-pasted from the parachain's runtime configuration (`BasicDeposit`, `ByteDeposit`, `SubAccountDeposit`, `EXISTENTIAL_DEPOSIT`) rather than reading the parachain's actual, live configuration. This mirrors the Solana report's `Rent::default()` vs `Rent::get()` defect: a value that must match a remote/live state is instead frozen into local, potentially stale constants.

### Finding Description
`ToParachainIdentityReaper::calculate_remote_deposit` hardcodes the People Chain's deposit parameters directly in the relay-chain runtime: [1](#0-0) 

These constants are simply mirrored from the parachain source at a point in time (see the comment referencing a specific commit `a146918`), not read from the parachain via any on-chain proof or storage query. `on_reap_identity` uses this value (`total_to_send`) to teleport funds and then issues an XCM `Transact` calling `poke_deposit` on the People Chain: [2](#0-1) 

On the destination side, `pallet_identity::poke_deposit` calls `rejig_deposit`, which does a hard `reserve()` of the *actual*, current deposit required by the People Chain's live `BasicDeposit`/`ByteDeposit`/`SubAccountDeposit` config: [3](#0-2) [4](#0-3) 

Meanwhile, on the relay chain, `reap_identity` unconditionally deletes `IdentityOf`/`SubsOf` and unreserves the relay-chain deposit *before* the cross-chain settlement is known to succeed — there is no atomicity between the relay-chain deletion and the parachain-side `poke_deposit`: [5](#0-4) 

If the People Chain's `BasicDeposit`, `ByteDeposit`, `SubAccountDeposit`, or `EXISTENTIAL_DEPOSIT` are ever changed (a routine, expected runtime-upgrade parameter change, not an admin abuse scenario) without a synchronized update of the hardcoded constants in every relay-chain's `calculate_remote_deposit`, the teleported `total_to_send` amount will no longer match what `rejig_deposit`'s `reserve()` call actually requires. If the parachain's live deposit requirement increases while the relay-chain hardcoded value remains the old, lower amount, the teleported funds land in the user's account via `DepositAsset`, but the subsequent `Transact { poke_deposit }` instruction's `reserve()` call fails with insufficient free balance, since the underfunded deposit computed off stale constants doesn't cover the current requirement.

### Impact Explanation
Because `reap_identity` on the relay chain already irrevocably removed the user's `IdentityOf`/`SubsOf` storage and unreserved their relay-chain deposit before the remote outcome is known, and the XCM program has no compensating rollback if `poke_deposit` fails partway (the `DepositAsset` step already executed and committed by the time `Transact` fails), a stale hardcoded remote deposit constant produces a state where:
- The user's identity data is permanently destroyed on the relay chain.
- The People Chain's `poke_deposit` fails, leaving the People Chain identity deposit inconsistent/unrejigged and the reaped user's on-chain identity migration incomplete.

This is a permanent-state-inconsistency / fund-accounting failure caused entirely by hardcoded values not tracking the destination chain's live configuration — directly analogous to accounts created non-rent-exempt due to `Rent::default()`.

### Likelihood Explanation
This requires no malicious actor: any unprivileged user can trigger `reap_identity` (the `Reaper` origin is `EnsureSigned<AccountId>`), and the failure condition is triggered purely by an ordinary, expected maintenance action — a future runtime upgrade of the People Chain's identity deposit parameters that isn't perfectly mirrored, byte-for-byte, back into every relay chain that references it via hardcoded constants. Given multiple runtimes (`rococo`, `westend`, `staking-async/rc`) independently hardcode the same set of constants, keeping them all synchronized with the actual People Chain config on every parameter change is fragile and error-prone by design.

### Recommendation
Do not hardcode remote-chain deposit parameters. Options:
- Have the People Chain report its live deposit parameters back to the relay chain (e.g., via a queryable XCM response or a periodically synced storage value) instead of embedding constants.
- Alternatively, restructure `poke_deposit`/`reap_identity` so that the relay-chain-side deletion and remote-side deposit settlement are not treated as independently final; e.g., only remove relay-chain state after confirmation of successful remote settlement, or send a safety margin plus make `poke_deposit` tolerant of underfunding by topping up rather than requiring exact reservation.

### Proof of Concept
1. On the People Chain, governance (via a normal runtime upgrade) increases `ByteDeposit` or `BasicDeposit` in `pallet_identity::Config` (e.g., due to a storage-price reassessment).
2. The relay chain's `polkadot/runtime/*/src/impls.rs::calculate_remote_deposit` still uses the old hardcoded `deposit(1, 17) / 100` etc. values.
3. A user with a large identity payload (`bytes` value) calls `reap_identity`, which succeeds on the relay chain: `IdentityOf`/`SubsOf` are removed and the relay-chain deposit unreserved.
4. The relay chain computes and teleports `total_to_send` based on stale constants — insufficient to cover the now-higher live `ByteDeposit` on the People Chain.
5. On the People Chain, the XCM program's `DepositAsset` deposits the (too-small) `total_to_send` into the user's account; the subsequent `Transact { poke_deposit }` fails because `rejig_deposit`'s `reserve()` call cannot reserve the correctly computed (higher) deposit from the underfunded account.
6. Result: the user's identity is permanently gone from the relay chain, and the People Chain identity deposit was never correctly re-reserved, leaving inconsistent/incomplete migrated state with no automatic recovery path.

### Citations

**File:** polkadot/runtime/rococo/src/impls.rs (L56-80)
```rust
	fn calculate_remote_deposit(bytes: u32, subs: u32) -> Balance {
		// Remote deposit constants. Parachain uses `deposit / 100`
		// Source:
		// https://github.com/paritytech/polkadot-sdk/blob/a146918/cumulus/parachains/common/src/rococo.rs#L29
		//
		// Parachain Deposit Configuration:
		//
		// pub const BasicDeposit: Balance = deposit(1, 17);
		// pub const ByteDeposit: Balance = deposit(0, 1);
		// pub const SubAccountDeposit: Balance = deposit(1, 53);
		// pub const EXISTENTIAL_DEPOSIT: Balance = constants::currency::EXISTENTIAL_DEPOSIT / 10;
		let para_basic_deposit = deposit(1, 17) / 100;
		let para_byte_deposit = deposit(0, 1) / 100;
		let para_sub_account_deposit = deposit(1, 53) / 100;
		let para_existential_deposit = EXISTENTIAL_DEPOSIT / 10;

		// pallet deposits
		let id_deposit =
			para_basic_deposit.saturating_add(para_byte_deposit.saturating_mul(bytes as Balance));
		let subs_deposit = para_sub_account_deposit.saturating_mul(subs as Balance);

		id_deposit
			.saturating_add(subs_deposit)
			.saturating_add(para_existential_deposit.saturating_mul(2))
	}
```

**File:** polkadot/runtime/rococo/src/impls.rs (L90-177)
```rust
	fn on_reap_identity(who: &AccountId, fields: u32, subs: u32) -> DispatchResult {
		use crate::{
			impls::IdentityMigratorCalls::PokeDeposit,
			weights::polkadot_runtime_common_identity_migrator::WeightInfo as MigratorWeights,
		};

		let total_to_send = Self::calculate_remote_deposit(fields, subs);

		// define asset / destination from relay perspective
		let roc = Asset { id: AssetId(Here.into_location()), fun: Fungible(total_to_send) };
		// People Chain: ParaId 1004
		let destination: Location = Location::new(0, Parachain(1004));

		// Do `check_out` accounting since the XCM Executor's `InitiateTeleport` doesn't support
		// unpaid teleports.

		// withdraw the asset from `who`
		let who_origin =
			Junction::AccountId32 { network: None, id: who.clone().into() }.into_location();
		let _withdrawn = xcm_config::LocalAssetTransactor::withdraw_asset(&roc, &who_origin, None)
			.map_err(|err| {
				log::error!(
					target: "runtime::on_reap_identity",
					"withdraw_asset(what: {:?}, who_origin: {:?}) error: {:?}",
					roc, who_origin, err
				);
				pallet_xcm::Error::<Runtime>::LowBalance
			})?;

		// check out
		xcm_config::LocalAssetTransactor::can_check_out(
			&destination,
			&roc,
			// not used in AssetTransactor
			&XcmContext { origin: None, message_id: [0; 32], topic: None },
		)
		.map_err(|err| {
			log::error!(
				target: "runtime::on_reap_identity",
				"can_check_out(destination: {:?}, asset: {:?}, _) error: {:?}",
				destination, roc, err
			);
			pallet_xcm::Error::<Runtime>::CannotCheckOutTeleport
		})?;
		xcm_config::LocalAssetTransactor::check_out(
			&destination,
			&roc,
			// not used in AssetTransactor
			&XcmContext { origin: None, message_id: [0; 32], topic: None },
		);

		// reanchor
		let roc_reanchored: Assets =
			vec![Asset { id: AssetId(Location::new(1, Here)), fun: Fungible(total_to_send) }]
				.into();

		let poke = PeopleRuntimePallets::<AccountId>::IdentityMigrator(PokeDeposit(who.clone()));
		let remote_weight_limit = MigratorWeights::<Runtime>::poke_deposit().saturating_mul(2);

		// Actual program to execute on People Chain.
		let program: Xcm<()> = Xcm(vec![
			// Unpaid as this is constructed by the system, once per user. The user shouldn't have
			// their balance reduced by teleport fees for the favor of migrating.
			UnpaidExecution { weight_limit: Unlimited, check_origin: None },
			// Receive the asset into holding.
			ReceiveTeleportedAsset(roc_reanchored),
			// Deposit into the user's account.
			DepositAsset {
				assets: Wild(AllCounted(1)),
				beneficiary: Junction::AccountId32 { network: None, id: who.clone().into() }
					.into_location()
					.into(),
			},
			// Poke the deposit to reserve the appropriate amount on the parachain.
			Transact {
				origin_kind: OriginKind::Superuser,
				fallback_max_weight: Some(remote_weight_limit),
				call: poke.encode().into(),
			},
		]);

		// send
		<pallet_xcm::Pallet<Runtime>>::send(
			RawOrigin::Root.into(),
			Box::new(VersionedLocation::from(destination)),
			Box::new(VersionedXcm::from(program)),
		)?;
		Ok(())
```

**File:** substrate/frame/identity/src/lib.rs (L1465-1478)
```rust
	/// Take the `current` deposit that `who` is holding, and update it to a `new` one.
	fn rejig_deposit(
		who: &T::AccountId,
		current: BalanceOf<T>,
		new: BalanceOf<T>,
	) -> DispatchResult {
		if new > current {
			T::Currency::reserve(who, new - current)?;
		} else if new < current {
			let err_amount = T::Currency::unreserve(who, current - new);
			debug_assert!(err_amount.is_zero());
		}
		Ok(())
	}
```

**File:** substrate/frame/identity/src/lib.rs (L1649-1687)
```rust
	pub fn poke_deposit(
		target: &T::AccountId,
	) -> Result<(BalanceOf<T>, BalanceOf<T>), DispatchError> {
		// Identity Deposit
		let new_id_deposit = IdentityOf::<T>::try_mutate(
			&target,
			|identity_of| -> Result<BalanceOf<T>, DispatchError> {
				let reg = identity_of.as_mut().ok_or(Error::<T>::NoIdentity)?;
				// Calculate what deposit should be
				let encoded_byte_size = reg.info.encoded_size() as u32;
				let byte_deposit =
					T::ByteDeposit::get().saturating_mul(BalanceOf::<T>::from(encoded_byte_size));
				let new_id_deposit = T::BasicDeposit::get().saturating_add(byte_deposit);

				// Update account
				Self::rejig_deposit(&target, reg.deposit, new_id_deposit)?;

				reg.deposit = new_id_deposit;
				Ok(new_id_deposit)
			},
		)?;

		let new_subs_deposit = if SubsOf::<T>::contains_key(&target) {
			SubsOf::<T>::try_mutate(
				&target,
				|(current_subs_deposit, subs_of)| -> Result<BalanceOf<T>, DispatchError> {
					let new_subs_deposit = Self::subs_deposit(subs_of.len() as u32);
					Self::rejig_deposit(&target, *current_subs_deposit, new_subs_deposit)?;
					*current_subs_deposit = new_subs_deposit;
					Ok(new_subs_deposit)
				},
			)?
		} else {
			// If the item doesn't exist, there is no "old" deposit, and the new one is zero, so no
			// need to call rejig, it'd just be zero -> zero.
			Zero::zero()
		};
		Ok((new_id_deposit, new_subs_deposit))
	}
```

**File:** polkadot/runtime/common/src/identity_migrator.rs (L106-132)
```rust
	impl<T: Config> Pallet<T> {
		/// Reap the `IdentityInfo` of `who` from the Identity pallet of `T`, unreserving any
		/// deposits held and removing storage items associated with `who`.
		#[pallet::call_index(0)]
		#[pallet::weight(<T as pallet::Config>::WeightInfo::reap_identity(
				T::MaxRegistrars::get(),
				T::MaxSubAccounts::get()
		))]
		pub fn reap_identity(
			origin: OriginFor<T>,
			who: T::AccountId,
		) -> DispatchResultWithPostInfo {
			T::Reaper::ensure_origin(origin)?;
			// - number of registrars (required to calculate weight)
			// - byte size of `IdentityInfo` (required to calculate remote deposit)
			// - number of sub accounts (required to calculate both weight and remote deposit)
			let (registrars, bytes, subs) = pallet_identity::Pallet::<T>::reap_identity(&who)?;
			T::ReapIdentityHandler::on_reap_identity(&who, bytes, subs)?;
			Self::deposit_event(Event::IdentityReaped { who });
			let post = PostDispatchInfo {
				actual_weight: Some(<T as pallet::Config>::WeightInfo::reap_identity(
					registrars, subs,
				)),
				pays_fee: Pays::No,
			};
			Ok(post)
		}
```
