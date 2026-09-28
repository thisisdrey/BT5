### Title
Dust-transfer griefing fills victim's bounded per-account token list and blocks new collateral deposits and debt issuance - ([File: contracts/Pool.sol](contracts/Pool.sol), [contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
The upstream bug is a fixed-capacity buffer (`dev_map_read` writing attacker-controlled entries into arrays that are too small). The Metronome analog is the fixed-capacity per-account token registry: `MAX_TOKENS_PER_USER = 30` bounds the combined size of `depositTokensOfAccount` + `debtTokensOfAccount`, and any `DepositToken.transfer` to a first-time holder forcibly writes a new entry into the victim's array via `Pool.addToDepositTokensOfAccount`. An attacker can overflow this bounded list with dust transfers, after which all "add new token" paths for the victim revert with `UserReachedMaxTokens`.

### Finding Description
`Pool.addToDepositTokensOfAccount` enforces `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER` → revert [1](#0-0) . The registry grows automatically: `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance is zero and `amount_ > 0` [2](#0-1) . `transfer`/`transferFrom` are public and require no recipient consent [3](#0-2) .

Attack path (unprivileged EOA):
1. Attacker acquires dust balances of many distinct `DepositToken`s of the target pool (deposit 1 wei of each underlying, or buy dust).
2. For each token, attacker calls `depositToken.transfer(victim, 1)`. Each call appends the token to `depositTokensOfAccount[victim]`.
3. Once the victim's combined list reaches 30 entries, every operation that would add a new token reverts:
   - `DepositToken.deposit(amount, victim)` for any collateral type the victim doesn't already hold — `_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens` [4](#0-3) .
   - `Pool.issue`/`DebtToken` mint paths for any debt token not already in the list — `addToDebtTokensOfAccount` shares the same combined counter [5](#0-4) .
   - Incoming `transfer`/`seize` of a new deposit token type reverts, so even liquidations paying out a new collateral type to the victim revert.

### Impact Explanation
Temporary freezing of funds / position functionality with a path to loss: the victim cannot onboard a new collateral type. If their existing deposit-token set cannot be topped up (e.g., they lack more of those underlyings, or only hold debt tokens), they cannot cure a deteriorating position before liquidation — the griefing directly enables forced liquidation of a position the victim could otherwise have saved. At minimum it is a reversible DoS: the victim must spend gas transferring each dust balance out in full (a zero balance triggers `removeFromDepositTokensOfAccount` [6](#0-5) ) to recover functionality.

### Likelihood Explanation
- No privileged role needed; attacker uses only public `transfer`.
- Feasibility depends on the deployed pool having enough distinct deposit/debt tokens to approach the 30-slot cap; this must be confirmed per chain (not verifiable from the index).
- Attacker cost is ~30 dust deposits + 30 transfers, and the dust is recoverable afterward.
- Uncertainty I could not fully verify in the available iterations: whether `DebtToken` permits transfers or `issue` to arbitrary `onBehalfOf_` (grep confirmed matching lines in `DebtToken.sol` but contents weren't returned). The deposit-token path alone is sufficient if ≥15–30 deposit tokens exist; combined counter means fewer are needed if the victim already holds some tokens.

### Recommendation
Do not mutate `depositTokensOfAccount` for unsolicted inbound transfers, or gate additions behind recipient opt-in. Alternative: track collateral membership implicitly via `balanceOf > 0` iteration over the pool-level `depositTokens` set, or decouple the revert from `transfer` (e.g., skip adding on plain transfers and only enforce the cap on `deposit`/mint). A simpler fix is to exempt `seize`/liquidation flows from the cap.

### Proof of Concept
Hardhat sketch against a forked pool with ≥N deposit tokens:

```ts
// victim already has k tokens; attacker fills the remaining 30 - k slots
for (const dt of depositTokens.slice(0, 30)) {
  // attacker acquires 1 wei of deposit token (or transfers existing dust)
  await dt.connect(attacker).transfer(victim.address, 1);
}
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(30);

// victim (or anyone) depositing a NEW collateral type to victim reverts
const newDt = depositTokens[30]; // or a newly added deposit token
await expect(newDt.connect(victim).deposit(1, victim.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens');

// likewise issuing debt of a synth not yet in the victim's list reverts
// via addToDebtTokensOfAccount sharing the same counter
```

The core assertions (cap check, auto-add on first receipt, revert propagation through `_mint`) are all confirmed in `Pool.sol:143-148` and `DepositToken.sol:486-488, 518-520`.

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

**File:** contracts/Pool.sol (L204-208)
```text
    function addToDebtTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _debtToken = _msgSender();
        _revertIfSenderIsNotDebtToken(_debtToken);
        if (!debtTokensOfAccount.add(account_, _debtToken)) revert DebtTokenAlreadyExists();
    }
```

**File:** contracts/DepositToken.sol (L348-353)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
```

**File:** contracts/DepositToken.sol (L486-488)
```text
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L518-520)
```text
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```

**File:** contracts/DepositToken.sol (L523-525)
```text
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```
