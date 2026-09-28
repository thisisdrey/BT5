### Title
Unprivileged users can permanently occupy a victim’s collateral slots with dust and block new deposits - (File: contracts/Pool.sol)

### Summary

`Pool` limits each account to 30 combined debt and deposit-token entries. `DepositToken._transfer` automatically inserts any first-time recipient’s token into that per-account list, and `Pool.addToDepositTokensOfAccount` reverts once the combined limit is reached. Because deposit tokens are freely transferable and zero-value transfers are prohibited rather than zero-resulting balances, an attacker can send one unit of every registered deposit token to a target, occupy the account’s slots, and prevent the target from receiving or depositing a new collateral type.

When the target has debt, the dust positions can be locked by `_revertIfLocked`, so the victim cannot necessarily remove them through `transfer` or `withdraw`. This can block the exact action needed to restore account health and expose the victim to liquidation.

### Finding Description

`Pool.MAX_TOKENS_PER_USER` is fixed at 30. The limit is enforced globally against the sum of a user’s debt-token and deposit-token sets in `onlyIfAdditionWillNotReachMaxTokens` at `contracts/Pool.sol:143-146`.

A deposit-token transfer invokes `DepositToken._transfer`. If the recipient previously had a zero balance and receives a nonzero amount, the token calls `pool.addToDepositTokensOfAccount(recipient_)` at `contracts/DepositToken.sol:517-520`. There is no recipient opt-in and no minimum economically meaningful amount.

The pool-side insertion checks the account-wide limit before validating that the calling token is registered at `contracts/Pool.sol:216-219`. Consequently, once a victim’s combined list contains 30 entries, even a legitimate deposit or transfer of a different registered collateral reverts before the deposit token is added.

Removal normally occurs when a token balance reaches zero in `DepositToken._transfer` at `contracts/DepositToken.sol:522-525` or `_burn` at `contracts/DepositToken.sol:459-462`. However, both public withdrawal paths and ordinary transfers first pass `_revertIfLocked` at `contracts/DepositToken.sol:180-182`, `contracts/DepositToken.sol:348-352`, and `contracts/DepositToken.sol:406-411`. The unlocked amount is derived from `Pool.debtPositionOf` at `contracts/DepositToken.sol:383-397`, so a dust position may be entirely locked for an indebted or narrowly collateralized victim.

The analogous authorization failure is that the recipient does not authorize the creation of a scarce per-account accounting entry. The sender alone can create persistent state that gates the recipient’s future protocol operations.

### Impact Explanation

An attacker can temporarily freeze the victim’s ability to add collateral. If the victim already has debt and all allowed entries are occupied, the victim cannot deposit a new collateral token or receive a new deposit-token type to improve health.

This is most consequential when the victim’s position becomes unhealthy before the dust entries can be removed. `Pool.liquidate` remains callable by any third party once `debtPositionOf(account_)` reports unhealthy at `contracts/Pool.sol:537-563`. Blocking collateral additions can therefore convert an otherwise recoverable position into a liquidatable one, causing loss of collateral through liquidation.

The impact is bounded to temporary freezing/prevention of rescue rather than direct theft by the attacker, but the accepted impact category includes temporary freezing of funds and externally forced liquidation loss.

### Likelihood Explanation

The attack requires the victim’s combined deposit-token and debt-token list to reach 30 entries. The attacker must acquire at least one transferable unit of enough distinct registered deposit tokens and send them to the victim. Existing debt-token entries count toward the same limit, reducing the number of dust collateral entries required.

No privileged role, oracle failure, malicious endpoint, governance action, or contract deployment is needed. The attacker uses only public ERC-20-style `transfer` calls on registered `DepositToken` contracts.

Feasibility depends on the deployed pool having enough registered deposit-token offerings to reach the victim’s remaining capacity. If a deployed pool has fewer registered deposit tokens than the available slots and the victim has no debt-token entries, this attack cannot reach the limit.

### Recommendation

Do not create per-account collateral entries as a side effect of permissionless transfers without recipient consent, or exempt economically insignificant balances from the limit.

Possible fixes include:

- add a minimum first-balance threshold before calling `addToDepositTokensOfAccount`;
- let users remove zero-value or dust entries through a dedicated cleanup path that bypasses lock checks;
- apply `MAX_TOKENS_PER_USER` separately to debt and collateral instead of to their sum;
- make collateral-token accounting opt-in;
- allow liquidation, repayment, and collateral-adding operations to proceed even when enumeration metadata is at capacity.

A cleanup mechanism must preserve solvency: a nonzero locked balance cannot simply be ignored by `depositOf`, or users could bypass collateral checks.

### Proof of Concept

Conceptual Foundry sequence against a deployed-pool fork:

```solidity
function testDustCollateralSlotsBlockNewDeposit() public {
    address victim = makeAddr("victim");
    address attacker = makeAddr("attacker");

    // Setup: victim has an existing debt position and little or no
    // remaining issuable collateral capacity.

    uint256 existingDebtEntries = pool.getDebtTokensOfAccount(victim).length;
    uint256 existingDepositEntries = pool.getDepositTokensOfAccount(victim).length;
    uint256 slotsLeft = pool.MAX_TOKENS_PER_USER()
        - existingDebtEntries
        - existingDepositEntries;

    address[] memory depositTokens = pool.getDepositTokens();
    require(depositTokens.length >= slotsLeft, "not enough offerings");

    vm.startPrank(attacker);

    for (uint256 i; i < slotsLeft; ++i) {
        IDepositToken token = IDepositToken(depositTokens[i]);

        // Give attacker a small position, then send one nonzero unit
        // to the victim. This creates a new per-account list entry.
        token.transfer(victim, 1);

        assertTrue(
            contains(
                pool.getDepositTokensOfAccount(victim),
                address(token)
            )
        );
    }

    vm.stopPrank();

    assertEq(
        pool.getDebtTokensOfAccount(victim).length
            + pool.getDepositTokensOfAccount(victim).length,
        pool.MAX_TOKENS_PER_USER()
    );

    // Any different registered deposit token not already in the list now
    // fails during _transfer/_mint -> addToDepositTokensOfAccount.
    IDepositToken newCollateral = IDepositToken(depositTokens[slotsLeft]);

    vm.prank(attacker);
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    newCollateral.transfer(victim, 1);

    // If the dust collateral is locked by the victim's debt position,
    // the victim cannot clear the occupied entry.
    vm.prank(victim);
    vm.expectRevert(DepositToken.NotEnoughFreeBalance.selector);
    IDepositToken(depositTokens[0]).transfer(attacker, 1);
}
```

The concrete fork proof should use the deployed registry to select the pool and its registered `depositTokens`, seed the attacker with the required tokens, and configure a victim whose debt leaves the dust collateral locked. If the inspected deployment does not expose enough distinct deposit-token entries to fill the victim’s remaining 30 slots, the issue is not reachable on that deployment.