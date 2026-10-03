# Iron Owl Help

## What Iron Owl is

Iron Owl keeps track of your money in one place: bank accounts, credit cards, savings, retirement accounts, investments and loans. It shows your balances, where your money goes each month, your bills and paychecks, and your goals.

It runs on your own Windows PC. Everything it knows is locked with your password.

## Your data stays on this PC

Your data stays on this PC. Iron Owl only talks to Plaid, to fetch your bank data, and to GitHub, to see if a new version is out. The update check sends nothing about you or your money.

Plaid is the company that connects Iron Owl to your banks. Iron Owl uses your own Plaid account for that (see "Getting your Plaid keys" below).

## Who to call

If someone set up Iron Owl for you, their name can show at the top of this Help page and on other screens, so you always know who to ask.

To add or change that name, go to **Settings**, then **Safety and backups**, then **Who to call for help**. Type a name and phone number, for example "Sam 555-0100", and click **Save**. Leave it blank if there's no one to name.

## You can ask Claude for help

If you're stuck, you can ask Claude, an AI assistant, at [claude.ai](https://claude.ai). Tell it you use Iron Owl and describe what you see on the screen. It can explain a step in plain words.

**Never share your password, your recovery sheet or your Plaid keys** with Claude or anyone else.

## Installing on Windows

1. Go to the [Iron Owl releases page](https://github.com/iron-owl-app/Iron-Owl/releases) and download the newest **Iron-Owl-Setup** file (for example Iron-Owl-Setup-2.0.0.exe).
2. Open the file you downloaded.
3. Windows may show a blue screen that says **"Windows protected your PC"**. This happens because Iron Owl is new and not yet known to Windows. Click **More info**, then click **Run anyway**.
4. The installer asks **who to call for help**. You can type a name and phone number, or leave it blank and add it later in Settings.
5. Choose whether you want an Iron Owl icon on your desktop.
6. Click **Install**. It takes a minute or two.
7. Click **Finish**. If **Open Iron Owl now** is ticked, Iron Owl opens.

You don't need to be an administrator. Iron Owl is installed only for the person signed in to Windows. You'll find it in the Start menu as **Iron Owl**.

The first time, Iron Owl asks you to choose a password. Then it shows your recovery sheet (see below).

To remove Iron Owl, open Windows **Settings**, then **Apps**, then **Installed apps**. Find **Iron Owl**, click the three dots, then **Uninstall**. A box asks if you're sure: click **Yes**. Your data is kept, so if you install Iron Owl again, everything is still there.

## Getting your Plaid keys

To link your banks, Iron Owl needs two keys from your own Plaid account. It takes about 10 minutes, and you can stop at any step and come back later.

1. **Get a free Plaid account.** Go to [Plaid's sign-up page](https://dashboard.plaid.com/signup) and sign up with your email address.
2. **Ask for real banks.** Right away, Plaid gives you test keys (Plaid calls them "Sandbox"). They only work with pretend banks. To link your real banks, ask Plaid for access to real banks in its dashboard and pick the free **Trial** plan. Plaid may ask a few questions first: say it's for your own personal finances.
3. **Copy your two keys.** In Plaid, open **Developers**, then **Keys** ([Plaid's keys page](https://dashboard.plaid.com/developers/keys)). Copy these two:
   - **Client ID**: a long mix of letters and numbers.
   - **Secret**: copy the **Production** secret once Plaid has approved you for real banks. Until then, the Sandbox one.
4. **Paste them into Iron Owl.** In Iron Owl, go to **Settings**, then **Banks**, then **Manage banks**. Under **Bank connection**, paste the Client ID and the Secret into their boxes. Iron Owl checks them with Plaid by itself.
5. When it says the keys work, type your Iron Owl password and click **Save and connect**.
6. **Link a bank.** Go to **Accounts**, then **Add account**, and sign in to your bank in the window that opens.

**Keep your keys private.** Don't share them with anyone: not by email, text or phone, and not with anyone who says they're from Iron Owl or Plaid. Only paste them into Iron Owl on this computer.

Plaid's free Trial plan lets you connect up to 10 banks, ever. Removing a bank doesn't give that connection back, so link only the banks you want to keep. If you're on the Trial plan, go to **Settings**, then **Banks**, then **Manage banks**, and under **Linked institutions** turn on **Count bank connections**. Iron Owl then keeps count for you.

Plaid's website changes from time to time. If a button has moved, look for **Keys** under **Developers**.

## Your recovery sheet

Nobody can reset your Iron Owl password for you. Your recovery sheet is the only way back in if you forget it.

When you first set up Iron Owl, it shows you a sheet with 36 numbers. Print it, or write the numbers down.

- Keep it with your important papers, like your passport or insurance papers.
- Don't keep it next to the computer.
- Don't take a photo of it or email it.

If you forget your password, click **Forgot your password?** on the password screen and type the numbers from your sheet. Then you choose a new password.

To make a new sheet, go to **Settings**, then **Safety and backups**, then **Recovery sheet**. The old sheet then stops working.

## Backups

A backup is a copy of all your Iron Owl data in one file. It only opens with your Iron Owl password.

- **Save a backup now**: go to **Settings**, then **Safety and backups**, then **Save a backup now**. The file goes to your Downloads folder.
- **Automatic backups**: in the same place, turn on **Automatic backups** and pick a folder. Iron Owl then saves a copy each time it locks, and once a day.
- **Restore**: **Restore from a backup** puts a backup's data back. On a new computer, the first Iron Owl screen has **Restore from a backup** too.

Each backup opens with the password you had when it was made. If you change your password later, older backups still need the old one.

## Updates

New versions of Iron Owl come out once they're ready. There's no fixed schedule.

Iron Owl checks GitHub once a day for a new version, only while it's unlocked. If one is out, Iron Owl downloads it, checks that it really comes from Iron Owl, and shows a message at the top of the page: **"A new version of Iron Owl is ready to install."**

- Click **Install update** to install it. Iron Owl makes a backup first, installs the update, restarts, and asks for your password again.
- Click **Not now** to be reminded tomorrow.
- **What's new** shows what changed.

Nothing installs until you click **Install update**.

To turn the daily check off, go to **Settings**, then **Colors, text size and updates**, and turn off **Check for updates once a day**. You can still click **Check now** any time.

## Opening Iron Owl and locking it

Open Iron Owl from the Start menu (or the desktop icon). Type your password to unlock it.

Iron Owl locks itself when you step away for a while. You can also lock it from the menu: **Lock Iron Owl**. When it's locked, your data can't be read without your password.
