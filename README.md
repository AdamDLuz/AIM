# Intravo Messenger

Local-network messenger for Claude, Grok, and Codex. One daemon on each of your Windows, Mac, and Ubuntu computers knows that computer's GitHub folder, runs commands there, and moves files between the machines. Intravo Corp.

An agent on one computer can ask another to `git pull` and build, then take the compiled result back. `ivm task` runs even when the agent session on the other computer is closed. `ivm send` leaves mail until that agent reads `ivm inbox`.

Humans can use the same commands. Run `ivm --help` for the full list. Agents should pass `--json`.

## Example

Windows asks the Mac to update a repo, build, and send the result back:

```
ivm peers
ivm task adam-mac --cwd MyApp --from claude --to claude --wait --timeout 900 --return-file dist/MyApp --fetch-to incoming --shell "git pull && make"
```

`adam-mac` is the name chosen when that computer was installed. `--cwd` and `--return-file` are relative to the Mac's GitHub folder, which `ivm peers` prints. A directory comes back as a zip into `incoming` on the Windows machine.

To leave a note for Claude on that Mac instead of running a command:

```
ivm send adam-mac --from claude --to claude --message "pulled the build"
```

Claude on the Mac reads it with `ivm inbox --agent claude`.

## Install

Requires Python 3.11 or newer. The program uses the standard library only.

From a checkout of this repo on Windows:

```
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

On Mac or Ubuntu:

```
sh scripts/install.sh
```

Add `--name adam-win` (or `adam-mac`, `adam-ubuntu`) so the other machines see a stable name. Add `--github-root` when the GitHub folder is somewhere else. The installer otherwise looks for `Documents/GitHub`, `GitHub`, `source/repos`, `Projects`, `code`, or `src` under your home directory.

Install registers the `ivm` command, copies the agent skill for Claude, Codex, and Grok, and starts the daemon. On Windows it registers a logon task. If Task Scheduler refuses that task, the installer puts `IntravoMessenger.vbs` in the user Startup folder instead. New terminals pick up `ivm` after the user PATH change.

Config lives at `%USERPROFILE%\.intravo-messenger\config.json` on Windows and `~/.intravo-messenger/config.json` on Mac and Ubuntu. The shared key is in that file. Re-running install keeps the same key.

## Pair the other computers

On the first computer:

```
ivm pair-export
```

That writes a pair file (by default `~/.intravo-messenger/pair.json`). Copy the file to the other computer yourself. Do not paste it into a chat, commit it, or email it.

On each other computer, from a checkout of this repo:

```
sh scripts/install.sh --secret-file /path/to/pair.json --name adam-mac
```

Windows:

```
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 --secret-file C:\path\to\pair.json --name adam-ubuntu
```

Use a different `--name` on every machine. After that, `ivm peers` on any of them should list the others. If a machine stays missing, broadcast is blocked (Wi-Fi client isolation or a firewall). Pin the address:

```
ivm peers add 192.168.1.20
```

## Firewall

The daemon listens on TCP 4777. Discovery uses UDP 4778. Both are inbound on private networks only.

Windows, in an elevated PowerShell:

```
New-NetFirewallRule -DisplayName "Intravo Messenger" -Direction Inbound -Protocol TCP -LocalPort 4777 -Action Allow -Profile Private
New-NetFirewallRule -DisplayName "Intravo Messenger Discovery" -Direction Inbound -Protocol UDP -LocalPort 4778 -Action Allow -Profile Private
```

If Windows asks whether to allow Python, allow it on private networks. The installer does not add these rules.

Ubuntu, only when ufw is enabled:

```
sudo ufw allow from 192.168.0.0/16 to any port 4777 proto tcp
sudo ufw allow from 192.168.0.0/16 to any port 4778 proto udp
sudo ufw allow from 10.0.0.0/8 to any port 4777 proto tcp
sudo ufw allow from 10.0.0.0/8 to any port 4778 proto udp
```

macOS may ask for local network access the first time the daemon binds. Allow it.

## Security

The shared key is permission to run commands as the user who started the daemon, on every computer that has it. The daemon rejects clients that are not on a local network (loopback, private, or link-local). Uploads, downloads, and files returned from a task must stay inside that machine's GitHub folder. A shell command itself is not sandboxed.

Do not commit `config.json` or `pair.json`.

## Check one machine

```
ivm status
ivm whoami
ivm self-test
```

`ivm whoami` prints the node name, the operating system, and the GitHub folder. It does not print the key. `ivm self-test` sends a local message and runs `echo ivm-ok` on this computer. It does not prove that the Mac or the Ubuntu computer can see this one. After those computers are paired, `ivm peers` should show them online.
