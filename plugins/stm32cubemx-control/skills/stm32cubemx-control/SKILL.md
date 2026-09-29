---
name: stm32cubemx-control
description: Drive STM32CubeMX programmatically instead of clicking through its GUI. Inspect what an MCU can be configured with from CubeMX's own device database, edit the .ioc control plane, let CubeMX validate and normalise the edit, regenerate code headlessly, and verify the result with a Keil MDK build. Use when configuring STM32 peripherals or middleware (TIM, ADC, COMP, OPAMP, FREERTOS, FATFS), when a CubeMX project has to change without opening the GUI, when CubeMX-generated code conflicts with hand-added sources, or when regenerated output must be checked.
---

# STM32CubeMX control

CubeMX has no public API, but its `-q` console accepts a small command language
(`set mode`, `set pin`, `set ip parameters`, `clock resolution`) and every command
drives CubeMX's own device model. That console, plus the device database and the
plain-text `.ioc` it reads and writes, is the whole control surface. This skill
wraps it and uses CubeMX headlessly as the validator and code generator.

## The three pieces

- `<cubemx>/db/mcu/<MCU>.xml` lists the IPs an MCU actually has, with their
  instance names and config files.
- `<cubemx>/db/mcu/IP/<IP>-<version>_Cube_Modes.xml` lists an IP's modes (which
  map to virtual pins such as `VP_TIM1_VS_...`) and its parameters with allowed
  values.
- `<cubemx>/db/mcu/config/<IP>-<family>_Configs.xml` lists family level
  parameters.

Read them with `ip-modes` and `ip-params` instead of guessing key names.

## Core rule: configure through the console, not by hand

CubeMX treats `Mcu.IP*`, `Mcu.Pin*` and `ProjectManager.functionlistsort` as
*output*. Writing `Mcu.IP3=TIM1` into the file by hand and reloading makes CubeMX
silently drop the IP again. Use `set mode` instead, so CubeMX itself enables the
peripheral and assigns the pins:

`set mode TIM1 "PWM Generation1 CH1 CH1N"`  ->  `OK`, CH1 lands on PA8, CH1N on PC13

Hand-editing is a fallback, not the primary route. Two things still true when you
do edit by hand:

- middleware and other non-pin IPs activate through a virtual pin, for example
  `VP_FREERTOS_VS_CMSIS_V2` or `VP_TIM1_VS_ControllerModeReset`. These survive a
  round trip.
- per-pin peripherals (PWM channels, capture inputs, UART pins) activate through
  `<PIN>.Mode` and `<PIN>.Signal`, and CubeMX only accepts them when the pin,
  signal and package all match its own model for that exact MCU.

So the reliable loop is always: **edit, then `normalize`, then read the diff.**
`normalize` makes CubeMX load and re-save the file. Whatever CubeMX refuses
disappears, and `ioc_diff` reports exactly which keys were dropped, added or
rewritten. Treat a non-empty `dropped` list as a failure, not a cosmetic change.

## Console commands

Run them with `script`, which brackets your commands with `config load` and
`config save` and then reports what changed:

`python scripts/cubemx.py script <project> --file cmds.txt`

| command | meaning |
| --- | --- |
| `set mode <Periph> <Mode>` | enable a peripheral; CubeMX assigns the pins. Quote modes containing spaces. |
| `set pin <Pin> <SIGNAL>` | map or relocate a signal, e.g. `set pin PB1 TIM1_CH3N` |
| `set ip parameters <IP> <Param> <Val>` | set a parameter; CubeMX does not validate the value |
| `set gpio parameters <Pin> <Param> <Val>` | `GPIO_Label`, `GPIO_PuPd`, `GPIO_Speed`, `PinState` |
| `clock resolution` | resolve the clock tree after RCC changes |
| `project generate` | generate code inside the same session |
| `load <MCU>` | open an MCU with no project loaded; the start of `create` |
| `project name` / `project path` / `project toolchain` | where a new project lands, and its toolchain |

Print the vocabulary with `script --help-console`. Prefer `--file` over `--cmd`
when commands contain quotes: PowerShell re-parses embedded quotes when handing
arguments to a native executable and silently splits them.

## Recipes

### Add a peripheral, including pin-bound ones

