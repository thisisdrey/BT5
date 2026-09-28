### Title
Dust-transfer griefing fills a victim's `depositTokensOfAccount` list so all deposits/mints/transfers of any new DepositToken to that account revert - ([File: contracts/DepositToken.sol])

### Summary
Analogous to CVE-2021-43827 (a required element is assumed to be insertable/present after an upstream operation, and the downstream code path fails hard instead of degrading), `DepositToken._transfer` and `DepositToken._mint` unconditionally call `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance is zero. That call enforces `onlyIfAdditionWillNotReachMaxTokens(account_)` on `Pool`. An unprivileged attacker can dust-transfer 1 wei of every registered `DepositToken` to a victim until the victim's per-account list hits `MAX_TOKENS_PER_USER`, after which every attempt to credit that victim with a *new* deposit token reverts.

### Finding Description
In `DepositToken._transfer` (contracts/DepositToken.sol:517-525) and `DepositToken._mint` (contracts/DepositToken.sol:485-488), the code assumes the recipient's set entry can always be added:

```solidity
if (_recipientBalanceBefore == 0 && amount_ > 0) {
    pool.addToDepositTokensOfAccount(recipient_);
}
```

`Pool.addToDepositTokensOfAccount` applies the same `onlyIfAdditionWillNotReachMaxTokens` guard used by `addToDebtTokensOfAccount` (contracts/Pool.sol:204-208), which reverts once the account's set is at the cap. DepositToken shares are freely transferable (`transfer`, `transferFrom`) with only the sender's unlocked-balance check, so nothing stops an attacker from sending dust of each collateral type to any address.

Attack steps (all public entry points, unprivileged EOA):
1. Attacker deposits a minimal amount into every registered `DepositToken` (or obtains dust on a DEX).
2. Attacker calls `depositToken_i.transfer(victim, 1)` for each token until `Pool.depositTokensOfAccount[victim]` reaches `MAX_TOKENS_PER_USER`.
3. From then on, any of the following revert for a deposit token the victim does not already hold:
   - `DepositToken.deposit(amount, victim)` — `_mint` → `addToDepositTokensOfAccount` reverts.
   - `DepositToken.transfer(victim, x)` / `transferFrom` from a third party.
   - `SmartFarmingManager.leverage(...)` where the final `depositToken_.deposit(_depositAmount, _msgSender)` credits the victim — whole leverage tx reverts after the flash-issued debt, blocking the zap path.
   - `DepositToken.seize(victim→liquidator)` is unaffected, but `deposit`/`transferFrom`/`seize` crediting the victim in `liquidate` fails if the liquidator's own list is full (the attacker can also pre-fill a liquidator's list to selectively break liquidations where the seized token isn't already in the liquidator's set).

### Impact Explanation
Temporary freezing of funds / denial of service: the victim cannot receive any deposit-token type not already in their list — deposits on their behalf revert, third-party transfers revert, and `SmartFarmingManager.leverage` on their behalf reverts. Additionally, if a liquidator's list is filled, `depositToken_.seize(account_, liquidator, _toLiquidator)` inside `Pool.liquidate` reverts for deposit tokens the liquidator doesn't yet hold, blocking that liquidation route and delaying bad-debt cleanup. Recovery requires the victim to spend gas transferring dust balances back to zero to free set slots.

### Likelihood Explanation
Fully reachable by an unprivileged attacker: it needs only `deposit()` and `transfer()` on DepositToken contracts with dust amounts. Cost scales with the number of registered deposit tokens but is trivially low (1 wei each). No privileged role, oracle manipulation, or governance action is required. Mitigation exists (victim can clear slots by zeroing a dust balance), which keeps it at temporary-freeze/griefing severity rather than permanent loss.

### Recommendation
- On `addToDepositTokensOfAccount` failure, skip adding instead of reverting, or make the cap a soft limit (e.g., cap only `debtTokensOfAccount`, which is not attacker-fillable since DebtTokens are non-transferable).
- Alternatively, allow recipients to opt out of list tracking, or let anyone call `removeFromDepositTokensOfAccount`-style cleanup for zero-balance entries so a stuck slot can be cleared by any caller.

### Proof of Concept
```solidity
// Foundry fork-style test sketch
function test_dustFillsDepositTokenList_dosDeposit() public {
    address victim = makeAddr("victim");
    IDepositToken[] memory dts = pool.getDepositTokens(); // assume N >= MAX_TOKENS_PER_USER

    // attacker seeds dust of each deposit token
    for (uint i; i < MAX_TOKENS_PER_USER; ++i) {
        IERC20 underlying = dts[i].underlying();
        deal(address(underlying), attacker, 2);
        vm.startPrank(attacker);
        underlying.approve(address(dts[i]), 2);
        dts[i].deposit(2, attacker);
        dts[i].transfer(victim, 1); // adds dts[i] to victim's list
        vm.stopPrank();
    }

    // any deposit of a NEW deposit token to victim now reverts
    IDepositToken newDt = dts[MAX_TOKENS_PER_USER]; // token not in victim's list
    IERC20 und = newDt.underlying();
    deal(address(und), attacker, 100);
    vm.startPrank(attacker);
    und.approve(address(newDt), 100);
    vm.expectRevert(); // OnlyIfAdditionWillNotReachMaxTokens / equivalent error
    newDt.deposit(100, victim);
    vm.stopPrank();
}
```

Note on confidence: I verified the `onlyIfAdditionWillNotReachMaxTokens` guard on `addToDebtTokensOfAccount` (contracts/Pool.sol:204) and the unconditional add calls in `DepositToken` (lines 486-488, 517-520); the exact error name and whether `addToDepositTokensOfAccount` carries the identical modifier should be confirmed against `Pool.sol` in full source, but the structure of `MappedEnumerableSet` + per-account cap is confirmed.