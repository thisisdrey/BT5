### Title
Deposit-token dust transfer griefing permanently blocks new deposits and debt issuance via `MAX_TOKENS_PER_USER` - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Analogous to CVE-2021-47395 — where an unbounded attacker-injected value (vht mcs/nss) passed unchecked into kernel state caused a repeatable WARN/DoS — Metronome lets any unprivileged user force entries into a victim's bounded `depositTokensOfAccount` set by transferring dust `DepositToken` amounts. Once the combined `debt + deposit` token count reaches `MAX_TOKENS_PER_USER = 30`, every subsequent deposit of a new collateral type and every issuance of a new debt token for that account reverts with `UserReachedMaxTokens`, freezing the victim's ability to manage their position.

### Finding Description
`DepositToken.transfer`/`transferFrom`/`seize` all funnel into `_transfer`, which unconditionally registers the recipient in the Pool's per-account set when the balance goes from 0 to >0:

```solidity
// contracts/DepositToken.sol:517-520
// Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
if (_recipientBalanceBefore == 0 && amount_ > 0) {
    pool.addToDepositTokensOfAccount(recipient_);
}
```

There is no minimum amount, no opt-in, and no consent check — the kernel analog of trusting an injected radiotap field. On the Pool side, the only guard is a hard cap:

```solidity
// contracts/Pool.sol:143-148, 216-220
modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
    if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
        revert UserReachedMaxTokens();
    }
    _;
}
function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) { ... }
```

The same cap gates `addToDebtTokensOfAccount` (`Pool.sol:204`), which is invoked from `DebtToken._mint` (`DebtToken.sol:598-600`) on every first-time debt issuance, and `_mint` on `DepositToken` (`DepositToken.sol:486-488`) on every first-time deposit. There is no way for the victim to reject the entry; `removeFromDepositTokensOfAccount` fires only when the balance returns to exactly 0 (`DepositToken.sol:459-462, 523-525`).

### Impact Explanation
An attacker deposits a minimal amount of each of the pool's registered deposit tokens into their own account (cost: `N` wei-scale dust of each underlying), then calls `depositToken.transfer(victim, 1)` for each. After at most 30 transfers the victim's combined set is full. From that point:

- `deposit()` for any collateral the victim does not already hold reverts → the victim cannot add collateral to improve an unhealthy position and will be liquidated.
- `DebtToken.issue()`/`SmartFarmingManager.leverage` for any synth the victim does not already owe reverts → no new debt positions.
- A liquidator calling `Pool.liquidate` with `to_` receiving a deposit token the liquidator doesn't hold can also be forced to revert (liquidators can workaround with fresh addresses, but the victim-side deposit DoS has no workaround while debt exists).

Because dust balance becomes part of locked collateral once the victim has debt (`unlockedBalanceOf` caps the transferable amount at what keeps the position healthy, `DepositToken.sol:383-398`), the victim cannot necessarily shed all entries to recover — yielding temporary-to-indefinite freezing of the victim's funds. Invariant broken: liveness/availability of the deposit and issue paths, caused by an unbounded attacker-writable set.

### Likelihood Explanation
Fully unprivileged: `DepositToken.transfer` is a public ERC20 entry point (`DepositToken.sol:348-354`) with only `_revertIfLocked` on the *sender*, and pools on deployed chains list multiple deposit/debt tokens so the 30-slot cap is reachable. Attack cost is dust principal plus ~30 cheap transactions; no oracle manipulation, no privileged role, no flash loan needed. The only mitigations are `whenNotShutdown`/`nonReentrant`, neither of which apply to `transfer` (it is not even `nonReentrant`), and the victim has no built-in recovery function.

### Recommendation
- Do not register the recipient on `transfer`/`transferFrom` (only on `deposit`/`seize`), or require a minimum transfer amount for set registration.
- Alternatively allow the account itself to call a `removeDepositToken`/`removeDebtToken` exit that burns/forfeits a dust balance, so a victim can always clear slots.
- Bound this like the kernel fix: treat externally influenced set growth as attacker input and enforce the limit on the writer's side, not the victim's.

### Proof of Concept
Hardhat fork sketch (against a deployed Pool with ≥1 registered `DepositToken`/`DebtToken` set; repeat across all listed `pool.getDepositTokens()`):

```ts
// attacker deposits 1 wei of each collateral into itself
for (const dt of depositTokens) {
  await underlying.connect(attacker).approve(dt.address, 1)
  await dt.connect(attacker).deposit(1, attacker.address)
  await dt.connect(attacker).transfer(victim.address, 1) // fills victim's set
}
// victim's depositTokensOfAccount.length + debtTokensOfAccount.length == 30

// now any first-time deposit or issue by victim reverts:
await expect(newDepositToken.connect(victim).deposit(amount, victim.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens')
await expect(debtToken.connect(victim).issue(amount, victim.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens')
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** contracts/DepositToken.sol (L383-398)
```text
    function unlockedBalanceOf(address account_) public view override returns (uint256 _unlockedBalance) {
        IPool _pool = pool;

        (, , uint256 _debtInUsd, , uint256 _issuableInUsd) = _pool.debtPositionOf(account_);

        if (_debtInUsd == 0) {
            return balanceOf[account_];
        }

        if (_issuableInUsd > 0) {
            _unlockedBalance = Math.min(
                balanceOf[account_],
                _pool.masterOracle().quoteUsdToToken(address(underlying), _issuableInUsd.wadDiv(collateralFactor))
            );
        }
    }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
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

**File:** contracts/DebtToken.sol (L597-600)
```text
        //  Add this token to the debt tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDebtTokensOfAccount(account_);
        }
```
