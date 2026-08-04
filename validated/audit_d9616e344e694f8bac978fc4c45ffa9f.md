### Title
`GatewayAddress` is a fixed constant, so an Ethereum-side Gateway contract change/upgrade permanently strands relayer-reward fees held in `PendingOrders` - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The RocketPool report describes proposals/challenges whose bonds become permanently unrecoverable because a version-gated check (`onlyLatestContract`) on the Ethereum side reverts every downstream call once a contract is upgraded, and there is no mechanism to release the locked bond. The Snowbridge outbound queue v2 pallet has the same structural weakness: it stages a bonded value (`PendingOrder.fee`, funded from the sender who paid for message delivery) keyed by nonce, and the only path to release/pay that value is `submit_delivery_receipt` → `process_delivery_receipt`, which hard-checks the receipt's `gateway` field against the compile-time `Config::GatewayAddress` constant [1](#0-0) . There is no other extrinsic, migration, or governance call in this pallet that can settle or refund a `PendingOrder` if that equality check can no longer be satisfied.

### Finding Description
`do_process_message` stores a `PendingOrder{ nonce, fee, block_number }` for every outbound message committed to the Merkle root, funded by the fee already collected from the sender [2](#0-1) . The only way this value is ever released — either paid out to a relayer as reward or simply removed — is through `process_delivery_receipt`:

```
ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);
...
let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
if order.fee > 0 { T::RewardPayment::register_reward(...); }
<PendingOrders<T>>::remove(nonce);
``` [3](#0-2) 

`GatewayAddress` is declared as a `#[pallet::constant]` `Get<H160>` [4](#0-3) , i.e. it is baked into the runtime and only changes via a runtime upgrade (the same "contract version registry" role that `RocketDAOProtocolProposal`/`RocketDAOProtocolVerifier`'s `onlyLatestContract` gate plays for RocketPool). If the Ethereum Gateway contract's outward-facing address is migrated (a real-world scenario for BridgeHub, since the whole point of `snowbridge-pallet-system-v2::upgrade()` is to let governance swap out the Gateway implementation/proxy over time), any `EventProof`/`DeliveryReceipt` emitted by the old address will permanently fail `InvalidGateway` for every `PendingOrder` that was created before the switch, exactly mirroring the RocketPool bug: the fee is bonded, the only settlement path is gated on a fixed "current version" check, and once that check can never again be satisfied, the fee is orphaned in storage with no code path to reclaim it (no admin call, no timeout-based refund, no migration is present in this pallet to sweep `PendingOrders`).

### Impact Explanation
Every outbound message fee collected but not yet delivered when the Gateway address changes becomes permanently locked in `PendingOrders`, unrecoverable by either the original payer or any relayer. Unlike the RocketPool team's mitigating claim ("we are in control of upgrades... it does not pose a current threat"), this repository provides no compensating mechanism (no bond-recovery extrinsic, no consideration/hold reclaim, no runtime migration draining `PendingOrders`) analogous to what exists in `pallet-treasury`'s `cleanup_proposals::Migration` (which explicitly unreserves stuck `Proposals` bonds after a governance-model change) [5](#0-4)  or `pallet-democracy`'s `UnlockAndUnreserveAllFunds` migration [6](#0-5) . Because this affects fee funds that are part of the bridge's fund-accounting/reward path, the impact matches the required-impact category "permanent user-fund or bridge-state lock."

### Likelihood Explanation
This is not a hypothetical peer/relayer/prover-abuse scenario — it is a direct consequence of an ordinary, expected maintenance action (rotating/upgrading the Ethereum-side Gateway address that `GatewayAddress` mirrors) combined with normal bridge usage (any in-flight message queued but not yet relayed at the time of the switch). No malicious actor, leaked key, or governance abuse is required; the root cause is a structural gap — a hard equality check with no fallback — not privileged misuse. The likelihood of an eventual Gateway redeployment over the life of the bridge is non-trivial given that `snowbridge-pallet-system-v2` itself exposes an `upgrade()` call whose purpose is exactly to change the Gateway's implementation [7](#0-6) .

### Recommendation
Add a recovery/fallback path for `PendingOrders` entries that can no longer be settled through `process_delivery_receipt`, e.g.: (1) allow governance or a permissionless-after-timeout call to refund the `fee` back to the original payer once an order has aged past a bound without a valid receipt, or (2) maintain a mapping of *historical* valid gateway addresses (with a validity window) rather than a single current constant, so in-flight orders created under a superseded address can still be settled. Mirror the pattern already used in `pallet-treasury::migration::cleanup_proposals` — a migration/extrinsic that walks `PendingOrders` and releases stuck fees — whenever `GatewayAddress` (or any future version marker) is rotated.

### Proof of Concept
1. Runtime `R1` has `GatewayAddress = G1`. A sender submits a message; `do_process_message` inserts `PendingOrders[nonce=42] = { fee: F, .. }`.
2. Before a relayer submits a delivery receipt for nonce 42, governance performs a runtime upgrade that changes `Config::GatewayAddress` from `G1` to `G2` (e.g. as part of migrating to a new Gateway contract deployment).
3. The relayer submits the legitimately-verified `EventProof`/`DeliveryReceipt` for nonce 42, which was emitted by `G1` (the address the message was actually delivered to before the switch).
4. `process_delivery_receipt` executes `ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway)` — `G2 != G1`, so the call reverts with `InvalidGateway` [1](#0-0) .
5. `PendingOrders[42]` can never be removed or paid out through any other extrinsic in the pallet; the fee `F` is permanently stranded in storage, unrecoverable by the payer or the relayer.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L161-163)
```rust
		/// Address of the Gateway contract
		#[pallet::constant]
		type GatewayAddress: Get<H160>;
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-437)
```rust
			// Generate `PendingOrder` with fee attached in the message, stored
			// into the `PendingOrders` map storage, with assigned nonce as the key.
			// When the message is processed on ethereum side, the relayer will send the nonce
			// back with delivery proof, only after that the order can
			// be resolved and the fee will be rewarded to the relayer.
			let order = PendingOrder {
				nonce,
				fee,
				block_number: frame_system::Pallet::<T>::current_block_number(),
			};
			<PendingOrders<T>>::insert(nonce, order);

```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L445-480)
```rust
		/// Process a delivery receipt from a relayer, to allocate the relayer reward.
		pub fn process_delivery_receipt(
			relayer: <T as frame_system::Config>::AccountId,
			receipt: DeliveryReceipt,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);

			let reward_account = if receipt.reward_address == [0u8; 32] {
				relayer
			} else {
				receipt.reward_address.into()
			};

			let nonce = receipt.nonce;

			let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;

			if order.fee > 0 {
				// Pay relayer reward
				T::RewardPayment::register_reward(
					&reward_account,
					T::DefaultRewardKind::get(),
					order.fee,
				);
			}

			<PendingOrders<T>>::remove(nonce);

			Self::deposit_event(Event::MessageDelivered { nonce });

			Ok(())
		}
```

**File:** substrate/frame/treasury/src/migration.rs (L30-88)
```rust
pub mod cleanup_proposals {
	use super::*;

	/// Migration to cleanup unapproved proposals to return the bonds back to the proposers.
	/// Proposals can no longer be created and the `Proposal` storage item will be removed in the
	/// future.
	///
	/// `UnreserveWeight` returns `Weight` of `unreserve_balance` operation which is perfomed during
	/// this migration.
	pub struct Migration<T, I, UnreserveWeight>(PhantomData<(T, I, UnreserveWeight)>);

	impl<T: Config<I>, I: 'static, UnreserveWeight: Get<Weight>> OnRuntimeUpgrade
		for Migration<T, I, UnreserveWeight>
	{
		fn on_runtime_upgrade() -> frame_support::weights::Weight {
			let mut approval_index = BTreeSet::new();
			#[allow(deprecated)]
			for approval in Approvals::<T, I>::get().iter() {
				approval_index.insert(*approval);
			}

			let mut proposals_processed = 0;
			#[allow(deprecated)]
			for (proposal_index, p) in Proposals::<T, I>::iter() {
				if !approval_index.contains(&proposal_index) {
					let err_amount = T::Currency::unreserve(&p.proposer, p.bond);
					if err_amount.is_zero() {
						Proposals::<T, I>::remove(proposal_index);
						log::info!(
							target: LOG_TARGET,
							"Released bond amount of {:?} to proposer {:?}",
							p.bond,
							p.proposer,
						);
					} else {
						defensive!(
							"err_amount is non zero for proposal {:?}",
							(proposal_index, err_amount)
						);
						Proposals::<T, I>::mutate_extant(proposal_index, |proposal| {
							proposal.value = err_amount;
						});
						log::info!(
							target: LOG_TARGET,
							"Released partial bond amount of {:?} to proposer {:?}",
							p.bond - err_amount,
							p.proposer,
						);
					}
					proposals_processed += 1;
				}
			}

			log::info!(
				target: LOG_TARGET,
				"Migration for pallet-treasury finished, released {} proposal bonds.",
				proposals_processed,
			);

```

**File:** substrate/frame/democracy/src/migrations/unlock_and_unreserve_all_funds.rs (L220-251)
```rust
	/// Executes the migration.
	///
	/// Steps:
	/// 1. Retrieves the deposit and accounts with locks for the pallet.
	/// 2. Unreserves the deposited funds for each account.
	/// 3. Unlocks the staked funds for each account.
	fn on_runtime_upgrade() -> frame_support::weights::Weight {
		// Get staked and deposited balances as reported by this pallet.
		let (account_deposits, account_stakes, initial_reads) =
			Self::get_account_deposits_and_locks();

		// Deposited funds need to be unreserved.
		for (account, unreserve_amount) in account_deposits.iter() {
			if unreserve_amount.is_zero() {
				log::warn!(target: LOG_TARGET, "Unexpected zero amount to unreserve!");
				continue;
			}
			T::Currency::unreserve(&account, *unreserve_amount);
		}

		// Staked funds need to be unlocked.
		for account in account_stakes.keys() {
			T::Currency::remove_lock(DEMOCRACY_ID, account);
		}

		T::DbWeight::get()
			.reads_writes(
				account_stakes.len().saturating_add(account_deposits.len()) as u64,
				account_stakes.len().saturating_add(account_deposits.len()) as u64,
			)
			.saturating_add(initial_reads)
	}
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L155-182)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight((<T as pallet::Config>::WeightInfo::upgrade(), DispatchClass::Operational))]
		pub fn upgrade(
			origin: OriginFor<T>,
			impl_address: H160,
			impl_code_hash: H256,
			initializer: Initializer,
		) -> DispatchResult {
			let origin_location = T::GovernanceOrigin::ensure_origin(origin)?;
			let origin = Self::location_to_message_origin(origin_location)?;

			ensure!(
				!impl_address.eq(&H160::zero()) && !impl_code_hash.eq(&H256::zero()),
				Error::<T>::InvalidUpgradeParameters
			);

			let initializer_params_hash: H256 = blake2_256(initializer.params.as_ref()).into();

			let command = Command::Upgrade { impl_address, impl_code_hash, initializer };
			Self::send(origin, command, 0)?;

			Self::deposit_event(Event::<T>::Upgrade {
				impl_address,
				impl_code_hash,
				initializer_params_hash,
			});
			Ok(())
		}
```
