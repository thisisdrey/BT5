### Title
Attacker can fill a victim's per-account token list via dust `DepositToken` transfers, permanently DoS-ing new collateral deposits, debt issuance, and top-ups needed to avoid liquidation - (File: contracts/DepositToken.sol)

### Summary
`Pool` enforces `MAX_TOKENS_PER_USER = 30` across the sum of an account's deposit-token and debt-token lists (`Pool.sol:79`, `Pool.sol:143-148`). Any `DepositToken._transfer` (or `deposit(..., onBehalfOf_)`, `seize`, or `DebtToken._mint`) that takes an account's balance of a given token from 0 to non-zero pushes that token into the victim's `depositTokensOfAccount`/`debtTokensOfAccount` `MappedEnumerableSet` and reverts with `UserReachedMaxTokens` once the cap is hit. Because `DepositToken.transfer`/`transferFrom` impose no consent or minimum-amount check on the recipient (`DepositToken.sol:348-376`), an attacker can dust-transfer 1 wei of every registered deposit token into an arbitrary victim's address and permanently block that victim from ever receiving, depositing, or being issued any token not already in their list.

### Finding Description
The invariant that breaks is liveness of the victim's position-management surface. The path is:

1. Attacker deposits a dust amount of underlying into each of the pool's N registered `DepositToken`s, receiving dust `msdToken` balances.
2. Attacker calls `depositToken.transfer(victim, 1)` for each deposit token. `DepositToken._transfer` executes `pool.addToDepositTokensOfAccount(recipient_)` whenever `balanceOf[recipient_] == 0 && amount_ > 0` (`DepositToken.sol:518-520`).
3. `Pool.addToDepositTokensOfAccount` reverts only once `debtTokensOfAccount.length(account) + depositTokensOfAccount.length(account) >= 30` (`Pool.sol:143-148`, `Pool.sol:216-220`). After ~30 dust transfers, the victim's list is full.
4. Thereafter, every operation that would add a *new* token to the victim's list reverts inside the modifier:
   - `DepositToken.deposit(amount_, victim)` for any collateral the victim doesn't already hold (`_mint` → `addToDepositTokensOfAccount`, `DepositToken.sol:486-488`)
   - `DepositToken.transfer`/`transferFrom`/`seize` of any new token to the victim
   - `DebtToken.issue` / `SmartFarmingManager.leverage` issuing a new debt token (`DebtToken.sol:598-600`)
   - The victim can additionally fill residual slots with debt issuance themselves, but the attacker can front-run or simply combine dust `msdToken` transfers to occupy all 30 slots.

Note the victim cannot remove entries they hold a non-zero (even 1 wei) balance of, and cannot preempt the attacker: the attacker can dust all registered deposit tokens in one transaction via `Operator.execute` or a helper contract, before the victim has any chance to react. Removing a token requires the victim's balance to reach exactly 0, which requires withdrawing/transferring the dust — but each such cleanup transaction can be immediately re-filled by the attacker's next dust transfer of the same or another token.

### Impact Explanation
Temporary/partial freezing of user funds and facilitated liquidation:

- A victim with an unhealthy position who holds only collateral types already in their list can still top up those tokens, but cannot add any *new* collateral type or new debt instrument. If their existing collateral is depreciating (or they hold none of a desired token), they are barred from restoring health via the obvious route and can be liquidated.
- If the victim currently holds 0 balance of any token, *every* deposit on their behalf (including NativeTokenGateway/VesperGateway deposits and SmartFarmingManager leverage) reverts — full DoS of position opening and recovery.
- The attack costs only dust deposits and gas, needs no privileged role, and works through `Operator.execute` batching.

### Likelihood Explanation
Likelihood is moderate: it requires the attacker to acquire dust positions across enough registered deposit tokens to fill 30 slots (deposit + debt tokens share the cap). On pools with several deposit tokens this is cheap; the attack is fully permissionless, uses only public entry points, and is unaffected by pause flags (transfers aren't pausable), SynthContext checks, or reentrancy guards. Griefing of this kind is most profitable when combined with a liquidation opportunity on the victim.

### Recommendation
- Only add a token to `depositTokensOfAccount` when the credit originates from a state-changing deposit/mint/seize by the account or an authorized flow — e.g., skip `addToDepositTokensOfAccount` in `DepositToken._transfer` for plain user transfers, or require a minimum non-dust amount.
- Alternatively, make `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` non-reverting at the cap (return false) and have callers handle it, or allow recipients to "opt out" and prune unsolicited dust entries.
- Consider excluding received-but-not-deposited transfer balances from `depositTokensOfAccount` accounting.

### Proof of Concept
```solidity
// Foundry fork-style PoC outline
function test_DustFillsVictimTokenList() public {
    address victim = makeAddr("victim");
    address attacker = makeAddr("attacker");

    IDepositToken[] memory dts = getAllDepositTokens(pool); // N >= MAX_TOKENS_PER_USER - len(victim's debt tokens)

    for (uint256 i; i < dts.length; ++i) {
        IERC20 underlying = dts[i].underlying();
        deal(address(underlying), attacker, 1e6);
        vm.startPrank(attacker);
        underlying.approve(address(dts[i]), 1e6);
        dts[i].deposit(1e6, attacker);       // attacker mints dust msdToken to self
        dts[i].transfer(victim, 1);          // pushes dts[i] into victim's depositTokensOfAccount
        vm.stopPrank();
    }

    assertEq(pool.getDepositTokensOfAccount(victim).length, 30);

    // Victim (or anyone) can no longer deposit a *new* collateral type for the victim,
    // nor can the victim be issued a new debt token / receive any new msdToken:
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    vm.prank(makeAddr("depositor"));
    newDepositToken.deposit(1 ether, victim); // reverts inside _mint -> addToDepositTokensOfAccount
}
```

Limitations/uncertainty: the victim retains the ability to withdraw existing collateral and deposit *more* of tokens already in their list, so the impact is blocking of new-position/diversification paths and forced-liquidation facilitation rather than permanent loss of all funds. Whether a pool actually registers enough deposit tokens to reach the 30-slot cap (counted jointly with debt tokens, which the victim's own debts also occupy) depends on deployed configuration; if a pool registers fewer than 30 tokens total the attack is not fully executable there.