`python scripts/cubemx.py script <project> --cmd 'set mode USART2 Asynchronous' --cmd 'set mode FREERTOS CMSIS_V2'`

Then read `ioc_diff`: with `set mode`, `dropped` stays empty and CubeMX assigns the
pins. Batching matters because every `script` call boots a whole CubeMX session, so
put related commands in one call and add `project generate` at the end to generate
code in that same session.

`enable-ip` is the hand-editing fallback. It works for middleware and virtual-mode
IPs (`VP_FREERTOS_VS_CMSIS_V2`, `VP_TIM1_VS_ControllerModeReset`): CubeMX accepted
TIM1 that way and canonicalised `ControllerModeReset` to `Reset Mode`. It does not
work for pin-bound peripherals, where the hand-written IP entry is dropped on the
next load while the pins survive, leaving the project inconsistent.

### Change parameters of an IP that is already active

`python scripts/cubemx.py set <project-or-ioc> "TIM1.Period=8499" "TIM1.Prescaler=0" "TIM1.DeadTime=100"`

Keys in a `.ioc` escape spaces and colons (`TIM1.Channel-PWM\ Generation1\ CH1\ CH1N`);
pass the plain name and the toolkit escapes it. Values are never escaped.
Add new names to the IP's `IPParameters` list as well, or CubeMX may ignore them.

### Transplant a configuration from a working project

Assembling a complex peripheral key by key is the least reliable route. Copying
the block from a project that already works is far better:

`python scripts/cubemx.py merge <project> <reference.ioc> --ip TIM1 --pin PB13 --pin PB15 --nvic TIM1_`

Then `normalize` and check `dropped`. Prefer a reference for the **same MCU and
package**; blocks lifted from a bigger package contain pins and signals your part
does not have, and CubeMX will quietly drop the IP while keeping the pins, which
leaves the project inconsistent.

### Regenerate and verify

`python scripts/cubemx.py generate <project> --force --drop-group FreeRTOS`
`python scripts/cubemx.py build <project>`

## New projects, pin planning and rollback

**From nothing but an MCU name.** The console can start from a bare device:
`load` opens the MCU (the same step as picking a part in the GUI), `project name`
and `project path` decide where it lands, and `config save` writes the .ioc.
CubeMX then fills in the order code, database version and toolchain itself, so
the result is a project it fully owns.

`python scripts/cubemx.py create STM32G431CBUx MyProject E:\work --generate`

Writing the .ioc by hand instead does not work, and both failures are verified:
synthesised from the device database it leaves `Mcu.getDie()` null (the database
has no order-code index) and `project generate` throws NullPointerException;
stripped down to a skeleton the session hangs on a dialog script mode cannot
display. Driving `load` is what makes a real from-scratch project possible.

**Cloning** is the alternative when a known-good starting point is wanted,
including its peripheral configuration:

`python scripts/cubemx.py new <name> <out_dir> --from <reference.ioc>`

**Placing a pin.** `set mode` can leave a channel unassigned when CubeMX cannot
place it (a three-phase PWM request left CH3N off). Ask the device database where
it can go, then apply it:

`python scripts/cubemx.py pin-map STM32G431CBUx --filter TIM1_CH`
`python scripts/cubemx.py pin-plan <project> --want TIM1_CH3N --apply`

**Rollback.** Every `generate` takes a snapshot first, skipping build artefacts so
it stays small, and keeps the five newest:

`python scripts/cubemx.py backups`
`python scripts/cubemx.py restore <project> <snapshot-name>`

## Hard rules

1. **Close the CubeMX GUI first.** A running GUI holds the project in memory and
   overwrites your regenerated `.ioc` the moment anyone saves. `generate` detects
   this and refuses; do not reach for `--force` without closing it.
2. **Never pipe CubeMX stdout into a pipeline.** If the reader stops early, the
   JVM blocks on a full pipe and hangs forever. The toolkit redirects output to
   a file and drains it.
3. **Waiting for `STM32CubeMX.exe` is not waiting for CubeMX.** The .exe is only a
   launcher; it starts a JVM and exits immediately. The toolkit reads the inherited
   pipe and additionally waits for newly spawned JVM processes to disappear.
