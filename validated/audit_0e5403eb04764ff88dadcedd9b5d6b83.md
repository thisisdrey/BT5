### Title
Windows `sh_binary`/`py_binary` launcher CWD-influenced interpreter resolution allows unqualified `bash.exe`/`python.exe` execution - (File: `src/tools/launcher/bash_launcher.cc`, `src/tools/launcher/python_launcher.cc`, `src/tools/launcher/launcher.cc`)

### Summary
On Windows, `BashBinaryLauncher::Launch` and `PythonBinaryLauncher::Launch` fall back to the unqualified names `"bash.exe"` / `"python.exe"` whenever the hermetic/rlocation-resolved interpreter path does not exist, and hand that bare name straight to `BinaryLauncherBase::LaunchProcess`, which calls `CreateProcessW` with `lpApplicationName = nullptr` and `lpCurrentDirectory = nullptr`. This matches Windows' documented implicit executable search order, which checks the process's current working directory before consulting `PATH`. An attacker who controls files checked out into the build's working directory (e.g. via an untrusted branch/PR that CI builds and runs) can plant a file literally named `bash.exe` or `python.exe` there and have it executed instead of the legitimate interpreter, achieving code execution in the CI/build process.

### Finding Description
`BashBinaryLauncher::Launch` resolves the interpreter via `Rlocation`, and only if that resolved path does not exist on disk does it fall back to a bare name: [1](#0-0) 

`PythonBinaryLauncher::Launch` does the analogous fallback: [2](#0-1) 

Both then call `BinaryLauncherBase::LaunchProcess(executable, args)`, which builds a quoted command line via `CreateCommandLine` and invokes `CreateProcessW` with `lpApplicationName = nullptr` and, critically, `lpCurrentDirectory = nullptr` (i.e. the child inherits the launcher's own current working directory): [3](#0-2) 

When `lpApplicationName` is `NULL` and the first token of `lpCommandLine` is an unqualified module name (no path separators, e.g. `"bash.exe"`), the Win32 loader's documented resolution order is: (1) the directory the calling process was loaded from, (2) the current directory of the calling process, (3)-(5) system/Windows directories, and only last (6) the directories in `PATH`. That means the current working directory of the Bazel-produced launcher process is consulted *before* `PATH`.

Because `lpCurrentDirectory` is `nullptr`, the child inherits whatever CWD the top-level `bazel run`/test invocation used — typically the workspace/repo root or the exec root, both of which can contain attacker-supplied files from a repository checkout (e.g. a hostile PR branch that CI clones and runs `bazel run //:tool` from). If that checkout root contains a file named `bash.exe` or `python.exe`, and the launcher takes the "interpreter not found via Rlocation" fallback branch (a very common situation on Windows when no hermetic Python/Bash toolchain interpreter is wired into `py_runtime`/`sh_binary`, so it depends on a system interpreter located purely via `PATH`), the planted binary in the CWD is executed in place of the trusted `bash.exe`/`python.exe`.

### Impact Explanation
This lets an unprivileged party who can only publish/merge content that CI or a victim's build checks out (no host access, no credentials) achieve arbitrary code execution in the context of the build/CI process whenever it runs a `sh_binary` or `py_binary` target on Windows through these fallback code paths. This is a `cwd`-injection analog to CWE-78 (OS command injection via untrusted search-path/wrapper resolution), directly mirroring the reported openclaw bug class (ACPX Windows wrapper resolution falling back to a search that lets an untrusted `cwd` influence which binary is executed).

### Likelihood Explanation
The fallback path is reached whenever `Rlocation`'s resolved interpreter file does not exist on disk — a realistic default for users who rely on a system-installed interpreter discovered via `PATH` rather than a fully hermetic `py_runtime`/bash toolchain, which is common in Windows CI configurations. No special flags are required; `lpCurrentDirectory = nullptr` and `lpApplicationName = nullptr` are the unconditional defaults in `LaunchProcess`.

### Recommendation
- Never pass a bare/unqualified executable name (`"bash.exe"`, `"python.exe"`) to `CreateProcessW`. Perform an explicit, PATH-only resolution (equivalent to `SearchPathW` restricted to `PATH`, as already done for Bash detection in `blaze_util_windows.cc`'s `GetBinaryFromPath`) and pass the fully qualified resulting path as `lpApplicationName`.
- Alternatively, set an explicit, trusted `lpCurrentDirectory` (e.g. a fixed system directory) for `LaunchProcess` so the implicit Win32 search order cannot consult an attacker-influenced CWD.
- Fail closed (die with a clear error) if PATH-based resolution cannot find the interpreter, rather than silently trusting implicit CreateProcess search order.

### Proof of Concept
1. On Windows, create a workspace containing a `py_binary`/`sh_binary` target whose Python/Bash interpreter is not wired hermetically (default system interpreter case), so `Rlocation`'s resolved path does not exist and the launcher falls into the `"python.exe"`/`"bash.exe"` fallback branch (`src/tools/launcher/python_launcher.cc:51-55`, `src/tools/launcher/bash_launcher.cc:47-51`).
2. In the workspace root (the CWD from which `bazel run` is invoked, e.g. as checked out from an attacker-controlled branch), place an executable literally named `python.exe` (or `bash.exe`) that writes a marker file / spawns a reverse shell.
3. From that workspace root, run `bazel run //:target`.
4. Observe that the launcher's `CreateProcessW` call (`lpApplicationName=nullptr`, `lpCurrentDirectory=nullptr`) executes the attacker-planted `python.exe`/`bash.exe` from the CWD instead of the real interpreter on `PATH`, verifiable by the marker file being created — demonstrating cwd-influenced wrapper resolution leading to code execution, reproducible as a `src/test/shell/bazel/bazel_windows_cpp_test.sh`-style integration test that `cd`s into a directory containing a decoy `python.exe`/`bash.exe` before invoking the built launcher. [1](#0-0) [2](#0-1) [4](#0-3)

### Citations

**File:** src/tools/launcher/bash_launcher.cc (L31-51)
```text
ExitCode BashBinaryLauncher::Launch() {
  wstring bash_binary = this->GetLaunchInfoByKey(BASH_BIN_PATH);

  // If bash_binary is already "bash" or "bash.exe", that means we want to
  // rely on the shell binary in PATH, no need to do Rlocation.
  if (GetBinaryPathWithoutExtension(bash_binary) != L"bash") {
    // Rlocation returns the original path if bash_binary is an absolute path.
    bash_binary = this->Rlocation(bash_binary, true);
  }

  if (DoesFilePathExist(bash_binary.c_str())) {
    wstring bash_bin_dir = GetParentDirFromPath(bash_binary);
    wstring path_env;
    GetEnv(L"PATH", &path_env);
    path_env = bash_bin_dir + L";" + path_env;
    SetEnv(L"PATH", path_env);
  } else {
    // If specified bash binary path doesn't exist, then fall back to
    // bash.exe and hope it's in PATH.
    bash_binary = L"bash.exe";
  }
```

**File:** src/tools/launcher/python_launcher.cc (L33-55)
```text
ExitCode PythonBinaryLauncher::Launch() {
  wstring python_binary = this->GetLaunchInfoByKey(PYTHON_BIN_PATH);

  // There are three kinds of values for `python_binary`:
  // 1. An absolute path to a system interpreter. This is the case if
  // `--python_path` is set by the
  //    user, or if a `py_runtime` is used that has `interpreter_path` set.
  // 2. A runfile path to an in-workspace interpreter. This is the case if a
  // `py_runtime` is used that has `interpreter` set.
  // 3. The special constant, "python". This is the default case if neither of
  // the above apply. Rlocation resolves runfiles paths to absolute paths, and
  // if given an absolute path it leaves it alone, so it's suitable for cases 1
  // and 2.
  if (GetBinaryPathWithoutExtension(python_binary) != L"python") {
    // Rlocation returns the original path if python_binary is an absolute path.
    python_binary = this->Rlocation(python_binary, true);
  }

  // If specified python binary path doesn't exist, then fall back to
  // python.exe and hope it's in PATH.
  if (!DoesFilePathExist(python_binary.c_str())) {
    python_binary = L"python.exe";
  }
```

**File:** src/tools/launcher/launcher.cc (L219-298)
```text
void BinaryLauncherBase::CreateCommandLine(
    CmdLine* result, const wstring& executable,
    const vector<wstring>& arguments) const {
  wostringstream cmdline;
  cmdline << L'\"' << executable << L'\"';
  for (const auto& s : arguments) {
    cmdline << L' ' << s;
  }

  wstring cmdline_str = cmdline.str();
  if (cmdline_str.size() >= MAX_CMDLINE_LENGTH) {
    die(L"Command line too long: %s", cmdline_str.c_str());
  }

  // Copy command line into a mutable buffer.
  // CreateProcess is allowed to mutate its command line argument.
  wcsncpy(result->cmdline, cmdline_str.c_str(), MAX_CMDLINE_LENGTH - 1);
  result->cmdline[MAX_CMDLINE_LENGTH - 1] = 0;
}

bool BinaryLauncherBase::PrintLauncherCommandLine(
    const wstring& executable, const vector<wstring>& arguments) const {
  bool has_print_cmd_flag = false;
  for (const auto& arg : arguments) {
    has_print_cmd_flag |= (arg == L"--print_launcher_command");
  }
  if (has_print_cmd_flag) {
    wprintf(L"%s\n", executable.c_str());
    for (const auto& arg : arguments) {
      wprintf(L"%s\n", arg.c_str());
    }
  }
  return has_print_cmd_flag;
}

ExitCode BinaryLauncherBase::LaunchProcess(const wstring& executable,
                                           const vector<wstring>& arguments,
                                           bool suppressOutput) const {
  std::vector<std::wstring> escaped_arguments(arguments.size());
  std::transform(arguments.cbegin(), arguments.cend(),
                 escaped_arguments.begin(),
                 [this](const wstring& arg) { return EscapeArg(arg); });
  if (PrintLauncherCommandLine(executable, escaped_arguments)) {
    return 0;
  }
  // Set RUNFILES_DIR if:
  //   1. Symlink runfiles tree is enabled, or
  //   2. We couldn't find manifest file (which probably means we are running
  //   remotely).
  // Otherwise, set RUNFILES_MANIFEST_ONLY and RUNFILES_MANIFEST_FILE
  if (symlink_runfiles_enabled || manifest_file.empty()) {
    SetEnv(L"RUNFILES_DIR", runfiles_dir);
  } else {
    SetEnv(L"RUNFILES_MANIFEST_ONLY", L"1");
    SetEnv(L"RUNFILES_MANIFEST_FILE", manifest_file);
  }
  CmdLine cmdline;
  CreateCommandLine(&cmdline, executable, escaped_arguments);
  PROCESS_INFORMATION processInfo = {0};
  STARTUPINFOW startupInfo = {0};
  startupInfo.cb = sizeof(startupInfo);
  BOOL ok = CreateProcessW(
      /* lpApplicationName */ nullptr,
      /* lpCommandLine */ cmdline.cmdline,
      /* lpProcessAttributes */ nullptr,
      /* lpThreadAttributes */ nullptr,
      /* bInheritHandles */ FALSE,
      /* dwCreationFlags */
      suppressOutput ? CREATE_NO_WINDOW  // no console window => no output
                     : 0,
      /* lpEnvironment */ nullptr,
      /* lpCurrentDirectory */ nullptr,
      /* lpStartupInfo */ &startupInfo,
      /* lpProcessInformation */ &processInfo);
  if (!ok) {
    PrintError(L"Cannot launch process: %s\nReason: %hs", cmdline.cmdline,
               GetLastErrorString().c_str());
    return GetLastError();
  }
  WaitForSingleObject(processInfo.hProcess, INFINITE);
```
