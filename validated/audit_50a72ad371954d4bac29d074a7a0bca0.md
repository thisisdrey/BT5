### Title
Account name uniqueness index accepts empty/unreadable byte sequences, enabling visually-indistinguishable account name squatting - (File: actuator/src/main/java/org/tron/core/utils/TransactionUtil.java)

### Summary
`TransactionUtil.validAccountName()` only checks the **length** of an account name (`validBytes(accountName, MAX_ACCOUNT_NAME_LEN, true)`), unlike `validAssetName`/`validAccountId`, which additionally require the bytes to be within the printable ASCII range (`validReadableBytes`). Because `AccountUpdateContract` is a broadcastable, unprivileged transaction, and the resulting account name is used as a **unique lookup key** in `AccountIndexStore` (analogous to a domain/handle registry), an attacker can register account names containing empty strings, control characters, NUL bytes, whitespace, or other non-printable/invisible byte sequences that are visually indistinguishable from an already-registered legitimate account name, yet compare as distinct keys in the store. This mirrors the zNS bug class: acceptance of empty/invisible label content in a name resolution system used for identity/uniqueness, permitting spoof entries that look identical to a victim's registered name.

### Finding Description
`UpdateAccountActuator.validate()` is the only gate before an account name is persisted and indexed: [1](#0-0) 

It calls `TransactionUtil.validAccountName`, whose implementation performs only a length check and explicitly allows empty content, with no character-set restriction: [2](#0-1) 

Contrast this with `validAssetName`/`validAccountId`, which route through `validReadableBytes` and reject empty and non-printable byte values (`< 0x21` or `> 0x7E`) — the exact category of hardening the zNS advisory recommended (reject empty/invisible label content used for identity uniqueness): [3](#0-2) 

The accepted `accountName` bytes are then used verbatim as the **unique key** for account name resolution in `AccountIndexStore`, with uniqueness enforcement gated only by `AllowUpdateAccountName`: [4](#0-3) [5](#0-4) 

Because the byte-level uniqueness check (`accountIndexStore.has(accountName)`) is a strict byte comparison but the input can contain zero-width/invisible/control characters or be entirely empty, two accounts can end up with names that render identically to a human (e.g. in a wallet UI or block explorer) while occupying distinct keys in the index — the same "visually indistinguishable but distinct identifier" defect described in the zNS report, just applied to TRON's account-name registry instead of a domain registry.

Separately, `AccountIdIndexStore` (used for `SetAccountIdContract`) shows the project has already had to patch a related normalization bug (Turkish locale case-folding causing inconsistent keys across nodes), confirming this class of "name index inconsistency/collision" issue has previously affected java-tron's identity-registry stores: [6](#0-5) 

For `AccountIndexStore`/`accountName`, no equivalent readability/non-empty validation exists.

### Impact Explanation
Account names are intended to function as a human-facing, unique identifier (the actuator explicitly rejects re-registration of an existing name as "This name is existed"). Any downstream service (exchange integrations, explorers, wallets) that resolves an account by name via `AccountIndexStore` can be deceived by an attacker-registered name that is visually identical or confusingly similar to a legitimate, already-registered account name (via empty string, invisible/control characters, or non-printable byte sequences that render as blank/similar glyphs). This enables account/identity spoofing that can be leveraged for phishing-style fund misdirection — a user or automated system intending to interact with the legitimate account name instead resolves to the attacker's account. This satisfies the "unauthorized account operation" bar via social/identity confusion reachable from a single unprivileged, signed `AccountUpdateContract` transaction.

### Likelihood Explanation
`AccountUpdateContract` is broadcastable by any account holder with no special privilege (only requiring `AllowUpdateAccountName` in the default one-time-update mode, which is a normal, commonly-enabled network parameter). Crafting a byte string containing invisible/control characters or an empty value that passes `validAccountName` is trivial for any transaction broadcaster.

### Recommendation
Route `accountName` validation through the same readable-byte-range check used for `validAssetName`/`validAccountId` (or an explicit Unicode-confusable/normalization policy for UTF-8 display names), and disallow empty account names, mirroring the recommended zNS fix of disallowing empty/invisible label content in identity-resolution constructs. Additionally consider normalizing (e.g., NFC + confusable-skeleton mapping) before using the name as an `AccountIndexStore` key so that visually indistinguishable names cannot map to different index entries.

### Proof of Concept
1. Attacker account `A` (already funded, existing on chain) submits an `AccountUpdateContract` with `account_name` set to a byte string consisting of e.g. `0x00` (NUL) repeated, or Unicode zero-width space bytes, or an empty `ByteString`.
2. `UpdateAccountActuator.validate()` calls `TransactionUtil.validAccountName(accountName)`, which returns `true` for such inputs because only length is checked (`validBytes(..., allowEmpty=true)` with no character filtering).
3. `execute()` sets this name on the account and calls `chainBaseManager.getAccountIndexStore().put(account)`, storing a new, distinct key in `AccountIndexStore` that is visually blank/indistinguishable from another party's registered name (or literally blank, colliding conceptually with "no name" state used elsewhere).
4. Any wallet/exchange resolving accounts "by name" via `AccountIndexStore.get(name)` can be tricked into displaying/resolving the attacker's account as if it were the legitimately named account, enabling phishing-driven fund transfers to the attacker's address.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateAccountActuator.java (L75-97)
```java
    byte[] ownerAddress = accountUpdateContract.getOwnerAddress().toByteArray();
    byte[] accountName = accountUpdateContract.getAccountName().toByteArray();
    if (!TransactionUtil.validAccountName(accountName)) {
      throw new ContractValidateException("Invalid accountName");
    }
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid ownerAddress");
    }

    AccountCapsule account = chainBaseManager.getAccountStore().get(ownerAddress);
    if (account == null) {
      throw new ContractValidateException("Account does not exist");
    }

    if (account.getAccountName() != null && !account.getAccountName().isEmpty()
        && chainBaseManager.getDynamicPropertiesStore().getAllowUpdateAccountName() == 0) {
      throw new ContractValidateException("This account name is already existed");
    }

    if (chainBaseManager.getAccountIndexStore().has(accountName)
        && chainBaseManager.getDynamicPropertiesStore().getAllowUpdateAccountName() == 0) {
      throw new ContractValidateException("This name is existed");
    }
```

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L73-118)
```java
  public static boolean validAccountName(byte[] accountName) {
    return validBytes(accountName, MAX_ACCOUNT_NAME_LEN, true);
  }

  public static boolean validAssetDescription(byte[] description) {
    return validBytes(description, MAX_ASSET_DESCRIPTION_LEN, true);
  }

  public static boolean validUrl(byte[] url) {
    return validBytes(url, MAX_URL_LEN, false);
  }

  public static boolean validAccountId(byte[] accountId) {
    return validReadableBytes(accountId, MAX_ACCOUNT_ID_LEN) && accountId.length >= MIN_ACCOUNT_ID_LEN;
  }

  public static boolean validAssetName(byte[] assetName) {
    return validReadableBytes(assetName, MAX_ASSET_NAME_LEN);
  }

  public static boolean validTokenAbbrName(byte[] abbrName) {
    return validReadableBytes(abbrName, MAX_TOKEN_ABBR_NAME_LEN);
  }

  private static boolean validBytes(byte[] bytes, int maxLength, boolean allowEmpty) {
    if (ArrayUtils.isEmpty(bytes)) {
      return allowEmpty;
    }
    return bytes.length <= maxLength;
  }

  private static boolean validReadableBytes(byte[] bytes, int maxLength) {
    if (ArrayUtils.isEmpty(bytes) || bytes.length > maxLength) {
      return false;
    }
    // b must be readable
    for (byte b : bytes) {
      if (b < 0x21) {
        return false; // 0x21 = '!'
      }
      if (b > 0x7E) {
        return false; // 0x7E = '~'
      }
    }
    return true;
  }
```

**File:** chainbase/src/main/java/org/tron/core/store/AccountIndexStore.java (L21-24)
```java
  public void put(AccountCapsule accountCapsule) {
    put(accountCapsule.getAccountName().toByteArray(),
        new BytesCapsule(accountCapsule.getAddress().toByteArray()));
  }
```
