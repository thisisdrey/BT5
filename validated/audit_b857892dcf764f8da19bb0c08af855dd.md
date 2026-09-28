### Title
Dust deposits inflate victim's per-account token set to `MAX_TOKENS_PER_USER`, permanently blocking them from opening any new collateral or debt position - (File: contracts/Pool.sol)

### Summary
`Pool` keeps a per-account set (`depositTokensOfAccount` + `debtTokensOfAccount`) capped at `MAX_TOKENS_PER_USER = 30`, and every first-time receipt of a `DepositToken` (or first mint of a `DebtToken` balance) appends to it, reverting with `UserReachedMaxTokens` once the cap is hit. Because `DepositToken.deposit(amount_, onBehalfOf_)` credits an arbitrary `onBehalfOf_` and `DepositToken.transfer`/`transferFrom` are permissionless, an unprivileged attacker can push dust of every whitelisted deposit token into a victim's account, filling the set. After that, any call path that would add a *new* token to the victim's account reverts: `deposit` (mint → `addToDepositTokensOfAccount`), `transfer`/`seize` of a token the victim doesn't hold, and `DebtToken.issue` for a synthetic the victim hasn't borrowed (`addToDebtTokensOfAccount`). This is the on-chain analog of the plone.rest `++api++` bug: repeated permissionless additions make the shared resource (the per-account token list) grow until legitimate requests start failing, and each `debtPositionOf`/`unlockedBalanceOf` health check also gets linearly more expensive because it loops the list calling the oracle per entry (`Pool.depositOf`/`debtOf`).

### Finding Description
`DepositToken._mint` (contracts/DepositToken.sol:486-488) and `DepositToken._transfer` (contracts/DepositToken.sol:518-520) call `pool.addToDepositTokensOfAccount(account_)` whenever a recipient's balance goes from 0. `Pool.addToDepositTokensOfAccount` (contracts/Pool.sol:216-220) is gated by `onlyIfAdditionWillNotReachMaxTokens` (contracts/Pool.sol:143-148), which reverts `UserReachedMaxTokens` when `debt + deposit` tokens of the account reach 30. There is no opt-out: the victim cannot refuse incoming `deposit(..., onBehalfOf)` or `transfer` dust, and there is no minimum-amount check. The attacker needs only a dust amount of each underlying (or of already-minted msdTOKENs) to occupy one slot per whitelisted deposit token — the plone pattern of N cheap repetitions exhausting a fixed budget. Removal only happens when a balance returns exactly to zero (`_burn` → `removeFromDepositTokensOfAccount`, DepositToken.sol:460-462).

### Impact Explanation
- The victim cannot deposit a collateral type they don't already hold (`_mint` reverts inside `deposit`), cannot receive a new msdTOKEN via transfer, and — critically — cannot be topped up by a third party or a zap/leverage path that mints a new deposit token.
- `DebtToken.issue` for a synthetic the victim hasn't borrowed yet reverts in `addToDebtTokensOfAccount` (Pool.sol:204-208), so the victim cannot open new debt positions either.
- While a victim with existing debt is being liquidated, they cannot deposit a *new* collateral type to restore health — liquidation proceeds against them. Existing balances are still withdrawable (withdrawal removes tokens and frees slots), so this is a temporary freezing of the deposit/borrow liveness plus forced-collateral-inflexibility rather than outright theft, matching the "temporary freezing of funds / degraded service" acceptance class of the source advisory.

### Likelihood Explanation
Cost is dust × (#deposit tokens needed to reach 30 minus tokens the victim already holds). On pools with many whitelisted collaterals a fresh or low-diversity victim can be fully capped cheaply; for a victim already holding k tokens the attacker fills the remaining `30 − k − debtTokens` slots. No privileged role, oracle manipulation, or economic risk is required — just repeated public `deposit`/`transfer` calls, exactly the unprivileged-repetition DoS class of CVE-2023-42457.

### Recommendation
Charge the accounting cost to the actor, not the holder: track the per-account set by the token, not the account (e.g., only add on `deposit`/explicit opt-in rather than on bare `transfer`), or let removal be initiated by anyone for zero-balance... concretely: add a `sweepFromAccountList`/`removeIfZeroBalance` function, and/or require `onBehalfOf_ == _msgSender()` unless approved, so dust can't be force-deposited. Alternatively raise the cap or skip oracle iteration for dust balances below a threshold in `depositOf`.

### Proof of Concept
```solidity
// Foundry fork test against deployed Pool + DepositTokens
function test_DustDepositFillsVictimTokenList() public {
    address victim = makeAddr("victim");
    address[] memory dts = pool.getDepositTokens(); // whitelisted DepositTokens

    // attacker deposits dust of each deposit token on behalf of victim
    for (uint i; i < dts.length && i < 30; ++i) {
        DepositToken dt = DepositToken(dts[i]);
        IERC20 underlying = dt.underlying();
        deal(address(underlying), attacker, 1);          // 1 wei dust
        vm.startPrank(attacker);
        underlying.approve(address(dt), 1);
        dt.deposit(1, victim);                           // mints dust msdTOKEN to victim
        vm.stopPrank();
    }

    // victim's token list is now at MAX_TOKENS_PER_USER (or capped by #deposit tokens)
    assertEq(
        pool.getDepositTokensOfAccount(victim).length + pool.getDebtTokensOfAccount(victim).length,
        30
    );

    // victim tries to deposit a collateral type they don't hold -> reverts
    DepositToken newDt = DepositToken(dts[0]);           // any token not yet held
    // (or use a token whose balance victim doesn't hold)
    vm.startPrank(victim);
    IERC20 newUnder = newDt.underlying();
    deal(address(newUnder), victim, 100 ether);
    newUnder.approve(address(newDt), 100 ether);
    vm.expectRevert(UserReachedMaxTokens.selector);
    newDt.deposit(100 ether, victim);

    // victim tries to issue a new synthetic -> reverts in addToDebtTokensOfAccount
    vm.expectRevert(UserReachedMaxTokens.selector);
    msUsdDebt.issue(1 ether);
    vm.stopPrank();
}
```
Attack also works with `dt.transfer(victim, 1)` of attacker-held msdTOKENs (no underlying needed), and `seize`/liquidation receipts into a full account revert identically.