### Title
Unprivileged attacker can dust-fill a victim's per-account token list (`MAX_TOKENS_PER_USER`) to DoS deposits, mints and collateral top-ups - (File: contracts/Pool.sol)

### Summary
`Pool` tracks every deposit/debt token an account holds via `MappedEnumerableSet.AddressSet` (`depositTokensOfAccount`, `debtTokensOfAccount`, `contracts/storage/PoolStorage.sol:78-83`) and enforces a combined cap of `MAX_TOKENS_PER_USER = 30` through `onlyIfAdditionWillNotReachMaxTokens` (`contracts/Pool.sol:143-148`). `DepositToken._transfer` adds the token to the *recipient's* list whenever the recipient's prior balance is zero (`contracts/DepositToken.sol:518-520`). Because `transfer`/`transferFrom` are permissionless ERC-20 entry points that only require the *sender's* balance to be unlocked (`contracts/DepositToken.sol:348-376`), any EOA can push dust amounts of msdTOKENs to a victim and grow the victim's `depositTokensOfAccount` set without consent. Once the combined count hits 30, every subsequent call that would add a *new* token to the victim's account reverts with `UserReachedMaxTokens`.

### Finding Description
- `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` are gated by `onlyIfAdditionWillNotReachMaxTokens`, reverting when `debtTokensOfAccount + depositTokensOfAccount >= 30` (`contracts/Pool.sol:143-148`); confirmed by the unit test `should revert when reach max tokens` (`test/Pool.test.ts:1386-1416`).
- `DepositToken._transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` on a zero-balance recipient (`contracts/DepositToken.sol:517-520`). There is no opt-out and no minimum amount — a 1-wei transfer permanently occupies a slot until the victim fully zeroes that token balance.
- The same add path is hit by `_mint` (`contracts/DepositToken.sol:486-488`), so `deposit()` (which mints to `onBehalfOf_`, `contracts/DepositToken.sol:234`), `DebtToken.issue`/`leverage` (which mint new debt tokens), `Pool.liquidate` → `seize` → `_transfer`, and any incoming `transfer`/`transferFrom` of a new token all revert for a victim whose list is full.
- Nothing prevents the attack: `transfer` only checks the sender's unlocked balance; `SynthContext._msgSender` meta-sender does not add any restriction; pause/shutdown flags only widen the DoS.

### Impact Explanation
Analogous to the CVE's "repeatable crash / hang" availability loss: an unprivileged attacker can deterministically make protocol features permanently revert for a targeted account. Concretely:
- The victim cannot deposit into any collateral type they don't already hold — including the emergency top-up needed to push an unhealthy position back above the liquidation threshold, enabling forced liquidation of the victim (loss of collateral to the liquidator).
- The victim cannot issue any new synthetic debt token and cannot receive any new msdTOKEN.
- The state is persistent: each forced slot stays occupied until the victim burns/transfers out 100% of that dusted token, and the attacker can re-fill freed slots cheaply.

This is not a gas/unbounded-loop DoS — it is a bounded, state-corrupting availability attack against a specific account.

### Likelihood Explanation
The attack costs only dust transfers of whitelisted deposit tokens (the attacker must first acquire or deposit a negligible amount of each underlying). Its feasibility depends on the number of registered `depositTokens` in the target pool: the attacker needs `30 - victimDebtTokens` distinct deposit tokens. On pools with many registered collaterals (or where the victim already holds several debt tokens), only a handful of dust transfers are needed. Cost per slot is a single cheap ERC-20 transfer, repeatable and front-runnable. Uncertainty: the exact count of registered deposit tokens per deployed pool was not enumerated from the index; on a pool with very few deposit tokens the cap may be unreachable.

### Recommendation
- Do not add tokens to `depositTokensOfAccount` on plain `transfer`/`transferFrom` (recipient-side), or require an opt-in/registry flag for unsolicited inbound positions.
- Alternatively, only count tokens toward `MAX_TOKENS_PER_USER` when added via `deposit()`/`issue()`/`seize()` code paths, not via permissionless transfers, or raise the cap materially and revert only for protocol-minted additions.
- Allow `addToDepositTokensOfAccount` to skip/replace slots with dust balances rather than reverting.

### Proof of Concept
```solidity
// Foundry fork test against a deployed Pool/DepositToken
// Assumes: pool has >= N registered deposit tokens; victim holds d debt tokens
function test_DustFillVictimTokenList() public {
    address victim = alice;                    // healthy or near-liquidation position
    uint256 needed = pool.MAX_TOKENS_PER_USER()
        - pool.getDebtTokensOfAccount(victim).length
        - pool.getDepositTokensOfAccount(victim).length;

    address[] memory dts = /* registered deposit tokens */;
    for (uint256 i; i < needed; ++i) {
        // attacker deposits a dust amount of underlying i to obtain msdToken
        IERC20(depToken[i].underlying()).approve(address(depToken[i]), 1);
        depToken[i].deposit(1, attacker);
        // dust-transfer to victim -> occupies a slot in depositTokensOfAccount
        depToken[i].transfer(victim, 1);
    }

    // Victim cannot deposit into a NEW collateral type anymore
    vm.startPrank(victim);
    IERC20(newDep.underlying()).approve(address(newDep), 1e18);
    vm.expectRevert(UserReachedMaxTokens.selector);
    newDep.deposit(1e18, victim);

    // Nor mint a new debt token
    vm.expectRevert(UserReachedMaxTokens.selector);
    debtTokenB.issue(1, victim);
    vm.stopPrank();
}
```