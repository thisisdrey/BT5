## Title
`sender` argument in `system-v2::register_token` / `add_tip` can be impersonated because it is never bound to the verified `FrontendOrigin` — ([File: bridges/snowbridge/pallets/system-v2/src/lib.rs])

## Summary
The Zetachain bug allowed an attacker/observer to swap the `sender` argument of the Solana `OnCall` instruction because that argument was never included in the data that was actually verified/signed. The Snowbridge `system-v2` pallet has the same broken invariant: `register_token` and `add_tip` accept a caller‑supplied `sender` value that is used for accounting and cross‑chain message attribution, but the pallet never checks that this `sender` matches the identity established by the actual origin check.

## Finding Description
`register_token` and `add_tip` both authenticate the call only via:

```rust
T::FrontendOrigin::ensure_origin(origin)?;
``` [1](#0-0) 

Note that, unlike `upgrade` and `set_operating_mode`, which capture the returned `Location` and use it as the actual message/fee origin:

```rust
let origin_location = T::GovernanceOrigin::ensure_origin(origin)?;
let origin = Self::location_to_message_origin(origin_location)?;
``` [2](#0-1) 

`register_token` discards the `Location` returned by `ensure_origin` entirely and instead trusts a plain, caller‑supplied parameter for the identity that will actually be charged/attributed:

```rust
pub fn register_token(
    origin: OriginFor<T>,
    sender: Box<VersionedLocation>,
    asset_id: Box<VersionedLocation>,
    metadata: AssetMetadata,
    amount: u128,
) -> DispatchResult {
    T::FrontendOrigin::ensure_origin(origin)?;

    let sender_location: Location =
        (*sender).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
    ...
    let message_origin = Self::location_to_message_origin(sender_location)?;
    Self::send(message_origin, command, amount)?;
``` [3](#0-2) 

The doc comment even confirms the intent that `sender` should represent "The original sender initiating the call on AH", i.e. an end user identity forwarded through the "system frontend pallet on AH", but nothing cryptographically or structurally ties the `sender` parameter to the actual authenticated `origin`/`FrontendOrigin::ensure_origin` result:

```rust
/// - `sender`: The original sender initiating the call on AH
``` [4](#0-3) 

The same pattern repeats in `add_tip`, which accepts a raw `sender: AccountIdOf<T>` and only checks `T::FrontendOrigin::ensure_origin(origin)?` — again discarding the returned `Location` and never validating that `sender` corresponds to it:

```rust
pub fn add_tip(
    origin: OriginFor<T>,
    sender: AccountIdOf<T>,
    message_id: MessageId,
    amount: u128,
) -> DispatchResult {
    T::FrontendOrigin::ensure_origin(origin)?;
    ...
    LostTips::<T>::mutate(&sender, |lost_tip| {
        *lost_tip = lost_tip.saturating_add(amount);
    });

    Self::deposit_event(Event::<T>::TipProcessed { sender, message_id, amount, success: result.is_ok() });
``` [5](#0-4) 

This is the exact bug-class of the Solana report: an argument (`sender`) that is used downstream for fund/fee attribution and for the emitted `message_origin`/`TipProcessed` accounting is not part of the authenticated/verified data — `FrontendOrigin::ensure_origin` proves *who is calling the pallet* (e.g. the system-frontend pallet's XCM location on AssetHub), but does not prove *which underlying AH end-user* the caller claims to represent. Any caller that satisfies `FrontendOrigin` (which by design is expected to be a fairly broad XCM-derived origin, since it is meant to be invoked by a proxying frontend pallet, not by an end-user signature) can pass an arbitrary `sender`/`VersionedLocation`, exactly mirroring the impersonation primitive in the Solana finding where the malicious relayer/observer swapped `sender` for the destination-program call.

## Impact Explanation
- In `register_token`, the impersonated `sender_location` is converted into `message_origin` and forwarded with `amount` to `Self::send(message_origin, command, amount)`, which drives the Ethereum-bound `RegisterForeignToken` command and its associated fee accounting keyed to that `message_origin`. An attacker able to invoke this call with an arbitrary `sender` can attribute the command/fee to any victim location's identity on the Ethereum side.
- In `add_tip`, an attacker can attribute `LostTips` and `TipProcessed` accounting (used for future recovery/incentive bookkeeping) to an arbitrary victim account, corrupting the relayer-incentive ledger and potentially enabling a victim's `LostTips` balance to be inflated or an attacker's own liability to be shifted onto someone else.
- This falls under "theft or unbacked mint/unlock" / "duplicate settlement or payout to wrong beneficiary" impact categories: the `sender` identity used for fee/reward bookkeeping is forged relative to the actually authenticated origin.

## Likelihood Explanation
Any account/origin that satisfies `T::FrontendOrigin` — which is explicitly designed to authorize the "system frontend pallet" proxying end-user calls from AssetHub, not to authenticate the specific end user — can call `register_token`/`add_tip` directly with a forged `sender`. No malicious validator, relayer, or governance actor is required; this is directly reachable through the pallet's own public dispatchables by anything meeting the coarse-grained `FrontendOrigin` check, which does not perform any per-user binding.

## Recommendation
Do not accept `sender` as a free-form dispatch parameter. Either:
1. Require `T::FrontendOrigin::ensure_origin(origin)` to return the specific end-user `Location`/`AccountId` (analogous to how `GovernanceOrigin` is used in `upgrade`/`set_operating_mode`) and use that value directly instead of a caller-supplied `sender`, or
2. Cryptographically bind the passed `sender` to the identity asserted by the origin (e.g., verify that `sender_location` is a descendant of, or equal to, the location returned by `ensure_origin`) before using it for fee attribution or `message_origin` derivation.

## Proof of Concept
1. Attacker controls (or is) any principal satisfying `T::FrontendOrigin` (e.g., a specific XCM location permitted by the runtime's `FrontendOrigin` configuration for the AssetHub frontend pallet).
2. Attacker calls `register_token(origin, sender: victim_location, asset_id, metadata, amount)`.
3. `T::FrontendOrigin::ensure_origin(origin)` succeeds and its returned `Location` is discarded; the pallet proceeds using `sender_location = victim_location` supplied by the attacker.
4. `message_origin = Self::location_to_message_origin(victim_location)` and `Self::send(message_origin, command, amount)` execute with the victim's identity, attributing fee accounting/registration to the victim rather than the true caller — an exact structural analog of the Solana `sender` impersonation in `OnCall`.

### Citations

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L163-164)
```rust
			let origin_location = T::GovernanceOrigin::ensure_origin(origin)?;
			let origin = Self::location_to_message_origin(origin_location)?;
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L206-206)
```rust
		/// - `sender`: The original sender initiating the call on AH
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L211-241)
```rust
		pub fn register_token(
			origin: OriginFor<T>,
			sender: Box<VersionedLocation>,
			asset_id: Box<VersionedLocation>,
			metadata: AssetMetadata,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let sender_location: Location =
				(*sender).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
			let asset_location: Location =
				(*asset_id).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;

			let location = Self::reanchor(asset_location)?;
			let token_id = TokenIdOf::convert_location(&location)
				.ok_or(Error::<T>::LocationConversionFailed)?;

			if !ForeignToNativeId::<T>::contains_key(token_id) {
				ForeignToNativeId::<T>::insert(token_id, location.clone());
			}

			let command = Command::RegisterForeignToken {
				token_id,
				name: metadata.name.into_inner(),
				symbol: metadata.symbol.into_inner(),
				decimals: metadata.decimals,
			};

			let message_origin = Self::location_to_message_origin(sender_location)?;
			Self::send(message_origin, command, amount)?;
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L253-278)
```rust
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

			if let Err(ref e) = result {
				tracing::debug!(target: LOG_TARGET, ?e, ?message_id, ?amount, "error adding tip");
				LostTips::<T>::mutate(&sender, |lost_tip| {
					*lost_tip = lost_tip.saturating_add(amount);
				});
			}

			Self::deposit_event(Event::<T>::TipProcessed {
				sender,
				message_id,
				amount,
				success: result.is_ok(),
			});
```
