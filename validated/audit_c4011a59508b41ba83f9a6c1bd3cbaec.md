The CVE class is "failure to initialize an object" — in this codebase the analog is `accountIndexOf` never being initialized for pre-existing token holders in `RewardsDistributor`. Let me confirm the token-hook wiring.### Title
Uninitialized `accountIndexOf` credits existing depositors rewards equal to their full token balance - ([File: contracts/RewardsDistributor.sol])

### Summary
`RewardsDistributor._calculateTokenDelta` treats `accountIndexOf[token][account] == 0` as "never initialized" and falls back to `INITIAL_INDEX` (1e18) — but only when the token index is **strictly greater** than `INITIAL_INDEX`. When the token index equals `INITIAL_INDEX` (i.e., the same block the governor registers a reward token via `updateTokenSpeed`/`updateTokenSpeeds`, before any accrual period elapses), the fallback is skipped and the raw delta `INITIAL_INDEX - 0` is applied against the account's existing balance, crediting rewards equal to the account's entire deposit/debt token balance.

This mirrors CVE-2016-6836: an object (`accountIndexOf` per account, analogous to QEMU's `txcq_descr`) is used before initialization, leaking value that was never earned.

### Finding Description
In `contracts/RewardsDistributor.sol`: [1](#0-0) 

```solidity
if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

When `_tokenIndex == INITIAL_INDEX` and `_accountIndex == 0`, `_deltaIndex = 1e18` and `_tokensDelta = balanceOf(account) * 1` — the full balance is credited as claimable rewards.

How the state arises:
- `_updateTokenSpeed` initializes `tokenStates[token_] = {index: INITIAL_INDEX, timestamp: block.timestamp}` when a token's speed is first set to non-zero [2](#0-1) .
- `_calculateTokenIndex` returns no update while `_deltaTimestamps == 0`, so within the same block the stored index remains exactly `INITIAL_INDEX` [3](#0-2) .
- Accounts that deposited/borrowed **before** the reward token was registered never had `accountIndexOf` written (the `tokenStates[token_].index > 0` guard in `updateBeforeMintOrBurn`/`updateBeforeTransfer` skipped them) [4](#0-3) .
- `claimRewards(address[] accounts_, IERC20[] tokens_)` is permissionless `nonReentrant` — anyone can trigger `_updateTokensAccruedOf` for any account, and `_transferRewardIfEnoughTokens` pays out up to the contract's whole reward-token balance [5](#0-4) .

Attack trace (fully unprivileged except the governance tx it backruns):
1. Attacker holds `msdToken` deposits made before the governor enables rewards (or any existing depositor can be used; attacker triggers for themselves).
2. Governor broadcasts `updateTokenSpeeds([msdToken],[speed])`. Attacker observes it in the mempool and backruns it **in the same block**.
3. Attacker calls `claimRewards([attacker],[msdToken])`. `tokenStates[msdToken].index == INITIAL_INDEX` passes the `> 0` guard; `_updateTokenIndex` is a no-op (same timestamp); `_calculateTokenDelta` computes `_deltaIndex = INITIAL_INDEX` and `tokensAccruedOf[attacker] += balanceOf(attacker)`.
4. `_transferRewardIfEnoughTokens` transfers reward tokens equal to the attacker's deposit balance (bounded only by the distributor's balance).

### Impact Explanation
Theft of unclaimed yield / direct draining of the reward contract. The credited amount is proportional to the attacker's deposit balance and is paid out up to the entire `rewardToken` balance held by the distributor. A whale depositor (or an attacker who accumulated deposits cheaply beforehand) can capture rewards accrued for all users in one call. The invariant broken is reward accrual: claims must equal `balance * (currentIndex - accountIndex)` with a correctly initialized `accountIndex`.

### Likelihood Explanation
Medium. Requirements:
- Governor adds a reward speed for a token that already has holders — a routine operational action (e.g., enabling new incentives on an existing `DepositToken`/`DebtToken`), so the precondition is realistic.
- The claim must land in the same block (or before any subsequent block with `speed > 0`, since once `index > INITIAL_INDEX` the fallback correctly seeds `accountIndexOf`). On chains with public mempools, a same-block backrun of a governance multisig tx is feasible; on chains where rewards are enabled via keeper (`syncTokenSpeed` is callable by `tokenSpeedKeeper`, also a public observable tx) the same window exists.
- No modifiers, pause flags, or reentrancy guards block the call; `claimRewards` is explicitly permissionless.

Limitation: if the token has zero holders at registration time, there is nothing to steal — the bug requires pre-existing balances.

### Recommendation
Seed `accountIndexOf` correctly instead of relying on the zero-check fallback. Options:
- In `_calculateTokenDelta`, treat `_accountIndex == 0` as `_tokenIndex` (no accrued delta) rather than `INITIAL_INDEX` when `_tokenIndex <= INITIAL_INDEX` — i.e., change the fallback to `if (_accountIndex == 0) _accountIndex = _tokenIndex > INITIAL_INDEX ? INITIAL_INDEX : _tokenIndex;` or simply `_accountIndex = _tokenIndex;` for first-time accounts so no historical delta is ever attributed.
- Alternatively, when `_updateTokenSpeed` registers a token, iterate/snapshot or require a checkpoint so existing holders' indexes are initialized at registration.

### Proof of Concept
Hardhat (repo's existing test stack). Outline:

```ts
// test/RewardsDistributor.uninitializedIndex.test.ts
it('credits full balance to pre-existing depositor on same block as updateTokenSpeed', async () => {
  // fixture: pool, depositToken (msdMET), rewardsDistributor, rewardToken (MET)
  // 1. alice deposits collateral -> msdMET minted; accountIndexOf[msdMET][alice] stays 0
  //    because tokenStates[msdMET].index == 0 (reward token not yet registered)
  await depositToken.connect(alice).approve(pool.address, amount)
  await pool.connect(alice).deposit(depositToken.address, amount)

  // 2. fund distributor with reward tokens
  await rewardToken.mint(rewardsDistributor.address, parseEther('10000'))

  // 3. enable speed and claim in the SAME block (automine off / batch txs)
  await ethers.provider.send('evm_setAutomine', [false])
  await rewardsDistributor.connect(governor).updateTokenSpeed(msdMET.address, parseEther('1'))
  await rewardsDistributor.connect(attacker).claimRewards([aliceOrAttacker], [msdMET.address])
  await ethers.provider.send('evm_mine')

  // 4. accrued == full balance (deltaIndex == 1e18), reward paid up to distributor balance
  const accrued = await rewardsDistributor.tokensAccruedOf(victim)
  expect(accrued).to.eq(await msdMET.balanceOf(victim)) // ~ balance * INITIAL_INDEX wadMul
})
```

To reproduce the same-block condition without disabling automine, send `updateTokenSpeeds` and `claimRewards` in one `Operator.execute`-style multicall or via a helper contract that calls both in sequence — `claimRewards` is permissionless, so the attacker contract can call it immediately after observing the governance transaction. The victim can be the attacker themselves (an existing depositor) or any pre-existing holder, since `claimRewards(accounts_, tokens_)` accepts arbitrary accounts.

Uncertainty noted: the exact call order between token mint and the `updateBeforeMintOrBurn` hook in `DepositToken.sol`/`DebtToken.sol` determines whether a *new* depositor in the same block also gets credited on pre-mint balance — but pre-existing holders (the realistic case) are affected regardless.

### Citations

**File:** contracts/RewardsDistributor.sol (L150-167)
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

**File:** contracts/RewardsDistributor.sol (L203-211)
```text
        if (_deltaTimestamps > 0 && _speed > 0) {
            uint256 _totalSupply = token_.totalSupply();
            uint256 _tokensAccrued = _deltaTimestamps * _speed;
            uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
            _newIndex = (_supplyState.index + _ratio).toUint224();
            _newTimestamp = block.timestamp.toUint32();
        } else if (_deltaTimestamps > 0 && _supplyState.index > 0) {
            _newTimestamp = block.timestamp.toUint32();
        }
```

**File:** contracts/RewardsDistributor.sol (L225-230)
```text
        if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
            _accountIndex = INITIAL_INDEX;
        }

        uint256 _deltaIndex = _tokenIndex - _accountIndex;
        _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

**File:** contracts/RewardsDistributor.sol (L296-299)
```text
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
```
