# Windows private-state ownership correction

PR #54 at `771339e478dc69caf05be0a1e54547c91f199685` passed Linux and frontend CI,
but its Windows job failed 21 credential tests at the check that an object's owner
must equal TokenUser (1,173 passed, 45 skipped). The
[original Windows job](https://github.com/zhouy1017/Karajan/actions/runs/34022889312/job/101458601663)
retains the full trace. It establishes an owner mismatch, without directly recording the owner SID.

Windows assigns the creating token's default owner to a new object; that owner need
not be TokenUser. This follows Microsoft's [new-object ownership rules](https://learn.microsoft.com/en-us/windows/win32/secauthz/owner-of-a-new-object)
and [access-token fields](https://learn.microsoft.com/en-us/windows/win32/secauthz/access-tokens).
Karajan's existing private-state trust model excludes the trusted controller and
host administrators, and its DACL already permits those principals and SYSTEM.

The owner check now accepts exactly the current TokenUser, built-in Administrators
(`S-1-5-32-544`), or Local System (`S-1-5-18`). Every existing DACL, protected-directory,
ACE, file-type, hardlink and ancestor check still runs. This does not accept arbitrary
TokenOwner/groups, alter any existing object's ACL/owner, or change Linux behavior.

The five new public-store tests use two distinct evidence kinds. Four replace only
the Win32 owner observation while retaining real TokenUser, NTFS DACL reads and
SQLite; they are not physical SYSTEM/Administrators-owned-file observations. The
two trusted-owner cases additionally add an Everyone ACE to a synthetic directory
and require rejection. The fifth reads a genuinely created `0700` directory's owner
and token-default-owner classes; both were `current_user` on the local test host.

Before the fix, the two trusted-owner cases failed and three cases passed
([red](red.xml), [input](red-input.py.txt)). With the fix and the added DACL assertions,
the original 22 plus five new tests passed in 7.21 seconds ([final](final.xml)).
Ruff and backend mypy passed. [Independent review](independent-review.md) found no
blocking issue; it did not rerun the tests. No provider request or real-key read occurred.

The earlier real Go report and its `live-freeze.json` remain unchanged and bind the
original `771339e` source. Verify that original bundle on that revision. This follow-up
changes only Windows ownership acceptance; it is not a second real Go run or a claim
that the earlier report used the new credential-source file bytes. New exact-head CI
is reported on the PR, not inferred from the original Linux success.
