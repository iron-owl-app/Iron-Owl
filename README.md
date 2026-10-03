# Iron Owl

A private personal finance app for Windows. It tracks bank accounts, credit cards, HSAs, 401(k)s and other retirement accounts, brokerage accounts and loans in one place. Accounts link through [Plaid](https://plaid.com) with your own Plaid keys, and anything Plaid can't reach can be tracked by hand.

Iron Owl runs on your PC at `http://127.0.0.1:8000`, and everything it stores is encrypted with your password.

**Your data stays on this PC. Iron Owl only talks to Plaid, to fetch your bank data, and to GitHub, to see if a new version is out. The update check sends nothing about you or your money.**

New here? Read [HELP.md](HELP.md): installing, getting your Plaid keys, the recovery sheet, backups and updates, in plain words. The same help is inside the app (menu, then **Help**), and it works while Iron Owl is locked.

## Install (Windows)

1. Download the newest `Iron-Owl-Setup-<version>.exe` from [Releases](https://github.com/iron-owl-app/Iron-Owl/releases).
2. Run it. The installer isn't code-signed yet, so Windows may say **"Windows protected your PC"**: click **More info**, then **Run anyway**.
3. No administrator rights are needed; it installs for the signed-in Windows user, with a Start menu entry and an uninstaller under **Settings > Apps**. Uninstalling keeps your data.
4. On first start, choose a master password (12+ characters), then print the recovery sheet Iron Owl shows next. Nobody can reset the password; the sheet is the only way back in if you forget it.
5. Add your Plaid keys under **Settings > Banks > Manage banks > Bank connection** (step by step in [HELP.md](HELP.md#getting-your-plaid-keys)), then link banks from **Accounts > Add account**.

## Connecting Plaid

Iron Owl uses your own Plaid account ("bring your own keys"):

1. Sign up at <https://dashboard.plaid.com/signup>. Sandbox (test) keys are free and immediate.
2. For your real banks, request Production access in the Plaid dashboard (Plaid's free Trial plan allows 10 bank connections, ever). Plaid reviews the request.
3. Copy your **Client ID** and **Secret** from **Developers > Keys** and paste them under **Settings > Banks > Manage banks > Bank connection**. Iron Owl checks them with Plaid, works out whether they're Sandbox or Production keys, and saves them after you confirm your master password. They're stored encrypted inside your vault (so they're in your encrypted backups too) and used only while Iron Owl is unlocked.
4. **Accounts > Add account** links an institution (bank or credit card, 401(k)/HSA/brokerage, or loan). One link imports every product the institution supports. Sandbox test login: username `user_good`, password `pass_good`.

On the Trial plan, turn on **Count bank connections** under **Settings > Banks > Manage banks > Linked institutions** so Iron Owl keeps count of the 10.

**Alternative for developers: the settings file.** A source checkout can instead put the keys in the `.env` file in the repo folder (created from `.env.example` on first run) and restart:
```
PLAID_CLIENT_ID=...
PLAID_SECRET=...
PLAID_ENV=sandbox
```
Keys there take priority over keys saved in the vault, and the app shows them read-only. Unlike the vault, that file is plain text on disk.

Plaid products used: **Transactions** (bank and credit), **Investments** (401(k), IRA, HSA and brokerage holdings) and **Liabilities** (student loans, mortgages and credit card APRs). There are no webhooks (a local app can't receive them): data syncs when you press **Sync**, and automatically on the schedule you choose under **Settings > Banks > Manage banks**.

## Features

- **Home**: what needs you, net worth, this month's budget and upcoming bills.
- **Budget**: one monthly amount of spending money, split across categories, with pace markers and a monthly review.
- **Bills and paychecks** (*Recurring*): detected from your history for you to confirm, a calendar with your checking balance day by day, and paycheck-to-paycheck planning.
- **Goals**: savings targets and debt payoff with projected finish dates.
- **Reports**: where the money went, habits, and paying off debt.
- **Rules and alerts** (in Settings): auto-categorize transactions and get in-app alerts for low balances, large charges, budgets, due dates, price changes and connections that need attention.
- **Transactions**: recategorize, split, tag, add notes, mark transfers, and export to CSV.
- **Quick search**: **Ctrl+K** jumps to any page, account or transaction.
- **Encrypted backups**: manual and automatic, under **Settings > Safety and backups**; restore on a new computer from the first screen.
- **Updates**: a daily check of this repo's GitHub Releases (with an off switch in **Settings > Colors, text size and updates**). An update is downloaded, its signature checked, and installed only when you click **Install update**.

## Security

| Threat | Protection |
|---|---|
| Laptop or backup stolen | The whole database is SQLCipher (AES-256) encrypted. The key is derived from your master password with Argon2id (64 MiB, 3 passes) and is never written to disk. |
| Someone walks up to your PC | Iron Owl locks after 15 minutes of no use (changeable in Settings), and the key is wiped from memory. |
| A malicious website in your browser | The server listens on 127.0.0.1 only and checks the Host header (blocks DNS rebinding). Changes need a custom header and a matching Origin (CSRF protection). Session cookies are `SameSite=Strict` and `HttpOnly`, the CSP is strict, and the app can't be framed. |
| Password guessing | Growing waits after 5 wrong tries. |
| Plaid tokens or keys leaking | Stored only inside the encrypted database; never sent to the browser (only the last 4 characters of the secret) or written to logs. Saving or removing keys needs your master password. |
| A tampered update | Update files are signed; Iron Owl installs only files whose signature matches its built-in public key. |

Recommendations:
- Turn on **BitLocker** for the drive and use a Windows account password.
- A backup holds your Plaid keys and access tokens (encrypted). Anyone with a backup and its password can use them.
- Restoring a backup replaces the current data only after you enter the current password; the replaced data is set aside, not deleted.
- Each backup opens with the master password that was current when it was made.

## Build from source

Needs Windows, Python 3.11 and Node.js (22, or 20.19+).

```bash
start.bat                               # first run: Python venv, frontend build, the settings file; then opens the app

# backend (from backend/)
.venv\Scripts\python -m pytest -q       # tests (FakePlaid; no network)
.venv\Scripts\python run.py             # API + built frontend on :8000

# frontend (from frontend/)
npm run dev                             # Vite on :5173, proxies /api to :8000 (needs DEV=1 in the settings file)
npm run dev:mock                        # UI only, with fake data; no backend
npm run test:unit                       # frontend unit tests
npx tsc --noEmit                        # typecheck
```

A source checkout never updates itself; pull new code with git instead.
`create-shortcut.ps1` (right-click, Run with PowerShell) puts a desktop shortcut to a source checkout's `start.bat`.

**The Windows installer** (by hand):
1. Get `python-3.11.9-embed-amd64.zip` from python.org and its SHA-256.
2. `powershell -NoProfile -ExecutionPolicy Bypass -File tools\release\build_package.ps1 -PythonZip <zip> -PythonZipSha256 <sha256>`
3. Install Inno Setup 6, then `powershell -NoProfile -ExecutionPolicy Bypass -File installer\build_installer.ps1` (makes `dist-installer\Iron-Owl-Setup-<version>.exe`).

**GitHub Actions**: tests run on every push (`.github/workflows/tests.yml`). Pushing a tag `vX.Y.Z` that matches `backend/app/version.py` runs `.github/workflows/release.yml`, which builds the installer and the signed update file and makes a draft release for a maintainer to publish.

Where things are: [SPEC.md](SPEC.md) (the product contract), [docs/MAP.md](docs/MAP.md) (every route, file and test) and [docs/guide/](docs/guide/) (architecture, running, hotspots).

## License

[MIT](LICENSE), © Iron Owl contributors. Third-party software that ships with the installer is listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Iron Owl is not affiliated with Plaid or GitHub.
