### Title
Dust-filling of `depositTokensOfAccount` permanently blocks a leveraged victim from adding new collateral or new debt positions, forcing liquidation - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` caps the combined number of deposit tokens and debt tokens an account may hold at `MAX_TOKENS_PER_USER = 30` and enforces it in `onlyIfAdditionWillNotReachMaxTokens`, which guards `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount`. Because `DepositToken.transfer`/`transferFrom` are permissionless ERC20 entry points and every incoming transfer of a new token calls `pool.addToDepositTokensOfAccount(recipient_)`, an unprivileged attacker can dust-transfer 1 wei of every deposit token in the pool to a victim and permanently fill all 30 slots. While the victim carries debt, the dusted balances count as collateral and cannot be transferred back out (`_revertIfLocked` / `unlockedBalanceOf`), so the victim cannot free the slots. Any subsequent deposit of a *new* collateral type or issuance of a *new* synthetic reverts with `UserReachedMaxTokens`, and the victim cannot top up a deteriorating position - guaranteeing liquidation.

### Finding Description
- `DepositToken._transfer` unconditionally registers the token on the recipient when the recipient's prior balance is 0 (`contracts/DepositToken.sol:517-520`). The same happens on `_mint` (`contracts/DepositToken.sol:485-488`) and on `seize` (`contracts/DepositToken.sol:343-345`).
- `Pool.addToDepositTokensOfAccount` is protected by `onlyIfAdditionWillNotReachMaxTokens`, which reverts once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (`contracts/Pool.sol:79`, `contracts/Pool.sol:143-148`).
- `DepositToken.transfer` (`contracts/DepositToken.sol:348-354`) is a public, unprivileged entry point. The attacker only needs a 1-wei balance of each deposit token, obtainable by depositing dust amounts of each underlying (or receiving them).
- Escape is blocked for a leveraged victim: `unlockedBalanceOf` returns the transferable amount as the excess over what is needed to collateralize the debt (`contracts/DepositToken.sol:383-398`). Once the victim has debt, the dusted balances are locked collateral, so `transfer`/`withdraw` of them reverts via `_revertIfLocked` (`contracts/DepositToken.sol:350`, `contracts/DepositToken.sol:409`). The victim cannot remove entries because `removeFromDepositTokensOfAccount` only fires when the balance reaches zero (`contracts/DepositToken.sol:522-525`, `contracts/DepositToken.sol:459-462`).
- After the slots are filled, `deposit` of any new collateral reverts (mint → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`), and `issue` of any new synthetic reverts (mint → `addToDebtTokensOfAccount` → `UserReachedMaxTokens`). Repayments and withdrawals of existing tokens still work, so this is not gas/loop DoS - it is an accounting-slot exhaustion via forced state insertion.

### Impact Explanation
Temporary freezing of funds / forced liquidation. A victim whose position drifts toward the liquidation threshold and who needs to deposit a *different* collateral type (e.g., their existing collateral is falling) is unable to do so: every such deposit reverts. The position is then liquidatable at the `liquidatorIncentive` discount, producing a direct, quantifiable loss of the victim's collateral equal to the liquidation penalty. The slots also cannot be cleared while debt exists, so the freeze persists for the lifetime of the debt position. Attack cost is bounded by the dust value of up to 30 deposit token types plus gas; no privileged role is needed.

### Likelihood Explanation
Requires that the pool lists enough deposit/debt tokens to fill 30 slots minus the victim's current usage, and that the target is a leveraged account that would later need new collateral. Deployed Metronome pools list multiple collaterals and synthetics; the attacker can also combine dust deposit-token transfers with dust debt issuance is not possible (debt can't be transferred), so the practical bound is the number of listed deposit tokens. Where pools have fewer than 30 deposit tokens, the attack requires the victim to already hold several token types, lowering likelihood. The attack is front-runnable and cheap relative to the victim's liquidation penalty, so when the precondition (enough listed deposit tokens) holds, likelihood is medium-high.

### Recommendation
- Do not count slots toward `MAX_TOKENS_PER_USER` for tokens received below a minimum dust threshold, or only register a token in `depositTokensOfAccount` on user-initiated deposits rather than on every ERC20 transfer/`seize` receipt.
- Alternatively, allow an account to always remove a token entry whose balance is below a dust threshold regardless of lock status (the locked-balance computation ignores economically negligible balances anyway), or let users "opt out" of tracking via an explicit `removeFromDepositTokensOfAccount` call that sweeps the dust to treasury.
- At minimum, exclude balances below `debtFloorInUsd`-equivalent value from the locked-balance calculation so forced dust can always be shed.

### Proof of Concept
Hardhat/Foundry fork sketch against a live pool with ≥ `N` deposit tokens:

```solidity
// Assumptions: pool has deposit tokens dt[0..N-1], victim holds debt.
// 1. Attacker deposits dust of each underlying and receives 1 wei of each dt[i],
//    or simply transfers existing balances.
for (uint i; i < N; ++i) {
    dt[i].transfer(victim, 1);            // adds dt[i] to victim's depositTokensOfAccount
}
// victim's slot count == 30 now.

// 2. Victim tries to clear dust: reverts because balances are locked by debt
vm.prank(victim);
vm.expectRevert(NotEnoughFreeBalance.selector);
dt[0].transfer(attacker, 1);

// 3. Victim tries to deposit a collateral type not yet in their list
vm.prank(victim);
underlying.approve(address(dtNew), type(uint256).max);
vm.prank(victim);
vm.expectRevert(UserReachedMaxTokens.selector);
dtNew.deposit(1e18, victim);            // _mint -> addToDepositTokensOfAccount -> revert

// 4. Price moves against victim; liquidator calls pool.liquidate(victim, ...)
//    and seizes collateral at discount. Victim could not add new collateral to deleverage.
```

Key assertions: `pool.getDepositTokensOfAccount(victim).length == MAX_TOKENS_PER_USER` after step 1; victim `dtNew.deposit` reverts; victim dust `transfer` reverts under `_revertIfLocked` while `debtPositionOf(victim).debtInUsd > 0`.

Caveat: I could not fully verify the exact revert name thrown by `_revertIfLocked` or the total number of deposit tokens listed on each deployed pool; if a target pool lists fewer deposit tokens than the cap, the attacker must combine this with the victim's pre-existing token count, which narrows the set of vulnerable accounts.