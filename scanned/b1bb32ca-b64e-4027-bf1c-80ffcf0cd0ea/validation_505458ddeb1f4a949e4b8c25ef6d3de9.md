## Summary

The Turnstile bug's core defect is: **a public entry point lets an unprivileged caller supply an arbitrary "recipient" address, the calling chain commits state as if delivery/registration is final, but the actual value-crediting step happens in a different execution context that can independently fail — with the record already irreversibly cleared and no retry path.**

I traced the equivalent pattern into `pallet-bridge-relayers`'s reward-claim flow, which is used by BridgeHub for Snowbridge relayer rewards.

## Analysis

`Pallet::<T,I>::do_claim_rewards` is the underlying logic for both `claim_rewards` and `claim_rewards_to`, both callable by any signed relayer with a pending reward: [1](#0-0) 

The critical detail is that `maybe_reward.take()` clears the `RelayerRewards` storage entry for the relayer *inside* the `try_mutate_exists` closure, and the entry only stays cleared if `T::PaymentProcedure::pay_reward(...)` returns `Ok(())`. For the Snowbridge reward kind, this delegates to `PayAccountOnLocation::pay_reward`, which builds a best-effort, unpaid XCM program and only confirms that the message was *queued* for delivery — not that it executed successfully on AssetHub: [2](#0-1) 

The runtime wiring confirms `beneficiary` is a caller-supplied `Location` (`BridgeRewardBeneficiaries::AssetHubLocation`) that is only version-decoded, never validated as a location that can actually receive the asset on AssetHub: [3](#0-2) 

`XcmSender::deliver(ticket)` returning `Ok` only means the message entered the XCMP queue; execution of `DepositAsset { beneficiary, .. }` happens later, asynchronously, on AssetHub. If that `DepositAsset` fails to resolve/credit the caller-chosen `beneficiary` (e.g., an unresolvable or otherwise non-receivable location), the assets are dropped into the XCM asset trap rather than reaching the relayer: [4](#0-3) 

Crucially, the trap is keyed by `hash_of(&(origin, assets))` where `origin` is the *executing* origin on AssetHub — for this reward flow that is the descended/universal Snowbridge-inbound-queue origin (`DescendOrigin(InboundQueueLocation)` + `UniversalOrigin(GlobalConsensus(EthereumNetwork))`), not the relayer's own AssetHub signed account. `claim_assets`/`ClaimAsset` require the claiming origin to match the trap origin exactly: [5](#0-4) [6](#0-5) 

Since the relayer's own signed account can never reproduce that bridge-descended origin, they cannot claim the trapped assets themselves — matching the "no way to re-register/re-claim" property of the original Turnstile bug.

Meanwhile, on BridgeHub, `do_claim_rewards` has already committed: the `RelayerRewards` entry is gone and `RewardPaid` is emitted, because `pay_reward` returned `Ok(())` at the point of successful *delivery*, not successful *execution*: [7](#0-6) 

This is directly analogous to the Turnstile finding: the application-layer action (`register`/`claim_rewards_to`) succeeds and is treated as final, while the consensus-layer/cross-chain crediting step (`GetAccount` check / `DepositAsset` on AssetHub) can independently and silently fail, and the one-shot nature of the storage mutation (`onlyUnregistered` / `take()` in `try_mutate_exists`) means there is no way back for the affected relayer.

### Title
Snowbridge relayer reward is marked paid and cleared on BridgeHub before the cross-chain `DepositAsset` to the caller-chosen beneficiary is confirmed, permanently losing the reward if delivery to AssetHub fails - (File: `bridges/modules/relayers/src/lib.rs`)

### Finding Description
`claim_rewards_to` lets any signed relayer with an accumulated reward choose an arbitrary `beneficiary: BeneficiaryOf<T,I>` (an XCM `Location`, wrapped as `BridgeRewardBeneficiaries::AssetHubLocation`). `do_claim_rewards` removes the `RelayerRewards` entry and emits `RewardPaid` as soon as `PaymentProcedure::pay_reward` returns `Ok(())`. For `BridgeReward::Snowbridge`, `pay_reward` is `PayAccountOnLocation::pay_reward`, which only checks that fee-charging and `XcmSender::deliver` succeed — i.e., that the reward-payment XCM was accepted into the outbound queue. It performs no validation that the caller-supplied `beneficiary` will actually resolve to a receivable account when `DepositAsset` executes on AssetHub. Execution of that `DepositAsset` happens later, asynchronously, in a different runtime/consensus context (AssetHub), exactly mirroring the Turnstile pattern where the mint succeeds at the application layer while the corresponding check ("existing recipient account" / "resolvable beneficiary") only surfaces in a separate execution layer.

If `DepositAsset` cannot credit the supplied `beneficiary` (unresolvable/degenerate `Location`, asset-transactor failure, etc.), the assets fall into `PolkadotXcm::drop_assets` and get trapped, keyed by the executing (bridge-descended) origin — not the relayer's own account — so the relayer cannot construct a matching `claim_assets` call to recover them.

### Impact Explanation
This breaks the "settle exactly once to the rightful beneficiary" invariant for bridge reward payouts: the relayer's reward record is deleted and `RewardPaid` is emitted on BridgeHub asserting successful payment, while the actual funds can end up permanently trapped on AssetHub under an origin the relayer cannot reproduce, and since the `RelayerRewards` storage entry has already been cleared, there is no retry/resubmission path — permanent value loss for the relayer with no recourse short of governance intervention.

### Likelihood Explanation
The only actor required is the relayer itself, submitting `claim_rewards_to` with a `beneficiary` value of their own choosing; no privileged, admin, or malicious-peer/validator assumption is needed. The failure path is deterministic once a beneficiary Location that `DepositAsset` cannot resolve is supplied, so it is trivially reproducible by any relayer (accidentally or otherwise) and is not merely theoretical — the codebase's own trapped-asset tests demonstrate `DepositAsset` failures routinely fall through to `drop_assets`.

### Recommendation
Do not treat XCM `deliver()` success as final settlement in `do_claim_rewards`. Either (a) keep the `RelayerRewards` entry pending until a delivery-receipt / execution confirmation is received back from AssetHub before clearing it and emitting `RewardPaid`, or (b) validate that `beneficiary` resolves to a receivable account before committing the storage removal, or (c) ensure the trap origin used for the reward-payment XCM is derivable by the relayer (e.g., preserve the relayer's own origin through the XCM) so a failed `DepositAsset` can always be recovered via `claim_assets`.

### Proof of Concept
1. Relayer accumulates a Snowbridge reward via `register_reward`, visible via `RelayerRewards::<T,I>::get(relayer, BridgeReward::Snowbridge)`.
2. Relayer calls `claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(some_location))` where `some_location` is a `Location` that AssetHub's asset transactor cannot resolve into a receivable account for the reserve-deposited asset (analogous to `test_set_asset_claimer_within_a_chain`/trapped-assets tests showing `DepositAsset` failures fall through to `drop_assets`).
3. On BridgeHub: `validate_send` and `XcmSender::deliver` succeed (message merely queued), so `pay_reward` returns `Ok(())`; `do_claim_rewards` clears `RelayerRewards` and emits `RewardPaid` — see `bridges/modules/relayers/src/lib.rs:263-301`.
4. On AssetHub: the XCM executes; `DepositAsset` to the unresolvable beneficiary fails, and the asset ends up trapped via `drop_assets`, keyed by the bridge-descended/universal origin rather than the relayer's account — see `polkadot/xcm/pallet-xcm/src/lib.rs:3901-3924`.
5. The relayer, signing `claim_assets` from their own AssetHub account, cannot reproduce the trap origin (`ClaimAssets::claim_assets` requires an exact origin match — `polkadot/xcm/pallet-xcm/src/lib.rs:3927-3950`), so the reward is permanently unrecoverable, while BridgeHub's records already show it as paid.

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L263-301)
```rust
		fn do_claim_rewards(
			relayer: T::AccountId,
			reward_kind: T::Reward,
			beneficiary: BeneficiaryOf<T, I>,
		) -> DispatchResult {
			RelayerRewards::<T, I>::try_mutate_exists(
				&relayer,
				reward_kind,
				|maybe_reward| -> DispatchResult {
					let reward_balance =
						maybe_reward.take().ok_or(Error::<T, I>::NoRewardForRelayer)?;
					T::PaymentProcedure::pay_reward(
						&relayer,
						reward_kind,
						reward_balance,
						beneficiary.clone(),
					)
					.map_err(|e| {
						tracing::error!(
							target: LOG_TARGET,
							error=?e,
							?relayer,
							?reward_kind,
							?reward_balance,
							?beneficiary,
							"Failed to pay rewards"
						);
						Error::<T, I>::FailedToPayReward
					})?;

					Self::deposit_event(Event::<T, I>::RewardPaid {
						relayer: relayer.clone(),
						reward_kind,
						reward_balance,
						beneficiary,
					});
					Ok(())
				},
			)
```

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L127-151)
```rust
	fn pay_reward(
		relayer: &Relayer,
		_: (),
		reward: RewardBalance,
		beneficiary: Self::Beneficiary,
	) -> Result<(), Self::Error> {
		let ethereum_location = Location::new(2, [GlobalConsensus(EthereumNetwork::get())]);
		let assets: Asset = (ethereum_location.clone(), reward.into()).into();

		let xcm: Xcm<()> = alloc::vec![
			UnpaidExecution { weight_limit: Unlimited, check_origin: None },
			DescendOrigin(InboundQueueLocation::get().into()),
			UniversalOrigin(GlobalConsensus(EthereumNetwork::get())),
			ReserveAssetDeposited(assets.into()),
			DepositAsset { assets: AllCounted(1).into(), beneficiary },
		]
		.into();

		let (ticket, fee) =
			validate_send::<XcmSender>(AssetHubLocation::get(), xcm).map_err(|_| XcmSendFailure)?;
		XcmExecutor::charge_fees(relayer.clone(), fee).map_err(|_| ChargeFeesFailure)?;
		XcmSender::deliver(ticket).map_err(|_| XcmSendFailure)?;

		Ok(())
	}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-139)
```rust
			BridgeReward::Snowbridge => {
				match beneficiary {
					BridgeRewardBeneficiaries::LocalAccount(_) => Err(Self::Error::Other("`LocalAccount` beneficiary is not supported for `Snowbridge` rewards!")),
					BridgeRewardBeneficiaries::AssetHubLocation(account_location) => {
						let account_location = Location::try_from(account_location)
							.map_err(|_| Self::Error::Other("`AssetHubLocation` beneficiary location version is not supported for `Snowbridge` rewards!"))?;
						snowbridge_core::reward::PayAccountOnLocation::<
							AccountId,
							u128,
							EthereumNetwork,
							AssetHubLocation,
							InboundQueueV2Location,
							XcmRouter,
							XcmExecutor<XcmConfig>,
							RuntimeCall
						>::pay_reward(
							relayer, (), reward, account_location
						)
					}
				}
			}
		}
	}
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L1520-1550)
```rust
		/// Claims assets trapped on this pallet because of leftover assets during XCM execution.
		///
		/// - `origin`: Anyone can call this extrinsic.
		/// - `assets`: The exact assets that were trapped. Use the version to specify what version
		/// was the latest when they were trapped.
		/// - `beneficiary`: The location/account where the claimed assets will be deposited.
		#[pallet::call_index(12)]
		pub fn claim_assets(
			origin: OriginFor<T>,
			assets: Box<VersionedAssets>,
			beneficiary: Box<VersionedLocation>,
		) -> DispatchResult {
			let origin_location = T::ExecuteXcmOrigin::ensure_origin(origin)?;
			tracing::debug!(target: "xcm::pallet_xcm::claim_assets", ?origin_location, ?assets, ?beneficiary);
			// Extract version from `assets`.
			let assets_version = assets.identify_version();
			let assets: Assets = (*assets).try_into().map_err(|()| {
				tracing::debug!(
					target: "xcm::pallet_xcm::claim_assets",
					"Failed to convert input VersionedAssets",
				);
				Error::<T>::BadVersion
			})?;
			let number_of_assets = assets.len() as u32;
			let beneficiary: Location = (*beneficiary).try_into().map_err(|()| {
				tracing::debug!(
					target: "xcm::pallet_xcm::claim_assets",
					"Failed to convert beneficiary VersionedLocation",
				);
				Error::<T>::BadVersion
			})?;
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L3901-3924)
```rust
impl<T: Config> DropAssets for Pallet<T> {
	fn drop_assets(origin: &Location, holding: AssetsInHolding, _context: &XcmContext) -> Weight {
		if holding.is_empty() {
			return Weight::zero();
		}
		let assets: Vec<Asset> = holding.assets_iter().collect();
		// SAFETY: "forget" about any fungible imbalances so that they are not dropped/resolved
		// here. The mirrored asset claiming operation will "recover" the imbalances by minting
		// back into holding, effectively duplicating the imbalance and only then dropping the
		// duplicate. As a result, total issuance doesn't change.
		holding.fungible.into_iter().for_each(|(_, mut accounting)| {
			accounting.forget_imbalance();
		});
		let versioned = VersionedAssets::from(Assets::from(assets));
		let hash = BlakeTwo256::hash_of(&(&origin, &versioned));
		AssetTraps::<T>::mutate(hash, |n| *n += 1);
		Self::deposit_event(Event::AssetsTrapped {
			hash,
			origin: origin.clone(),
			assets: versioned,
		});
		// TODO #3735: Put the real weight in there.
		Weight::zero()
	}
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L3927-3950)
```rust
impl<T: Config> ClaimAssets for Pallet<T> {
	fn claim_assets(
		origin: &Location,
		ticket: &Location,
		assets: &Assets,
		context: &XcmContext,
	) -> Option<AssetsInHolding> {
		let mut versioned = VersionedAssets::from(assets.clone());
		match ticket.unpack() {
			(0, [GeneralIndex(i)]) => {
				versioned = match versioned.into_version(*i as u32) {
					Ok(v) => v,
					Err(()) => return None,
				}
			},
			(0, []) => (),
			_ => return None,
		};
		let hash = BlakeTwo256::hash_of(&(origin.clone(), versioned.clone()));
		match AssetTraps::<T>::get(hash) {
			0 => return None,
			1 => AssetTraps::<T>::remove(hash),
			n => AssetTraps::<T>::insert(hash, n - 1),
		}
```
