Based on my investigation, I found a concrete local analog: **`add_tip` is permissionless and irrevocably burns/swaps user funds against a bare `nonce`/`message_id` key that is never validated against the caller and can correspond to a message that will never be (or has already been) processed, permanently locking the tipper's funds with no refund path** — structurally the same broken invariant as the report (a caller-supplied ID, decoupled from payer identity, determines fund destiny, and no guard prevents loss when that ID's associated state diverges from the payer's expectation).

### Title
Permanent fund loss via unrefundable `add_tip` burn against an unvalidated/unconsumed nonce - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`, `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

### Summary
`snowbridge-pallet-system-frontend::add_tip` [1](#0-0)  lets any signed user irrevocably swap-and-burn an asset for Ether and dispatch an `AddTip` XCM Transact to Bridge Hub's `EthereumSystem::add_tip`, which lands on `snowbridge-pallet-inbound-queue-v2::Pallet::add_tip` [2](#0-1) . The tip is recorded purely against the numeric `nonce` in `Tips<T>` — a value chosen entirely by the caller, not bound to who they are or ownership of the underlying inbound message. The only guard is that the nonce must not already be consumed (`Nonce::<T>::get(nonce)`); there is no check that the nonce even corresponds to a message that will ever exist or be processed.

### Finding Description
The pattern mirrors the report's core defect: a fund-affecting operation is keyed by a caller-supplied identifier (`accountId` in the report, `message_id`/`nonce` here) that is independent of the payer's own identity or control. In the report, an attacker exploits this by occupying the ID before the rightful party's operation completes, redirecting funds. Here, the same "ID-not-bound-to-payer, no completion guarantee" flaw produces fund loss even without any attacker:

- The `swap_fee_asset_and_burn` step in `system-frontend::add_tip` burns the user's asset for teleport unconditionally, before any confirmation that the target `nonce`/message will ever be processed on Bridge Hub [3](#0-2) .
- On Bridge Hub, `inbound-queue-v2::add_tip` only requires `!Nonce::<T>::get(nonce)` [4](#0-3) ; it does not verify the nonce refers to an actual pending/valid Ethereum message. A user can tip a nonce that:
  - has already been consumed by the time the XCM Transact lands (race between frontend submission and BH processing), or
  - was never legitimately assigned/will never be submitted (typo, stale value, or one guessed ahead of time), or
  - belongs to a message the relayer network never picks up.
- If the nonce is never consumed via `process_message`, `Tips::<T>::take(nonce)` at line 235 is never called, and the tip amount remains permanently stranded in `Tips<T>` storage with the Ether already burned on the sending side — there is no extrinsic, event, or governance path in either pallet to reclaim it.
- Conversely, if the nonce is already consumed by the time the tip lands, `add_tip` returns `AddTipError::NonceConsumed`, but the Ether was already burned on the frontend chain in the same call sequence via `swap_fee_asset_and_burn`/`burn_for_teleport` *before* the cross-chain dispatch, and that burn is not atomic with, nor reversible upon, the remote failure.

This is directly analogous to the report's core broken invariant: **value is committed against an externally/self-chosen identifier before the state that identifier depends on is confirmed to exist/succeed**, and existing guards (`NonceConsumed` check) only prevent double-spending into an already-used slot — they do nothing to prevent the payer's own funds from being burned into a slot that will never be filled.

### Impact Explanation
This is a "permanent user-fund lock" per the impact gate: burned Ether tips corresponding to nonces that never get consumed are permanently unrecoverable, with no refund, reclaim, or expiry mechanism in `Tips<T>` or the frontend pallet. Because the burn (`swap_fee_asset_and_burn`/`burn_for_teleport`) happens unconditionally on the sending chain regardless of whether the remote `add_tip` ultimately succeeds, ordinary user error (stale/incorrect nonce, submitting after the message was already relayed) — not just malicious action — causes irreversible fund loss at the protocol level.

### Likelihood Explanation
Likelihood is highest under ordinary operation, since it requires no attacker: any legitimate user can lose funds via a nonce that resolves before their tip lands (race condition against the relayer network) or a benign nonce mistake, and the interface offers no protection or visibility into whether the tip will actually be applied before the burn occurs. Unprivileged users can trigger this using a normal `add_tip` call, satisfying the "public underpriced/unrecoverable work" style criteria without needing a malicious peer, relayer, or governance actor.

### Recommendation
- Make the cross-chain tip dispatch atomic with the burn: do not burn/swap the fee asset on the sending chain until the remote `add_tip` on Bridge Hub has confirmed the nonce is a valid, not-yet-consumed, still-pending inbound message (e.g., via a two-phase reserve/confirm, or reversible teleport/refund on `NonceConsumed`/`AddTipError`).
- Add an explicit existence check for the nonce (e.g., require it to correspond to a message that has been submitted but not yet processed, rather than merely "not yet consumed"), or provide a refund/expiry path for stranded `Tips<T>` entries.
- Emit a Bridge Hub event/return value that the frontend pallet can use to trigger a refund flow back to the original sender when `add_tip` fails.

### Proof of Concept
1. A user calls `snowbridge-pallet-system-frontend::add_tip(message_id, asset)` intending to tip an in-flight inbound message.
2. `swap_fee_asset_and_burn` swaps and burns the user's asset for Ether immediately, regardless of the remote outcome [5](#0-4) .
3. The XCM Transact carrying `EthereumSystemCall::AddTip { sender, message_id, amount }` is delivered to Bridge Hub, but by the time it executes, the corresponding nonce has already been consumed by `process_message` (a relayer got there first), so `Nonce::<T>::get(nonce)` is true and `add_tip` returns `AddTipError::NonceConsumed` [4](#0-3) .
4. The user's Ether, already burned in step 2, is never refunded to them, nor allocated as a reward — it is simply lost, matching the report's "gas fee theft" pattern of an ID-based fund commitment being invalidated by state changing out from under the payer, with no recovery path.

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L254-273)
```rust
		/// Add an additional relayer tip for a committed message identified by `message_id`.
		/// The tip asset will be swapped for ether.
		#[pallet::call_index(2)]
		#[pallet::weight(
			T::WeightInfo::add_tip()
				.saturating_add(T::BackendWeightInfo::transact_add_tip())
		)]
		pub fn add_tip(origin: OriginFor<T>, message_id: MessageId, asset: Asset) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: Into<Location>,
		{
			let who = ensure_signed(origin)?;

			let ether_gained = Self::swap_fee_asset_and_burn(who.clone().into(), asset)?;

			// Send the tip details to BH to be allocated to the reward in the Inbound/Outbound
			// pallet
			let call = Self::build_add_tip_call(who.clone(), message_id.clone(), ether_gained);
			Self::send_transact_call(who.into(), call)
		}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-259)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			// If the nonce is already processed, return an error
			ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed);
			// Otherwise add the tip.
			Tips::<T>::mutate(nonce, |tip| {
				*tip = Some(tip.unwrap_or_default().saturating_add(amount));
			});
			return Ok(());
		}
	}
```
