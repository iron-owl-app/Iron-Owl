; Iron Owl one-click Windows installer (Inno Setup 6).
;
; Build it with installer\build_installer.ps1, which reads the version from
; backend\app\version.py (the one place it lives) and passes the package folder that
; tools\release\build_package.ps1 made:
;     ISCC.exe /DAppVersion=2.0.0 /DPackageDir=<...\dist-package\Iron-Owl-2.0.0> [/DOutputDir=<folder>] iron-owl.iss
;
; What the .exe does: unpacks that package into a temporary folder and runs its install.ps1
; with -Unattended (the same install logic as a hand install: per-user, no administrator
; rights, %LOCALAPPDATA%\Programs\FinTrack, data kept in %LOCALAPPDATA%\FinTrack). install.ps1
; makes the Start menu (and optional desktop) shortcut "Iron Owl" and the Settings > Apps entry
; "Iron Owl", whose Uninstall runs uninstall.ps1 (it keeps the user's data). So Inno Setup
; itself installs no files and registers no uninstaller of its own.
;
; The optional "who to call for help" name is written to a UTF-8 file in Setup's private temp
; folder and handed to install.ps1 as -SupportContactFile <path>: the name itself never goes
; on a command line. Unsigned for 2.0.0 (Windows SmartScreen: "More info", then "Run anyway").

#ifndef AppVersion
  #error AppVersion is not defined. Build with installer\build_installer.ps1 (it reads backend\app\version.py).
#endif
#ifndef PackageDir
  #error PackageDir is not defined. Build with installer\build_installer.ps1 after tools\release\build_package.ps1.
#endif
#ifndef OutputDir
  #define OutputDir "..\dist-installer"
#endif

#define AppName "Iron Owl"
; Longest "who to call" name (backend\app\config.py SUPPORT_CONTACT_MAX).
#define ContactMax 60

[Setup]
AppId={{B4055092-E82E-466F-9231-A7B1B8C57A2D}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Iron Owl contributors
AppPublisherURL=https://github.com/iron-owl-app/Iron-Owl
AppSupportURL=https://github.com/iron-owl-app/Iron-Owl
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
; Per user, never elevated: install.ps1 refuses an administrator window.
PrivilegesRequired=lowest
; install.ps1 picks the folders and makes the shortcuts and the Settings > Apps entry.
CreateAppDir=no
DisableProgramGroupPage=yes
Uninstallable=no
CreateUninstallRegKey=no
; install.ps1 stops a running Iron Owl itself (it locks and backs up first).
CloseApplications=no
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
DisableWelcomePage=no
ShowLanguageDialog=no
SetupIconFile=..\packaging\brand\iron-owl.ico
WizardImageFile=..\packaging\brand\wizard-image.bmp
WizardSmallImageFile=..\packaging\brand\wizard-small.bmp
OutputDir={#OutputDir}
OutputBaseFilename=Iron-Owl-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel1=Install [name]
WelcomeLabel2=This puts [name/ver] on this computer, for the person signed in now.%n%nYour money details stay on this computer. If you used Iron Owl before, everything you saved is kept.
ReadyLabel1=[name] is ready to install.
ReadyLabel2a=Click Install. It takes a minute or two.
FinishedHeadingLabel=[name] is installed
FinishedLabelNoIcons=[name] is ready. Open it from the Start menu any time.
FinishedLabel=[name] is ready. Open it from the Start menu any time.
ClickFinish=Click Finish to close this window.

[CustomMessages]
ContactCaption=Who to call for help
ContactDescription=Optional. You can leave it blank.
ContactSubCaption=If something goes wrong, Iron Owl shows this name so you know who to ask. You can add or change it later in Settings, under Safety and backups.
ContactPrompt=Name and phone number (optional), for example: Sam 555-0100
ContactBadChars=Some of these characters can't be shown. Please type the name again.
DesktopIcon=Put an Iron Owl icon on the desktop
ExtraIcons=Extra icon:
Preparing=Preparing Iron Owl. This takes a minute or two...
OpenNow=Open Iron Owl now
NotAdmin=Please run this installer the normal way, not "as administrator".%n%nIron Owl installs only for the person signed in, so it doesn't need administrator rights.
FailedHeading=Iron Owl was not installed
FailedText=Something went wrong, and nothing you saved was changed.%n%nRestart your computer and run this installer again. If it still doesn't work, ask for help and mention this file:%n%n%1
FailedBox=Iron Owl could not be installed. Nothing you saved was changed.%n%nRestart your computer and run this installer again. If it still doesn't work, ask for help and mention this file:%n%n%1

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; GroupDescription: "{cm:ExtraIcons}"

[Files]
; The whole package, unpacked only into Setup's private temp folder (deleted when Setup ends).
Source: "{#PackageDir}\*"; DestDir: "{tmp}\package"; Flags: ignoreversion recursesubdirs createallsubdirs

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "{code:InstallParams}"; \
    WorkingDir: "{tmp}\package"; StatusMsg: "{cm:Preparing}"; Flags: runhidden waituntilterminated; \
    BeforeInstall: WriteContactFile; AfterInstall: CheckInstallResult
Filename: "{localappdata}\Programs\FinTrack\python\pythonw.exe"; \
    Parameters: """{localappdata}\Programs\FinTrack\launch.pyw"""; \
    WorkingDir: "{localappdata}\Programs\FinTrack"; Description: "{cm:OpenNow}"; \
    Flags: postinstall nowait skipifsilent; Check: InstallWorked

[Code]
var
  ContactPage: TInputQueryWizardPage;
  InstallOk: Boolean;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if IsAdmin() then
  begin
    SuppressibleMsgBox(CustomMessage('NotAdmin'), mbInformation, MB_OK, IDOK);
    Result := False;
  end;
end;

procedure InitializeWizard();
begin
  ContactPage := CreateInputQueryPage(wpWelcome, CustomMessage('ContactCaption'),
    CustomMessage('ContactDescription'), CustomMessage('ContactSubCaption'));
  ContactPage.Add(CustomMessage('ContactPrompt'), False);
  ContactPage.Edits[0].MaxLength := {#ContactMax};
end;

// Control and invisible formatting characters are refused, like the app does.
function HasHiddenChars(const S: String): Boolean;
var
  I, C: Integer;
begin
  Result := False;
  for I := 1 to Length(S) do
  begin
    C := Ord(S[I]);
    if (C < 32) or ((C >= 127) and (C <= 159)) or ((C >= $200B) and (C <= $200F)) or
       ((C >= $202A) and (C <= $202E)) or ((C >= $2066) and (C <= $2069)) or (C = $FEFF) then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

function ContactName(): String;
begin
  Result := Trim(ContactPage.Values[0]);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = ContactPage.ID) and (HasHiddenChars(ContactName()) or (Length(ContactName()) > {#ContactMax})) then
  begin
    MsgBox(CustomMessage('ContactBadChars'), mbError, MB_OK);
    Result := False;
  end;
end;

function ContactFile(): String;
begin
  Result := ExpandConstant('{tmp}\support-contact.txt');
end;

function ResultFile(): String;
begin
  Result := ExpandConstant('{tmp}\install-result.txt');
end;

function LogFile(): String;
begin
  // Outside Setup's temp folder, so it is still there after a failed install.
  Result := AddBackslash(GetTempDir()) + 'Iron-Owl-install.log';
end;

procedure WriteContactFile();
var
  Lines: TArrayOfString;
begin
  if ContactName() <> '' then
  begin
    SetArrayLength(Lines, 1);
    Lines[0] := ContactName();
    if not SaveStringsToUTF8File(ContactFile(), Lines, False) then
      Log('Could not write the support contact file.');
  end;
end;

// Only fixed paths go on the command line; the name travels in ContactFile().
function InstallParams(Param: String): String;
begin
  Result := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{tmp}\package\install.ps1') + '" -Unattended' +
    ' -LogFile "' + LogFile() + '" -ResultFile "' + ResultFile() + '"';
  if FileExists(ContactFile()) then
    Result := Result + ' -SupportContactFile "' + ContactFile() + '"';
  if not WizardIsTaskSelected('desktopicon') then
    Result := Result + ' -NoDesktopShortcut';
end;

procedure CheckInstallResult();
var
  Text: AnsiString;
begin
  InstallOk := LoadStringFromFile(ResultFile(), Text) and (Trim(String(Text)) = 'ok');
  if not InstallOk then
  begin
    Log('install.ps1 did not finish; see ' + LogFile());
    SuppressibleMsgBox(FmtMessage(CustomMessage('FailedBox'), [LogFile()]), mbError, MB_OK, IDOK);
  end;
end;

function InstallWorked(): Boolean;
begin
  Result := InstallOk;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and not InstallOk then
  begin
    WizardForm.FinishedHeadingLabel.Caption := CustomMessage('FailedHeading');
    WizardForm.FinishedLabel.Caption := FmtMessage(CustomMessage('FailedText'), [LogFile()]);
  end;
end;

// A failed install.ps1 makes Setup exit with 1 (for silent installs and scripts).
function GetCustomSetupExitCode(): Integer;
begin
  if InstallOk then
    Result := 0
  else
    Result := 1;
end;
