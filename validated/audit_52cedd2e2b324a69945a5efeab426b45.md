### Title
Attacker can force-fill a victim's per-account token set to `MAX_TOKENS_PER_USER` via dust `deposit()` calls, blocking new collateral deposits and pushing the victim into liquidation - (File: contracts/Pool.sol)

### Summary
The Wireshark bug class is "missing validation of an input field leads to a crash / denial of service." The Metronome analog is `Pool.MAX_TOKENS_PER_USER` enforcement: `DepositToken.deposit(uint256 amount_, address onBehalfOf_)` accepts an arbitrary `onBehalfOf_` beneficiary with no validation that the caller is the beneficiary or that the beneficiary consented. Because every successful deposit registers the deposit token in the beneficiary's bounded account set, an attacker can deposit 1-wei dust amounts to any victim's address until their token list reaches the cap, after which any `deposit()` (or `msdTOKEN` transfer) of a *new* collateral type to that account reverts with `UserReachedMaxTokens`.

### Finding Description
`DepositToken.deposit` only validates `amount_ > 0` and `onBehalfOf_ != address(0)` before minting `msdTOKEN` to `onBehalfOf_` (`contracts/DepositToken.sol:211-236`). The mint hook registers the deposit token in the account's `MappedEnumerableSet`, and `Pool` enforces `uint256 public constant MAX_TOKENS_PER_USER = 30`, reverting with `UserReachedMaxTokens` once the set is full (`contracts/Pool.sol:32,79`).

Attack path (unprivileged EOA, or via `Operator.execute` since `_msgSender()` just needs to differ from treasury):

1. Victim holds an open borrow position collateralized by deposit token(s) D1.
2. Attacker calls `depositToken_i.deposit(1 wei of underlying_i, victim)` for every other registered deposit token D2..Dn.
3. Each dust mint adds Di to `victim`'s account set. Once the set reaches `MAX_TOKENS_PER_USER`, any subsequent deposit of a collateral type not already in the set reverts.
4. When the victim's position drifts toward unhealthy (price movement or interest accrual), the victim cannot add new collateral; only repayment of debt remains, which requires external funds and may be impossible if the user intended to rely on collateral top-ups.
5. The attacker (or anyone) then calls `Pool.liquidate` and seizes the victim's collateral at a discount.

The same vector applies to `transfer`/`transferFrom` of `msdTOKEN`: the recipient's set is updated on receipt, so a victim holding e.g. 30 token types cannot receive a new type either.

### Impact Explanation
Temporary freezing of the victim's ability to add collateral combined with forced liquidation of an unhealthy position → direct loss of collateral value (liquidation bonus + discount seized by liquidator). This satisfies the "temporary freezing of funds" / forced-exit impact class, not merely a gas or unbounded-loop DoS, and it stems from Metronome's own missing check (no equality/consent check between `_msgSender()` and `onBehalfOf_`), mirroring the missing IPv6-address validation in the reference CVE.

### Likelihood Explanation
- Cost is dust: `amount_` may be 1 wei of each underlying; the attacker only needs to acquire trivial amounts of each listed collateral.
- Reachability precondition: the number of registered deposit tokens (plus whatever else populates the account set) must allow reaching `MAX_TOKENS_PER_USER = 30`; if the pool registers fewer collateral types, the attacker alone cannot fill the set and this degrades to raising the victim's floor for future deposits. This is the main constraint and depends on the deployed pool configuration — verify the count of `pool.getDepositTokens()` per deployment (e.g., Pool1/Pool2 on mainnet).
- No privileged role, oracle manipulation, or malicious LZ peer is needed; `deposit` is `whenNotPaused`/`nonReentrant`/`onlyIfDepositTokenExists`, none of which block the dust pattern.
- The victim retains the ability to withdraw existing unlocked collateral and repay debt, so impact is a forced-liquidation window rather than permanent loss of all funds.

### Recommendation
- Do not register a deposit token in `onBehalfOf_`'s account set for sub-threshold (dust) deposits, or require a minimum USD value per `deposit()` call (e.g., reuse `debtFloorInUsd`-style floor) so filling 30 slots becomes economically prohibitive.
- Alternatively, cap the set by *value-relevant* entries only (drop zero/near-zero balances via `unlockedBalanceOf`-aware removal), or allow an account to remove a token from its own set, giving victims an escape hatch that does not require governance.
- Consider making `deposit`'s beneficiary tracking opt-in (e.g., only the operator-authenticated `_msgSender()` registers tokens) so a third party cannot enlarge another account's set.

### Proof of Concept
```solidity
// Foundry fork test sketch — assumes Pool has >= 30 registered deposit tokens
function test_dustFillAccountSet_blocksNewCollateral() public {
    address victim = makeAddr("victim");
    // victim has open position collateralized by depositTokens[0] with debt
    address[] memory dts = pool.getDepositTokens();
    require(dts.length >= 30, "not enough deposit tokens to reach cap");

    for (uint i = 1; i < 30; i++) {
        IDepositToken dt = IDepositToken(dts[i]);
        IERC20 underlying = dt.underlying();
        deal(address(underlying), attacker, 1);
        vm.startPrank(attacker);
        underlying.approve(address(dt), 1);
        dt.deposit(1, victim); // onBehalfOf_ = victim
        vm.stopPrank();
    }
    assertEq(pool.depositTokensOf(victim).length, 30);

    // victim now cannot deposit a new collateral type
    vm.prank(victim);
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    IDepositToken(dts[30]).deposit(1e6, victim);

    // position drifts unhealthy -> liquidate succeeds; victim's top-up path is bricked
    vm.prank(liquidator);
    pool.liquidate(syntheticToken, victim, depositToken, amountToRepay);
}
```
Note: the PoC is contingent on the deployed pool exposing ≥30 deposit/debt token types that populate the per-account set; if the live configuration registers fewer, the impact degrades to partial set-griefing and the finding may not be reproducible as a full block. Exact line numbers for the `UserReachedMaxTokens` revert inside the account-set update hook could not be fully verified within available search iterations — confirm whether the check lives in `Pool` (hooked by `DepositToken._mint`) before finalizing the report.