Found the key mismatch: `add_tip` in `snowbridge-pallet-system-v2` is gated by `T::FrontendOrigin`, which is an XCM-based origin filter, **not** a currency-charging mechanism. The `sender` parameter is caller-supplied data used only for bookkeeping (`LostTips`/event), while the actual value transfer for the `amount` credited as a tip must happen upstream (e.g. on AssetHub) before the XCM message reaches this pallet.

### Title
`add_tip` credits relayer rewards without any local balance debit, allowing a caller with `FrontendOrigin` to register unbacked reward inflation - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

### Summary
The `add_tip` extrinsic checks only `T::FrontendOrigin::ensure_origin(origin)?` and then directly calls `<T as Config>::InboundQueue::add_tip(nonce, amount)` / `OutboundQueue::add_tip(nonce, amount)`, which increases the `fee`/`tip` stored in `PendingOrder` for an existing nonce. [1](#0-0) 
That accumulated `fee` is later paid out in full to the relayer via `T::RewardPayment::register_reward` in `process_delivery_receipt` when the receipt is submitted. [2](#0-1) 
Nowhere in `add_tip`, nor in `AddTip::add_tip` on the outbound/inbound queue side, is the `amount` actually withdrawn, transferred, or reserved from the `sender` account or any account at all — it is a pure storage mutation of `order.fee`. [3](#0-2) 

### Finding Description
This is a direct structural analog of the external report's core defect: **a single privileged path is allowed to both define the "amount owed" and be the party that benefits from it, without the accounting invariant "value must be conserved / actually collected before it can be paid out" being enforced in the same call path.** In the external report the relayer could rewrite `tokenId → address` mappings and then claim balances that were never truly deposited; here, whatever entity is authorized under `T::FrontendOrigin` (an XCM-derived origin representing a sibling location, e.g. AssetHub) can call `add_tip` to arbitrarily inflate the `fee` on any still-pending outbound/inbound order by any `amount`, and that inflated `fee`/tip is paid straight out of the reward payment mechanism (`T::RewardPayment` / `BridgeRewardPayer`) to whichever relayer submits the delivery receipt. [4](#0-3) 
There is no local balance check, no `Currency::transfer`/`hold` from `sender`, and no correlation between the `amount` argument and any funds actually locked on this chain — the pallet trusts that whatever called through `FrontendOrigin` already collected the tip on the sending side (AssetHub), but this pallet itself enforces nothing. [3](#0-2) 
The payout side then treats `order.fee` as fully trustworthy and pays it from the bridge's reward ledger to `reward_account` with no re-validation against any deposited value. [5](#0-4) 

### Impact Explanation
If `T::FrontendOrigin` is broader than intended (e.g. it accepts the sibling AssetHub location generically rather than being tightly bound to the specific pallet/account instance on AssetHub that actually collected the tip payment — see `system-frontend` pallet proxying), any code reachable through that origin filter can mint unbacked relayer rewards by repeatedly calling `add_tip` on live pending nonces, draining the reward pot/sovereign account that backs `BridgeRewardPayer` (theft/unbacked mint class impact, matching "theft or unbacked mint" in the Required Impacts). [6](#0-5) 

### Likelihood Explanation
This does not require a malicious relayer, validator, governance actor, or leaked key — it only requires the ability to construct/route an XCM message that satisfies `T::FrontendOrigin`, which is designed to be reachable from a sibling chain (AssetHub) as a normal user-facing feature (token registration/tip top-up), making the trust boundary between "origin passed the filter" and "value was actually collected" the exact broken invariant, mirroring the external report's root cause of an under-scoped privileged function mixing accounting authority with payout benefit.

### Recommendation
Enforce that `add_tip`'s `amount` is backed by an actual on-chain value transfer/hold at the point of registration (either by the frontend pallet or by requiring proof of a locked deposit tied to the specific nonce and `sender`), and reconcile the total `fee` paid out in `process_delivery_receipt` against verifiable collected value rather than trusting an unconditionally-incrementable storage field.

### Proof of Concept
1. Attacker (or any entity able to satisfy `T::FrontendOrigin`, e.g. a compromised/rogue XCM sender module on the sibling chain, which is a live cross-chain-callable surface, not an admin/governance actor) sends an XCM that resolves through `system-frontend`'s proxy into `snowbridge_pallet_system_v2::Pallet::add_tip(origin, sender, MessageId::Outbound(nonce), amount = 10_000 ETH)` for an existing pending order. [7](#0-6) 
2. No balance is moved; `PendingOrders[nonce].fee` in `outbound-queue-v2` is simply increased by `amount`. [3](#0-2) 
3. The relayer (who may be the same account, or colludes) submits `submit_delivery_receipt`, causing `process_delivery_receipt` to call `T::RewardPayment::register_reward(&reward_account, DefaultRewardKind, order.fee)` for the fully inflated `fee`, paying out the fabricated `10_000 ETH` reward from the bridge reward account. [2](#0-1) 

Note: I was not able to fully verify the exact scope/binding of `T::FrontendOrigin` as configured in the live `bridge-hub-westend`/`bridge-hub-rococo` runtimes (i.e., whether it is tightly restricted to only a trusted AssetHub pallet instance that guarantees prior fund collection) within the tool budget available — this is the critical fact that determines whether the path is truly reachable by an unprivileged actor or is safely gated by a narrow, pre-vetted origin. This should be confirmed by reading `system-frontend` pallet's call-forwarding logic and the runtime's `FrontendOrigin` type definition in a follow-up session.

### Citations

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-264)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(<T as pallet::Config>::WeightInfo::add_tip())]
		pub fn add_tip(
			origin: OriginFor<T>,
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let result = match message_id {
				Inbound(nonce) => <T as pallet::Config>::InboundQueue::add_tip(nonce, amount),
				Outbound(nonce) => <T as pallet::Config>::OutboundQueue::add_tip(nonce, amount),
			};
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L483-496)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			PendingOrders::<T>::try_mutate_exists(nonce, |maybe_order| -> Result<(), AddTipError> {
				match maybe_order {
					Some(order) => {
						order.fee = order.fee.saturating_add(amount);
						Ok(())
					},
					None => Err(AddTipError::UnknownMessage),
				}
			})
		}
	}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L89-140)
```rust
/// Implementation of `bp_relayers::PaymentProcedure` as a pay/claim rewards scheme.
pub struct BridgeRewardPayer;
impl bp_relayers::PaymentProcedure<AccountId, BridgeReward, u128> for BridgeRewardPayer {
	type Error = sp_runtime::DispatchError;
	type Beneficiary = BridgeRewardBeneficiaries;

	fn pay_reward(
		relayer: &AccountId,
		reward_kind: BridgeReward,
		reward: u128,
		beneficiary: BridgeRewardBeneficiaries,
	) -> Result<(), Self::Error> {
		match reward_kind {
			BridgeReward::RococoWestend(lane_params) => {
				match beneficiary {
					BridgeRewardBeneficiaries::LocalAccount(account) => {
						bp_relayers::PayRewardFromAccount::<
							Balances,
							AccountId,
							LegacyLaneId,
							u128,
						>::pay_reward(
							&relayer, lane_params, reward, account,
						)
					},
					BridgeRewardBeneficiaries::AssetHubLocation(_) => Err(Self::Error::Other("`AssetHubLocation` beneficiary is not supported for `RococoWestend` rewards!")),
				}
			},
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
}
```
