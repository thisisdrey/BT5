### Title
Retroactive reward accrual for first-time users via `INITIAL_INDEX` fallback allows theft of other users' unclaimed yield - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor._calculateTokenDelta` treats a first-time account's stored index of `0` as `INITIAL_INDEX` (`1e18`) whenever the global index has already grown past it. Because rewards are computed as `balance * (globalIndex - accountIndex)` using the account's *current* token balance, an attacker who deposits for the first time after the index has accrued receives rewards for the entire historical period — a stale/uninitialized-state reuse analogous to a use-after-free: the "free" (zero) slot is interpreted as an old valid index.

### Finding Description
- `claimRewards(address[] accounts_, IERC20[] tokens_)` is public and permissionless; anyone can trigger accrual for any account. [1](#0-0) 
- `_updateTokensAccruedOf` calls `_calculateTokenDelta`, which does:
  - `_accountIndex = accountIndexOf[token_][account_]` (0 for a new account)
  - `if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) _accountIndex = INITIAL_INDEX;`
  - `_tokensDelta = token_.balanceOf(account_).wadMul(_tokenIndex - _accountIndex)` [2](#0-1) 
- `updateBeforeMintOrBurn`/`updateBeforeTransfer` (called by `DepositToken`/`DebtToken` on mint/transfer hooks) use the same fallback, so even the hook does not fix it — it *records* the retroactive delta into `tokensAccruedOf` at the moment of the attacker's first deposit. [3](#0-2) 
- Once `tokenStates[token_].index > 1e18` (i.e., any rewards have ever streamed), a brand-new account instantly accrues `balance * (index - 1e18)` of rewards that were emitted while the account held nothing.

Concrete attack (single account, no privileged role):
1. Wait/observe a pool where `tokenStates[depositToken].index > INITIAL_INDEX` (true on any live deployment that has streamed rewards).
2. Deposit collateral via `Pool`/`NativeTokenGateway`/`VesperGateway` to obtain `DepositToken` balance (or mint debt so `DebtToken` balance is nonzero). The mint hook writes `tokensAccruedOf[attacker] = balance * (index - 1e18)`.
3. Call `RewardsDistributor.claimRewards(attacker)` (or have anyone call it) → `_transferRewardIfEnoughTokens` pays out as long as the distributor holds enough `rewardToken`. [4](#0-3) 
4. Withdraw collateral (subject to lock/health checks) — rewards already stolen.

The invariant broken: reward accrual must be proportional to balance × time held; here a zero-initialized slot is treated as an old index, decoupling accrual from holding duration.

### Impact Explanation
Direct theft of unclaimed yield. The attacker siphons `rewardToken` that the index accumulated for all past holders; honest users' later claims will find `amount_ > _balance` in `_transferRewardIfEnoughTokens` and receive nothing (accrued stays but tokens are gone). Magnitude scales with deposit size and elapsed emission — a flash-loan-sized deposit against a long-lived index captures nearly the whole distributor balance.

### Likelihood Explanation
- Entry points are all public: deposit flows, `claimRewards`, `updateBeforeMintOrBurn`.
- Precondition (`index > INITIAL_INDEX`) is the normal state of any active reward stream; `syncTokenSpeed`/`updateTokenSpeed` keep it running.
- Reentrancy guards, pause flags, `SynthContext`, and `_revertIfLocked` do not intervene; the claim leg has no health check. Likelihood is limited mainly by needing deposit capital (or debt position) and by the distributor actually holding reward tokens, and governor could in principle stop emissions after the fact — but the theft itself is atomic.

### Recommendation
Initialize `accountIndexOf` to the *current* global index on first interaction instead of `INITIAL_INDEX`: in `_calculateTokenDelta`, when `_accountIndex == 0`, use `_tokenIndex` (yielding `delta = 0`), and only apply the `INITIAL_INDEX` fallback for accounts whose index was genuinely recorded as `INITIAL_INDEX` before accrual began — e.g., distinguish "never seen" (0) from "seen at genesis" by tracking a flag, or ensure `updateBeforeMintOrBurn` always stamps first-touch accounts at the live index.

### Proof of Concept
Hardhat/Foundry fork sketch:
```solidity
// fork a chain where RewardsDistributor is live with tokenSpeeds[depositToken] > 0
// and tokenStates[depositToken].index > 1e18
uint256 bal = distributor.rewardToken().balanceOf(address(distributor));

// attacker (fresh EOA, never held depositToken)
vm.prank(attacker);
pool.deposit(depositToken, amount);           // mint hook -> tokensAccruedOf = amount * (index-1e18)

uint256 accrued = distributor.tokensAccruedOf(attacker);
assertGt(accrued, 0);                          // retroactive rewards recorded

vm.prank(attacker); // or anyone
distributor.claimRewards(attacker);
assertEq(rewardToken.balanceOf(attacker), min(accrued, bal)); // stolen yield
```
The same works via a `DebtToken` position since `IDebtToken.balanceOf` is used identically.

### Citations

**File:** contracts/RewardsDistributor.sol (L150-168)
```text
    function claimRewards(address[] memory accounts_, IERC20[] memory tokens_) public override nonReentrant {
        uint256 _accountsLength = accounts_.length;
        uint256 _tokensLength = tokens_.length;
        for (uint256 i; i < _tokensLength; ++i) {
            IERC20 _token = tokens_[i];

            if (tokenStates[_token].index > 0) {
                _updateTokenIndex(_token);
                for (uint256 j; j < _accountsLength; j++) {
                    _updateTokensAccruedOf(_token, accounts_[j]);
                }
            }
        }

        for (uint256 j; j < _accountsLength; j++) {
            address _account = accounts_[j];
            _transferRewardIfEnoughTokens(_account, tokensAccruedOf[_account]);
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L175-192)
```text
    function updateBeforeMintOrBurn(IERC20 token_, address account_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, account_);
        }
    }

    /**
     * @notice Update indexes on pre-transfer
     * @dev Called by DepositToken and DebtToken contracts
     */
    function updateBeforeTransfer(IERC20 token_, address from_, address to_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, from_);
            _updateTokensAccruedOf(token_, to_);
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L217-231)
```text
    function _calculateTokenDelta(
        TokenState memory _tokenState,
        IERC20 token_,
        address account_
    ) private view returns (uint256 _tokenIndex, uint256 _tokensDelta) {
        _tokenIndex = _tokenState.index;
        uint256 _accountIndex = accountIndexOf[token_][account_];

        if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
            _accountIndex = INITIAL_INDEX;
        }

        uint256 _deltaIndex = _tokenIndex - _accountIndex;
        _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
    }
```

**File:** contracts/RewardsDistributor.sol (L248-256)
```text
    function _transferRewardIfEnoughTokens(address account_, uint256 amount_) private {
        IERC20 _rewardToken = rewardToken;
        uint256 _balance = _rewardToken.balanceOf(address(this));
        if (amount_ > 0 && amount_ <= _balance) {
            tokensAccruedOf[account_] = 0;
            _rewardToken.safeTransfer(account_, amount_);
            emit RewardClaimed(account_, amount_);
        }
    }
```
