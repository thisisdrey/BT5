### Title
Account display-name spoofing via unrestricted Unicode/control bytes in `account_name` - (File: `actuator/src/main/java/org/tron/core/utils/TransactionUtil.java`)

### Summary
`AccountUpdateContract.account_name` (and the account-name field accepted by `AccountCreateContract`) is validated only for length, not for character content. Any signer can set an arbitrary byte sequence — including bidirectional-override, zero-width, or other non-printable Unicode code points — as their on-chain account name. That value is later served verbatim by the public `GetAccount`/`GetAccountById` HTTP and gRPC APIs and rendered as UTF-8 text by wallets/exchanges, enabling the same "display spoofing via crafted whitespace/Unicode" bug class described in CVE-2020-12397 (Thunderbird `From` header spoofing), but applied to TRON account identity display.

### Finding Description
`TransactionUtil.validAccountName()` only calls `validBytes()`, which checks length (`<= 200`) and allows empty — it performs **no character-set restriction**: [1](#0-0) 

Contrast this with `validAssetName`/`validAccountId`, which call `validReadableBytes()` and strictly reject any byte outside the printable ASCII range `0x21`–`0x7E` (rejecting spaces, control bytes, and multi-byte/Unicode sequences): [2](#0-1) 

`UpdateAccountActuator.validate()` calls only `validAccountName` before storing the name, and `execute()` writes the raw, unsanitized bytes into `AccountCapsule`: [3](#0-2) [4](#0-3) 

`CreateAccountActuator.validate()` doesn't even check the account name at all — the check is explicitly commented out: [5](#0-4) 

This stored `account_name` is subsequently exposed unfiltered through the public, unauthenticated wallet query surface: `Wallet.getAccount()` returns the raw capsule instance, [6](#0-5) 
which is served by `GetAccountServlet`/`GetAccountByIdServlet` (HTTP) and `RpcApiService.WalletApi/WalletSolidityApi.getAccount` (gRPC): [7](#0-6) [8](#0-7) 

When `visible=true`, `Util.printAccount` renders the field as a UTF-8 JSON string via `JsonFormat.printToString`, so any injected Unicode control/whitespace/bidi characters are decoded and passed straight to client UIs: [9](#0-8) 

### Impact Explanation
An attacker-controlled account name containing bidi-override or zero-width characters can be crafted to visually mimic another account's name (e.g., a well-known exchange or SR name) when rendered by wallets, explorers, or the public JSON APIs that echo `account_name`. This is a low-cost, on-chain identity-spoofing primitive that facilitates social-engineering / phishing attacks aimed at getting users to transfer funds to the impersonating account — analogous to the sender-spoofing impact of CVE-2020-12397. It does not directly move funds or corrupt consensus, so severity should be scoped to Medium (display/identity spoofing enabling downstream fraud), matching the CVSS 4.3 baseline of the reference CVE.

### Likelihood Explanation
Any funded account can broadcast a single `AccountUpdateContract` (or set the name at account creation) with an attacker-chosen byte string; only a length check (≤200 bytes) gates the value — there is no readability/printable-character restriction as exists for `validAssetName`/`validAccountId`. The malicious value is automatically surfaced by any node responding to `wallet/getaccount`/`wallet/getaccountbyid` HTTP calls or the `GetAccount`/`GetAccountById` gRPC methods, requiring no special privilege from the querying client. This makes the likelihood high for any attacker willing to craft the encoding.

### Recommendation
Apply the same `validReadableBytes`-style restriction (or an explicit Unicode bidi/format-character blacklist, e.g. reject categories `Cf`, `Cc`, and disallowed whitespace) to `validAccountName`, mirroring `validAssetName`/`validAccountId`, and add the missing account-name validation to `CreateAccountActuator.validate()`. Optionally, sanitize/normalize `account_name` (NFKC + strip bidi/format control characters) before returning it through `Wallet.getAccount`/`Util.printAccount` so historical accounts with already-set spoofing names are not rendered dangerously.

### Proof of Concept
1. Attacker account A signs and broadcasts an `AccountUpdateContract` with `account_name` set to a UTF-8 byte sequence containing e.g. U+202E (RIGHT-TO-LEFT OVERRIDE) followed by characters chosen so the rendered (reversed) text visually matches a legitimate exchange/SR account name.
2. `UpdateAccountActuator.validate()` passes because `validAccountName` only checks `length <= 200`.
3. `UpdateAccountActuator.execute()` stores the raw bytes into the account's `AccountCapsule`.
4. Any client calls `GET /wallet/getaccount?address=<A>&visible=true` (or the gRPC `GetAccount`), and `Util.printAccount` → `JsonFormat.printToString` returns the account JSON with `account_name` rendered as the spoofed UTF-8 string, which wallet UIs display verbatim to end users.

### Citations

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L73-102)
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
```

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L104-118)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateAccountActuator.java (L42-46)
```java
    byte[] ownerAddress = accountUpdateContract.getOwnerAddress().toByteArray();
    AccountCapsule account = chainBaseManager.getAccountStore().get(ownerAddress);

    account.setAccountName(accountUpdateContract.getAccountName().toByteArray());
    chainBaseManager.getAccountStore().put(ownerAddress, account);
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateAccountActuator.java (L75-79)
```java
    byte[] ownerAddress = accountUpdateContract.getOwnerAddress().toByteArray();
    byte[] accountName = accountUpdateContract.getAccountName().toByteArray();
    if (!TransactionUtil.validAccountName(accountName)) {
      throw new ContractValidateException("Invalid accountName");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/CreateAccountActuator.java (L88-90)
```java
//    if (contract.getAccountName().isEmpty()) {
//      throw new ContractValidateException("AccountName is null");
//    }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L335-358)
```java
  public Account getAccount(Account account) {
    AccountStore accountStore = chainBaseManager.getAccountStore();
    AccountCapsule accountCapsule = accountStore.get(account.getAddress().toByteArray());
    if (accountCapsule == null) {
      return null;
    }
    accountCapsule.importAllAsset();
    BandwidthProcessor processor = new BandwidthProcessor(chainBaseManager);
    processor.updateUsage(accountCapsule);

    EnergyProcessor energyProcessor = new EnergyProcessor(
        chainBaseManager.getDynamicPropertiesStore(),
        chainBaseManager.getAccountStore());
    energyProcessor.updateUsage(accountCapsule);

    long genesisTimeStamp = chainBaseManager.getGenesisBlock().getTimeStamp();
    accountCapsule.setLatestConsumeTime(genesisTimeStamp
        + BLOCK_PRODUCED_INTERVAL * accountCapsule.getLatestConsumeTime());
    accountCapsule.setLatestConsumeFreeTime(genesisTimeStamp
        + BLOCK_PRODUCED_INTERVAL * accountCapsule.getLatestConsumeFreeTime());
    accountCapsule.setLatestConsumeTimeForEnergy(genesisTimeStamp
        + BLOCK_PRODUCED_INTERVAL * accountCapsule.getLatestConsumeTimeForEnergy());
    sortFrozenV2List(accountCapsule);
    return accountCapsule.getInstance();
```

**File:** framework/src/main/java/org/tron/core/services/http/GetAccountServlet.java (L45-49)
```java
  private void fillResponse(boolean visible, Account account, HttpServletResponse response)
      throws Exception {
    Account reply = wallet.getAccount(account);
    Util.printAccount(reply, response, visible);
  }
```

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L369-379)
```java
    @Override
    public void getAccount(Account request, StreamObserver<Account> responseObserver) {
      ByteString addressBs = request.getAddress();
      if (addressBs != null) {
        Account reply = wallet.getAccount(request);
        responseObserver.onNext(reply);
      } else {
        responseObserver.onNext(null);
      }
      responseObserver.onCompleted();
    }
```

**File:** framework/src/main/java/org/tron/core/services/http/Util.java (L549-560)
```java
  public static void printAccount(Account reply, HttpServletResponse response, Boolean visible)
      throws java.io.IOException {
    if (reply != null) {
      if (visible) {
        response.getWriter().println(JsonFormat.printToString(reply, true));
      } else {
        response.getWriter().println(convertOutput(reply));
      }
    } else {
      response.getWriter().println("{}");
    }
  }
```
