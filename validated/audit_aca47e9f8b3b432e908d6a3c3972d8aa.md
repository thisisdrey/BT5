### Title
Attacker can permanently occupy a victim's `depositTokensOfAccount`/`debtTokensOfAccount` slots with dust deposits/transfers, hitting `MAX_TOKENS_PER_USER` and DoSing deposits, transfers-in, issuance and collateral top-ups - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Analogous to the Nokogiri/nekohtml "uncontrolled resource consumption via crafted input" DoS, `Pool` tracks per-account deposit and debt tokens in a bounded list (`MAX_TOKENS_PER_USER = 30`). An unprivileged attacker can fill a victim's list with worthless dust positions via `DepositToken.deposit(amount_, onBehalfOf_ = victim)` or `DepositToken.transfer(victim, dust)`. Once the list is full, every code path that adds a new token to the account reverts with `UserReachedMaxTokens`, and if the victim has outstanding debt the dust balances are locked (`_revertIfLocked`), so the victim cannot evict them.

### Finding Description
`Pool` maintains `depositTokensOfAccount` and `debtTokensOfAccount` as `MappedEnumerableSet` lists capped at `MAX_TOKENS_PER_USER = 30`, enforced by the `onlyIfAdditionWillNotReachMaxTokens` modifier on `addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` [1](#0-0) . These functions are invoked by `DepositToken._mint` (during `deposit`) and `DepositToken._transfer` (during `transfer`/`transferFrom`/`seize`) whenever the recipient's prior balance is zero [2](#0-1) . There is no minimum-amount check beyond `amount_ > 0` and no opt-in: `deposit(amount_, onBehalfOf_)` mints to an arbitrary `onBehalfOf_`, and `transfer` credits an arbitrary recipient.

Attack path (all public, unprivileged):
1. For each registered `DepositToken` the victim does not hold, attacker calls `deposit(1, victim)` (or acquires dust and calls `transfer(victim, 1)`).
2. Each call pushes the token into `depositTokensOfAccount[victim]` until the combined list reaches 30.
3. Afterwards, any victim action that adds a new token — depositing a new collateral type, receiving a deposit-token transfer, receiving seized collateral via `Pool.liquidate`, or issuing a new synthetic debt token (which calls `addToDebtTokensOfAccount`) — reverts.

Crucially, removal requires the dust balance to be unlocked: `transfer` and `withdraw` both call `_revertIfLocked`, which consults `unlockedBalanceOf` → `Pool.debtPositionOf` [3](#0-2) . If the victim has debt, the dust is treated as collateral and locked, so the victim cannot clear the slots without first repaying debt — which itself may be impossible for a new debt token slot.

### Impact Explanation
The liveness/availability invariant breaks: a victim holding a debt position can be permanently prevented from (a) depositing additional collateral types to restore health (forced liquidation risk), (b) receiving any new deposit token (including liquidation proceeds via `seize` → `_transfer` → `addToDepositTokensOfAccount`), and (c) minting new synthetic assets. This is a griefing DoS that can temporarily freeze the victim's ability to manage their position and can be combined with liquidation to force losses — matching the "temporary freezing of funds" acceptance criterion.

### Likelihood Explanation
Feasible whenever a pool lists enough deposit tokens (combined debt+deposit slots of 30) or when the attacker can also occupy debt slots via positions they control and transfer. Cost is ~30 dust transfers/deposits of 1 wei each plus fees — trivially cheap relative to the damage of forcing a leveraged position into liquidation. No privileged role, oracle manipulation, or malicious infrastructure is needed. The main limitation is that impact scales with the number of registered collateral types; on pools with few deposit tokens, the attacker cannot reach the cap alone unless the victim already occupies many slots.

### Recommendation
- Require a minimum meaningful amount (e.g., a USD floor via the oracle) before a token is added to `depositTokensOfAccount`, or
- Make list membership opt-in: only add tokens on `deposit` initiated/approved by the account, not on inbound `transfer`, or
- Allow permissionless removal of zero-value/dust entries, or raise/decouple the cap so dust slots cannot block legitimate collateral additions.

### Proof of Concept
Hardhat sketch:

```ts
// pool, depositTokens[] (>= 30 registered collaterals), victim has an open debt position
const attacker = signers[1];
const victim = signers[2].address;

// 1. Fill victim's deposit token list with dust
for (const dt of depositTokens) {
  const underlying = await ethers.getContractAt('ERC20', await dt.underlying());
  await underlying.connect(attacker).approve(dt.address, 1);
  await dt.connect(attacker).deposit(1, victim); // adds dt to depositTokensOfAccount[victim]
  if ((await pool.getDepositTokensOfAccount(victim)).length +
      (await pool.getDebtTokensOfAccount(victim)).length >= 30) break;
}

// 2. Any new collateral deposit for victim now reverts
const newDt = depositTokens[depositTokens.length - 1];
await expect(newDt.connect(attacker).deposit(1, victim))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 3. Victim cannot evict dust while debt locks balances
await expect(depositTokens[0].connect(victimSigner).transfer(attacker.address, 1))
  .to.be.revertedWithCustomError(depositTokens[0], 'NotEnoughFreeBalance');
```

Confirm on a fork against the deployed pool's registered `depositTokens` count; the attack requires `len(depositTokens) + len(debtTokens)` reachable slots ≥ 30 combined with the victim's existing entries.

### Citations

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/DepositToken.sol (L180-182)
```text
    function _revertIfLocked(address account_, uint256 amount_) private view {
        if (unlockedBalanceOf(account_) < amount_) revert NotEnoughFreeBalance();
    }
```

**File:** contracts/DepositToken.sol (L485-525)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
    }

    /// @inheritdoc TokenHolder
    // solhint-disable-next-line no-empty-blocks
    function _requireCanSweep() internal view override onlyGovernor {}

    /**
     * @notice Move `amount` of tokens from `sender` to `recipient`
     */
    function _transfer(
        address sender_,
        address recipient_,
        uint256 amount_
    ) private updateRewardsBeforeTransfer(sender_, recipient_) {
        if (sender_ == address(0)) revert TransferFromTheZeroAddress();
        if (recipient_ == address(0)) revert TransferToTheZeroAddress();

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