4. **CubeMX preserves file groups it did not create** when it rewrites the Keil
   `.uvprojx`. A previously hand-added kernel or driver folder keeps getting
   compiled and can shadow the CubeMX-managed sources, which shows up as a storm
   of unrelated compile errors. Remove those groups with `--drop-group` and drop
   their include paths with `--drop-include`.
5. **Write the script file as UTF-8 without a BOM.** Project paths with non-ASCII
   characters work once the script file is written that way.
6. **Keep the toolkit's own output out of the project.** Backups, CubeMX script
   files and logs go to the work directory (`CUBEMX_TOOLKIT_WORK_DIR`, default
   `~/.cubemx-toolkit`), never into the firmware tree.
7. **Enabling FreeRTOS changes the HAL time base question.** With the SYS time
   base left on SysTick, CubeMX generates a `SysTick_Handler` that feeds both
   `HAL_IncTick()` and `xPortSysTickHandler()`, which is correct. Moving the time
   base to a timer instead makes CubeMX emit `extern TIM_HandleTypeDef htim6`
   while `HAL_TIM_MODULE_ENABLED` is still commented out in
   `stm32g4xx_hal_conf.h`, producing an undefined-type error until a real TIM
   instance is configured. Prefer leaving the time base on SysTick.
8. **Scripts must run on Python 3.8.** `Path.write_text(newline=...)` is 3.10+;
   use the toolkit's `_write_text` helper.
9. **Batch commands.** Every `script`, `normalize` and `generate` call starts a new
   CubeMX session, which costs 20-30 seconds. Combine related commands.
10. **Do not trust `set ip parameters` values blindly.** CubeMX stores whatever
   string it is given; take the exact value from `ip-params`.

## Verification habits

- The console answers `OK` or `KO` per command; `command_results` and
  `failed_commands` report them, and a run counts as failed if any command said
  KO. This matters: CubeMX still ends with `Bye bye` after a command throws, so
  an exit-code check alone reports success for a run that produced nothing.
- **A hung session is detected, not waited out.** CubeMX can block on a dialog it
  cannot show in script mode. The runner watches the log for growth and kills the
  JVM after `CUBEMX_TOOLKIT_STALL_TIMEOUT` seconds (default 90) of silence,
  reporting `stalled: true` instead of burning the full timeout.
- **Defaults never appear in the .ioc.** Setting `TIM1 Prescaler 0` produced no
  `TIM1.Prescaler` key at all. A missing key means "default", not "not applied".
- **Complementary channels can be missed.** `set mode TIM1 "PWM Generation1 CH1 CH1N"`
  assigned CH1 and CH1N, but requesting all three phases still left CH3N unassigned.
  Always re-read the pins after configuring and close gaps with `set pin`.
- Run `clock resolution` after any RCC change.
- The STM32H7 family hangs in headless mode on a power-supply dialog CubeMX cannot
  display. F4/F7/G4/L4/G0 are fine.

## Command reference

| command | purpose |
| --- | --- |
| `env` | locate CubeMX, Keil UV4, firmware packs, db |
| `mcu <ref>` | IPs and memory of an MCU from the db |
| `ip-modes <ip> --filter <re>` | modes, virtual pins, parameters |
| `ip-params <ip>` | family level parameters |
| `show <project>` | MCU, IPs, virtual modes, pins of a .ioc |
| `set <project> k=v ...` | set .ioc keys |
| `enable-ip` / `disable-ip` | structural IP edits |
| `merge <project> <ref.ioc>` | transplant a config block |
| `normalize` | CubeMX validates and canonicalises |
| `script --file cmds.txt` | run raw console commands (set mode / set pin / ...) |
| `new <name> <dir> --from <ref.ioc>` | start a project by copying a complete reference |
| `create <mcu> <name> <dir> --generate` | build a project from an MCU name alone |
| `verify --expect <SIGNAL>` | pin map plus duplicate / unlisted / missing signal checks |
| `pin-map <mcu> --filter <re>` | which pins can carry a signal, from the device db |
| `pin-plan --want <SIGNAL> --apply` | pick a free pin and apply it through CubeMX |
| `backups` / `restore` | list and roll back the snapshots taken before generation |
| `generate` | headless code regeneration |
| `build` | Keil UV4 build and result parsing |
