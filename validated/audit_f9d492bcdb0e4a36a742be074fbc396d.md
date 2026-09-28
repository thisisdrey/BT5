### Title
VesperGateway.deposit credits pre-existing stray vToken balance to the caller - (File: contracts/VesperGateway.sol)

### Summary
The ATM LP-burn exploit's bug class is "assets that land in a contract's own balance can be claimed by any caller through a public entry point." The Metronome analog is `VesperGateway.deposit`: it measures the minted vToken amount as a balance-delta of the gateway contract (`balanceOf(this) after - before`) instead of the return value of `vToken_.deposit()`. Any vTokens already sitting in the gateway — e.g., mistakenly transferred there by a user — are folded into the caller's delta and converted into `msdToken` collateral credited to the caller. [1](#0-0) 

### Finding Description
In `deposit(IPool pool_, IVPool vToken_, uint256 amount_)`:

1. The gateway pulls `amount_` of `underlying` from the caller via `transferFrom` (line 51).
2. It records `_balanceBefore = vToken_.balanceOf(address(this))`, calls `vToken_.deposit(amount_)`, and computes `_vTokenAmount` as the post-minus-pre delta (lines 56-58).
3. It approves and deposits the *entire delta* into `pool_.depositTokenOf(vToken_)`, minting `msdToken`s to `_msgSender()` (lines 61-64).

Because the delta is measured against the contract's own balance, any vToken balance held by the gateway before the call — donated tokens, tokens sent by mistake, leftovers from a previous `withdraw` flow — is swept into the caller's deposit. The caller receives `msdToken`s (interest-bearing, withdrawable collateral claims) for tokens they never supplied.

### Impact Explanation
Direct theft of funds stuck in the contract: an attacker can monitor for stray vToken balances in `VesperGateway` and, with a minimal `amount_` deposit (even 1 wei of underlying), claim the whole stray balance as freshly minted `msdToken`s, then withdraw the underlying collateral via `DepositToken.withdraw`. The invariant broken is deposit attribution — collateral minted must equal what the caller contributed. `msdToken`s are fully backed here, so the protocol isn't insolvent; the stolen value is the stray vTokens, which is exactly the same impact shape as the reference incident (a mistaken transfer redeemed by the first bot to call `burn`).

### Likelihood Explanation
- Public, permissionless entry point: `deposit` only requires `pool_` to be registered and has no `onlyGovernor`/keeper gating; `vToken_` just must map to a `DepositToken` via `pool_.depositTokenOf`.
- Precondition: a nonzero stray vToken balance in the gateway. This is plausible — the class-defining incident itself shows users routinely transfer tokens to contract addresses by mistake, and the gateway also briefly holds vTokens mid-flow. Any accidental direct `vToken.transfer(gateway, ...)` becomes claimable by the next caller.
- The `sweep` escape hatch is governor-only and does not prevent the leak — stray balances remain claimable until governance acts (`TokenHolder.sweep` at contracts/utils/TokenHolder.sol:37-45, `_requireCanSweep` gated at contracts/VesperGateway.sol:102).

### Recommendation
Do not use contract-balance deltas where attribution matters. In `VesperGateway.deposit`, use the return value of `vToken_.deposit()` (or `underlying`-denominated shares) for `_vTokenAmount`, or snapshot-and-sweep stray balances only via the governor `sweep`. If delta accounting is retained (e.g., for fee-on-transfer underlyings), first `sweep`/reconcile any pre-existing `vToken_` balance before measuring, or credit only `sharesMinted` returned by the Vesper pool.

### Proof of Concept
Foundry-style sketch (requires deployed/mocked `IPoolRegistry`, registered `IPool`, real or mock `IVPool` vToken and its `DepositToken`):

```solidity
// setup: victim (or donation) leaves stray vTokens in the gateway
vToken.transfer(address(gateway), strayAmount);          // strayAmount > 0

// attacker calls deposit with dust
uint256 dust = 1;
underlying.approve(address(gateway), dust);
uint256 msdBefore = depositToken.balanceOf(attacker);
gateway.deposit(pool, vToken, dust);
uint256 msdMinted = depositToken.balanceOf(attacker) - msdBefore;

// msdMinted corresponds to (dust-derived shares + strayAmount):
// delta = vToken.balanceOf(gateway_after) - vToken.balanceOf(gateway_before)
//        includes the pre-existing strayAmount
assertGt(msdMinted, depositToken.quoteDepositOut(strayAmount));
```

The single call `VesperGateway.deposit(pool_, vToken_, dust)` both proves reachability and theft, since the stray balance is folded into `_vTokenAmount` at contracts/VesperGateway.sol:56-64.

### Citations

**File:** contracts/VesperGateway.sol (L56-64)
```text
        uint256 _balanceBefore = vToken_.balanceOf(address(this));
        vToken_.deposit(amount_);
        uint256 _vTokenAmount = vToken_.balanceOf(address(this)) - _balanceBefore;

        // 3. Deposit `VPool` to `Synth` and send `msdTokens` to the `_msgSender()`
        IDepositToken _depositToken = pool_.depositTokenOf(vToken_);
        vToken_.safeApprove(address(_depositToken), 0);
        vToken_.safeApprove(address(_depositToken), _vTokenAmount);
        _depositToken.deposit(_vTokenAmount, _msgSender);
```
