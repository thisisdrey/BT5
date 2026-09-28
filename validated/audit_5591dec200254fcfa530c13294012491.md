### Title
Attacker can fill a victim's per-account token lists with dust, reverting all new collateral deposits and debt issuance (`Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount`) - (File: `contracts/Pool.sol`)

### Summary
The GoPistolet bug class (CWE-404, improper resource shutdown/release → resource exhaustion DoS) maps onto Metronome's per-account token bookkeeping: `depositTokensOfAccount` and `debtTokensOfAccount` in `PoolStorageV1` are append-only sets that are only "released" when a balance returns to zero, and every addition is capped by `MAX_TOKENS_PER_USER` via `onlyIfAdditionWillNotReachMaxTokens` [1](#0-0) [2](#0-1) . An unprivileged attacker can force "leaks" of slots in a victim's list by dust-transferring deposit tokens to them, since `DepositToken._transfer` adds the token to the recipient's list while only the sender's balance is lock-checked [3](#0-2) .

### Finding Description
- `Pool.addToDepositTokensOfAccount(account_)` and `addToDebtTokensOfAccount(account_)` are guarded by `onlyIfAdditionWillNotReachMaxTokens(account_)`, which reverts `UserReachedMaxTokens` when the combined deposit+debt list reaches `MAX_TOKENS_PER_USER` [2](#0-1) .
- `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance goes from 0 to >0; nothing requires the recipient's consent [4](#0-3) .
- Similarly, `DebtToken` issuance to an arbitrary `onBehalfOf_`/recipient registers a debt token in the victim's list (the test suite issues to `liquidator.address`, confirming third-party recipients) [5](#0-4) .
- The removal path exists (`removeFromDepositTokensOfAccount` on burn/zero balance) but only triggers when the balance fully drains [6](#0-5)  — so attacker-placed dust occupies slots until the victim manually clears them.

Attack sequence:
1. Attacker deposits minimal collateral into every `DepositToken` registered in the pool and dust-transfers each to the victim.
2. Optionally, attacker issues dust amounts of each `DebtToken` synth to the victim.
3. Once `depositTokensOfAccount + debtTokensOfAccount` hits `MAX_TOKENS_PER_USER`, every subsequent `addTo*` call reverts: the victim cannot deposit a new collateral type, receive a new msd-token, or receive new debt — and any transfer *to* them of a new token type reverts, DoSing senders too.
4. If the victim's position becomes unhealthy during this window, they cannot add a fresh collateral type to restore health, enabling liquidation.

### Impact Explanation
Denial of the deposit pathway for any collateral type not already in the victim's list, denial of new debt issuance, and forced reverts on inbound token transfers. This is a liveness/resource-exhaustion failure of the account bookkeeping, directly analogous to unreleased-resource DoS. Whether this rises to exploitable theft depends on timing: clearing slots requires the victim to transfer/burn the dust (which may itself be lock-constrained if their position is unhealthy, since `_revertIfLocked`/`unlockedBalanceOf` restrict outgoing transfers [7](#0-6) ). A victim with an underwater-adjacent position may be unable to free slots, making the freeze effectively permanent for them and enabling forced liquidation — which qualifies as indirect loss of user funds.

### Likelihood Explanation
Constrained by the number of distinct `DepositToken`/`DebtToken` contracts a pool actually registers — these are governor-added, so if `MAX_TOKENS_PER_USER` exceeds the real token count, the cap is unreachable and the attack fails entirely. The exact `MAX_TOKENS_PER_USER` value and per-pool token counts on deployed chains were not verified in this pass; feasibility requires `poolDepositTokens + poolDebtTokens ≥ MAX_TOKENS_PER_USER`. Cost to the attacker is bounded (dust deposits + gas per token, plus collateral to back issued debt), and no privileged role is needed.

### Recommendation
- Exclude dust-size balance changes from list registration, or make `addTo*TokensOfAccount` skip rather than revert at the cap and let health/deposit functions tolerate unlisted tokens.
- Alternatively, allow accounts to self-remove entries (`removeFromDepositTokensOfAccount` callable by the account holder when balance is dust/zero) so victims can always free slots even when locked.

### Proof of Concept
A Foundry/Hardhat fork PoC (not fully executed here; feasibility gated on token count vs `MAX_TOKENS_PER_USER`):

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";

contract TokenListGriefingTest is Test {
    address constant POOL = /* deployed Pool proxy */;
    address victim = address(0xBEEF);

    function test_fillVictimTokenList() public {
        IPool pool = IPool(POOL);
        uint256 max = pool.MAX_TOKENS_PER_USER();
        address[] memory depositTokens = pool.getDepositTokens();

        // Attacker deposits dust and transfers to victim for each deposit token
        for (uint256 i; i < depositTokens.length && pool.getDepositTokensOfAccount(victim).length < max; ++i) {
            IDepositToken dt = IDepositToken(depositTokens[i]);
            IERC20 underlying = dt.underlying();
            deal(address(underlying), address(this), 1);
            underlying.approve(address(dt), 1);
            dt.deposit(1, address(this));
            dt.transfer(victim, 1); // adds depositTokens[i] to victim's list
        }

        // Victim can no longer receive/deposit any new collateral type
        vm.prank(victim);
        vm.expectRevert(); // UserReachedMaxTokens propagated through deposit/transfer
        IDepositToken(depositTokens[0]).transfer(victim, 1);
    }
}
```

Caveat: if the pool's registered deposit+debt token count is below `MAX_TOKENS_PER_USER`, the cap cannot be reached and this degrades to a non-issue on that deployment. I did not confirm the constant's value or the deployed token counts, so the report should be validated against mainnet/Base state before submission.

### Citations

**File:** contracts/storage/PoolStorage.sol (L76-83)
```text
     * @notice Per-account deposit tokens (i.e. tokens that user has balance > 0)
     */
    MappedEnumerableSet.AddressSet internal depositTokensOfAccount;

    /**
     * @notice Per-account debt tokens (i.e. tokens that user has balance > 0)
     */
    MappedEnumerableSet.AddressSet internal debtTokensOfAccount;
```

**File:** contracts/Pool.sol (L204-220)
```text
    function addToDebtTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _debtToken = _msgSender();
        _revertIfSenderIsNotDebtToken(_debtToken);
        if (!debtTokensOfAccount.add(account_, _debtToken)) revert DebtTokenAlreadyExists();
    }

    /**
     * @notice Add a deposit token to the per-account list
     * @dev This function is called from `DepositToken` when user's balance changes from `0`
     * @dev The caller should ensure to not pass `address(0)` as `_account`
     * @param account_ The account address
     */
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```

**File:** contracts/DepositToken.sol (L459-462)
```text
        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (_amount > 0 && _balanceAfter == 0) {
            pool.removeFromDepositTokensOfAccount(_account);
        }
```

**File:** contracts/DepositToken.sol (L506-525)
```text
        uint256 _senderBalanceBefore = balanceOf[sender_];
        if (_senderBalanceBefore < amount_) revert TransferAmountExceedsBalance();
        uint256 _recipientBalanceBefore = balanceOf[recipient_];

        unchecked {
            balanceOf[sender_] = _senderBalanceBefore - amount_;
            balanceOf[recipient_] += amount_;
        }

        emit Transfer(sender_, recipient_, amount_);

        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```

**File:** test/Pool.test.ts (L987-989)
```typescript
            const amountToRepay = await msDogeDebtToken.balanceOf(alice.address)
            await msDogeDebtToken.connect(liquidator).issue(amountToRepay, liquidator.address)
            await pool.connect(liquidator).liquidate(msDoge.address, alice.address, amountToRepay, msdMET.address)
```
