Audit Report

## Title
Missing slippage protection in Snowbridge `system-frontend` fee-swap lets any user's tip/registration payment be drained via price manipulation - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

## Summary
`Pallet::add_tip` and the non-root path of `Pallet::register_token` route the caller's fee/tip asset through `swap_fee_asset_and_burn` → `swap_and_burn`, which calls `T::Swap::swap_exact_tokens_for_tokens(..., None, ...)` with a hard-coded `None` for `amount_out_min` at `bridges/snowbridge/pallets/system-frontend/src/lib.rs` line 305. This disables the slippage check that `pallet_asset_conversion`'s own public dispatchable enforces, so an attacker who manipulates the AMM pool immediately before a victim's transaction can force `ether_gained` toward zero while the victim's tip/fee asset is fully withdrawn and burned.

## Finding Description
`swap_and_burn` at lines 290-317 unconditionally passes `None` as the `amount_out_min` argument to `T::Swap::swap_exact_tokens_for_tokens` (line 301-308):
```rust
let ether_gained = T::Swap::swap_exact_tokens_for_tokens(
    who.clone(),
    swap_path,
    tip_amount,
    None, // No minimum amount required
    who,
    true,
)?;
```
Because `amount_out_min` is `None`, `pallet_asset_conversion::Pallet::do_swap_exact_tokens_for_tokens`'s guard `ensure!(amount_out >= amount_out_min, Error::<T>::ProvidedMinimumNotSufficientForSwap)` is skipped entirely (this check only fires `if let Some(amount_out_min) = amount_out_min`), so whatever output the pool yields at that instant is accepted unconditionally.

Both call sites are reachable by unprivileged signed accounts: `add_tip` (lines 261-273) is gated only by `ensure_signed`, and `register_token`'s swap branch (lines 225-252, `else` branch at line 241) executes for any non-`Here` origin. In both cases the full `fee_amount`/`tip_amount` is withdrawn and burned via `burn_for_teleport` regardless of how unfavorable the swap execution was — the loss is realized on-chain immediately, independent of what `ether_gained` ends up being.

Existing protection elsewhere in the codebase is real but not applied here: `pallet_asset_conversion`'s own public `swap_exact_tokens_for_tokens` extrinsic requires and enforces a caller-supplied minimum. `system-frontend` is the outlier that opts out of this mechanism by construction, not due to any structural limitation — the `Swap` trait signature already accepts an `Option<Balance>` minimum, but the pallet always passes `None`.

## Impact Explanation
This satisfies the "public underpriced work that degrades block production or stalls bridge processing" and fund-loss/payout-integrity categories in scope. `ether_gained` — the exact value forwarded in the `RegisterToken`/`AddTip` Transact call to BridgeHub and used to size the relayer reward or registration fee — can be manipulated down to a negligible amount by sandwiching the victim's extrinsic, while the victim's tip/fee asset is fully and irreversibly burned. This degrades the intended relayer-reward incentive (potentially stalling outbound message processing since tips go unrewarded) and destroys user funds without delivering the intended benefit.

## Likelihood Explanation
Any unprivileged, signed account can call `add_tip` with an arbitrary `Asset`/amount, and the underlying `pallet_asset_conversion` pools are themselves publicly tradable via ordinary `swap_exact_tokens_for_tokens`/liquidity extrinsics. No governance, admin, relayer, or validator privilege is required — a plain sandwich (front-run/back-run) against a thin liquidity pool for a fee asset is sufficient, and the vulnerability is a missing parameter in code, not merely an environmental condition.

## Recommendation
Add an explicit minimum-output parameter to `add_tip`/`register_token` (or derive one from a recent on-chain price quote with a bounded tolerance), and thread it through `swap_fee_asset_and_burn`/`swap_and_burn` into `T::Swap::swap_exact_tokens_for_tokens` instead of hard-coding `None`, mirroring the `ProvidedMinimumNotSufficientForSwap` check already enforced in `pallet_asset_conversion::Pallet::swap_exact_tokens_for_tokens`.

## Proof of Concept
1. Deploy `system-frontend` with `T::Swap` pointed at a `pallet_asset_conversion` pool for a low-liquidity fee-asset/Ether pair.
2. Attacker calls `AssetConversion::swap_exact_tokens_for_tokens` (or add/remove liquidity) to push the fee-asset price down just before the victim's transaction is included.
3. Victim calls `add_tip(message_id, asset)`; `swap_and_burn` (lines 290-317) executes `T::Swap::swap_exact_tokens_for_tokens(..., None, ...)`, accepting the manipulated `ether_gained` with no `ensure!(amount_out >= amount_out_min)` check, unlike the analogous check in `substrate/frame/asset-conversion/src/lib.rs` (lines 988-1004).
4. Attacker reverses the initial trade, pocketing the price difference; the victim's full tip asset is burned via `burn_for_teleport` for a near-zero `ether_gained`, which is embedded in the `AddTip` Transact call sent to BridgeHub.